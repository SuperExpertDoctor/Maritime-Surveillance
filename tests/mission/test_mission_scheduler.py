from dataclasses import replace
import json
import math
import time

import pytest

from src.mission.contracts import (
    MissionSelection,
    TaskCandidate,
    UavResource,
)
from src.mission.llm_gateway import LLMGateway
from src.mission.mission_scheduler import (
    Assignment,
    FeasibleEdge,
    MissionScheduler,
    MissionSnapshot,
    TaskRecord,
    _minimum_cost_matching,
    pair_selected_tasks,
    validate_selection,
)
from tests.mission.conftest import ScriptedTransport


def _task(task_id, *, kind="search", contact_id=None, bbox=None):
    if kind == "search" and bbox is None:
        index = int("".join(character for character in task_id if character.isdigit()) or 1)
        start = 1 + (index - 1) * 6
        bbox = (start, 1, start + 4, 6)
    return TaskCandidate(
        task_id,
        kind,
        bbox,
        contact_id,
        (),
        (),
        0.0,
        "high" if kind != "search" else "medium",
        4.0,
        1.0,
        1.0 if kind == "probe" else 0.5,
    )


def _resource(
    uav_id,
    *,
    operation="idle",
    current_task_id=None,
    generation=2,
    last_reassigned=0.0,
    remaining=100.0,
):
    return UavResource(
        uav_id,
        (float(len(uav_id)), 2.0),
        0.0,
        1.0,
        remaining,
        operation,
        current_task_id,
        generation,
        last_reassigned,
    )


def _edge(task_id, uav_id, cost):
    return FeasibleEdge(task_id, uav_id, cost, 5.0, 5.0, 2.0, f"map:{uav_id}:{task_id}")


def _snapshot(
    tasks,
    resources,
    edges,
    *,
    available=None,
    preemptible=None,
    snapshot_id="E01:10:1",
    now=10.0,
    active_tasks=(),
):
    resources = tuple(resources)
    return MissionSnapshot(
        snapshot_id,
        now,
        tuple(tasks),
        tuple(available if available is not None else [r.uav_id for r in resources if r.operation == "idle"]),
        tuple(preemptible if preemptible is not None else []),
        tuple((r.uav_id, r.generation) for r in resources),
        resources,
        tuple(edges),
        tuple(active_tasks),
        (),
        (),
        (),
        "memory-v1",
        7,
        "review summary",
    )


def _selection(snapshot, task_ids, *, preempt=(), defer=None):
    return {
        "schema_version": "mission-selection/v1",
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": list(task_ids),
        "preempt_uav_ids": list(preempt),
        "defer_reason": defer,
        "notes": "test selection",
    }


def test_matching_detects_shared_only_uav():
    snapshot = _snapshot(
        [_task("Q1"), _task("Q2")],
        [_resource("U1")],
        [_edge("Q1", "U1", 1.0), _edge("Q2", "U1", 2.0)],
        available=("U1",),
    )

    errors = validate_selection(_selection(snapshot, ["Q1", "Q2"]), snapshot)

    assert "infeasible_assignment" in errors


def test_pairing_minimizes_total_transit_cost_instead_of_greedy_order():
    snapshot = _snapshot(
        [_task("Q1"), _task("Q2")],
        [_resource("U1"), _resource("U2")],
        [
            _edge("Q1", "U1", 1.0),
            _edge("Q1", "U2", 2.0),
            _edge("Q2", "U1", 1.5),
            _edge("Q2", "U2", 100.0),
        ],
        available=("U1", "U2"),
    )
    selection = MissionSelection(
        "mission-selection/v1", snapshot.snapshot_id, ("Q1", "Q2"), (), None, "",
    )

    assignments = pair_selected_tasks(selection, snapshot)

    assert [(item.task_id, item.uav_id) for item in assignments] == [
        ("Q1", "U2"),
        ("Q2", "U1"),
    ]
    assert sum(
        next(edge.transit_time_min for edge in snapshot.feasible_edges
             if edge.task_id == item.task_id and edge.uav_id == item.uav_id)
        for item in assignments
    ) == pytest.approx(3.5)


@pytest.mark.timeout(5)
def test_large_matching_finds_exact_minimum_without_exponential_search():
    task_ids = tuple(f"Q{index}" for index in range(10))
    resource_ids = tuple(f"U{index}" for index in range(10))
    resources = {uav_id: _resource(uav_id) for uav_id in resource_ids}
    options = {
        task_id: tuple(
            _edge(task_id, uav_id, 0.0 if uav_id == f"U{9 - int(task_id[1:])}" else 1.0)
            for uav_id in resource_ids
        )
        for task_id in task_ids
    }

    matching = _minimum_cost_matching(
        task_ids,
        options,
        resources,
        required_preempt_uav_ids=frozenset({"U9"}),
    )

    assert matching is not None
    assert {task_id: edge.uav_id for task_id, edge in matching.items()} == {
        task_id: f"U{9 - int(task_id[1:])}" for task_id in task_ids
    }
    assert sum(edge.transit_time_min for edge in matching.values()) == pytest.approx(0.0)


def test_selection_rejects_unknown_ids_duplicate_contacts_and_overlapping_searches():
    tasks = [
        _task("Q1", kind="probe", contact_id="C1"),
        _task("Q2", kind="track", contact_id="C1"),
        _task("Q3", bbox=(1, 1, 5, 6)),
        _task("Q4", bbox=(4, 4, 8, 9)),
    ]
    snapshot = _snapshot(
        tasks,
        [_resource("U1"), _resource("U2"), _resource("U3")],
        [_edge(task.task_id, "U1", 1.0) for task in tasks],
        available=("U1", "U2", "U3"),
    )

    assert any("unknown_task_id" in error for error in validate_selection(
        _selection(snapshot, ["Q9"]), snapshot
    ))
    assert "duplicate_contact" in validate_selection(
        _selection(snapshot, ["Q1", "Q2"]), snapshot
    )
    assert any("overlapping_search" in error for error in validate_selection(
        _selection(snapshot, ["Q3", "Q4"]), snapshot
    ))


def test_selection_rejects_stale_snapshot_and_unused_preemption():
    task = _task("Q1", kind="probe", contact_id="C1")
    snapshot = _snapshot(
        [task],
        [_resource("U1", operation="coverage", current_task_id="S1"), _resource("U2")],
        [_edge("Q1", "U1", 1.0), _edge("Q1", "U2", 2.0)],
        available=("U2",),
        preemptible=("U1",),
    )

    stale = _selection(snapshot, ["Q1"])
    stale["snapshot_id"] = "E01:09:0"
    assert "stale_snapshot" in validate_selection(stale, snapshot)
    assert validate_selection(
        _selection(snapshot, ["Q1"], preempt=("U1",)), snapshot
    ) == ()

    no_edge_for_preempt = replace(
        snapshot,
        feasible_edges=(_edge("Q1", "U2", 2.0),),
    )
    assert any("unused_preempt_uav" in error for error in validate_selection(
        _selection(no_edge_for_preempt, ["Q1"], preempt=("U1",)),
        no_edge_for_preempt,
    ))


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "1", None, [], {}])
def test_selection_rejects_malformed_information_version(value):
    snapshot = _snapshot(
        [_task("Q1")],
        [_resource("U1")],
        [_edge("Q1", "U1", 1.0)],
        available=("U1",),
    )
    payload = _selection(snapshot, ["Q1"])
    payload["information_version"] = value

    assert validate_selection(payload, snapshot) == ("invalid_information_version",)


def test_selection_accepts_matching_information_version_and_reports_stale():
    snapshot = replace(
        _snapshot(
            [_task("Q1")],
            [_resource("U1")],
            [_edge("Q1", "U1", 1.0)],
            available=("U1",),
        ),
        _information_version=3,
    )

    matched = _selection(snapshot, ["Q1"])
    matched["information_version"] = 3
    assert validate_selection(matched, snapshot) == ()

    stale = _selection(snapshot, ["Q1"])
    stale["information_version"] = 2
    assert validate_selection(stale, snapshot) == ("stale_information_version",)


def test_scheduler_retries_a_selection_with_a_malformed_information_version():
    snapshot = _snapshot(
        [_task("Q1")],
        [_resource("U1")],
        [_edge("Q1", "U1", 1.0)],
        available=("U1",),
    )
    malformed = _selection(snapshot, ["Q1"])
    malformed["information_version"] = True
    transport = ScriptedTransport({
        "decision_maker": [
            json.dumps(malformed),
            json.dumps(_selection(snapshot, ["Q1"])),
        ],
    })
    scheduler = MissionScheduler(gateway=LLMGateway(transport=transport))

    batch = scheduler.decide(snapshot)

    assert batch is not None
    assert [item.task_id for item in batch.assignments] == ["Q1"]
    assert len(transport.calls) == 2
    correction = json.loads(transport.calls[1]["messages"][-1]["content"])
    assert correction["type"] == "validation_correction"
    assert correction["errors"] == ["invalid_information_version"]


def test_full_capacity_probe_preempts_only_ordinary_search_after_cooldown():
    probe = _task("Q-probe", kind="probe", contact_id="C1")
    search = _task("Q-search", bbox=(10, 10, 14, 15))
    snapshot = _snapshot(
        [probe, search],
        [
            _resource("U1", operation="coverage", current_task_id="Q-old", last_reassigned=0.0),
            _resource("U2", operation="return", current_task_id="Q-return"),
        ],
        [_edge("Q-probe", "U1", 1.0), _edge("Q-search", "U1", 2.0)],
        available=(),
        preemptible=("U1",),
        active_tasks=(TaskRecord(
            "Q-old", "search", "executing", (2, 2, 6, 7), None, (), "U1",
            None, 0.0, 1.0, None, None,
        ),),
    )
    selection = _selection(snapshot, ["Q-probe"], preempt=("U1",))

    assert validate_selection(selection, snapshot) == ()
    assignments = pair_selected_tasks(
        MissionSelection(
            "mission-selection/v1", snapshot.snapshot_id, ("Q-probe",), ("U1",), None, "",
        ),
        snapshot,
    )
    assert assignments == (Assignment("Q-probe", "U1", 2, "Q-old"),)
    assert validate_selection(
        _selection(snapshot, ["Q-probe"], preempt=("U2",)), snapshot
    )


def test_protected_returning_resource_and_cooldown_cannot_be_preempted():
    task = _task("Q1", kind="track", contact_id="C1")
    returning = _snapshot(
        [task],
        [_resource("U1", operation="return", current_task_id="R1")],
        [_edge("Q1", "U1", 1.0)],
        preemptible=("U1",),
    )
    assert "infeasible_assignment" in validate_selection(
        _selection(returning, ["Q1"], preempt=("U1",)), returning
    )

    cooldown = _snapshot(
        [task],
        [_resource("U1", operation="coverage", current_task_id="S1", last_reassigned=8.0)],
        [_edge("Q1", "U1", 1.0)],
        preemptible=("U1",),
    )
    assert any("reassignment_cooldown" in error for error in validate_selection(
        _selection(cooldown, ["Q1"], preempt=("U1",)), cooldown
    ))


def test_empty_selection_requires_defer_reason_when_legal_work_exists():
    task = _task("Q1")
    snapshot = _snapshot(
        [task], [_resource("U1")], [_edge("Q1", "U1", 1.0)], available=("U1",)
    )
    assert "underutilized_feasible_work:Q1" in validate_selection(
        _selection(snapshot, []), snapshot
    )
    assert "underutilized_feasible_work:Q1" in validate_selection(
        _selection(snapshot, [], defer="no feasible edges"), snapshot
    )


def test_reason_does_not_allow_empty_selection_with_idle_feasible_work():
    snapshot = _snapshot(
        [_task("S1")],
        [_resource("U1", generation=0)],
        [_edge("S1", "U1", 1.0)],
    )

    errors = validate_selection(
        _selection(snapshot, [], defer="no feasible edges"), snapshot,
    )

    assert "underutilized_feasible_work:S1" in errors


def test_partial_selection_must_add_independent_idle_work():
    snapshot = _snapshot(
        [_task("S1"), _task("S2")],
        [_resource("U1"), _resource("U2")],
        [_edge("S1", "U1", 1.0), _edge("S2", "U2", 1.0)],
    )

    errors = validate_selection(_selection(snapshot, ["S1"]), snapshot)

    assert "underutilized_feasible_work:S2" in errors


def test_underutilization_allows_rematching_selected_work_to_use_idle_uav():
    snapshot = _snapshot(
        [_task("S1"), _task("S2")],
        [_resource("U1"), _resource("U2")],
        [
            _edge("S1", "U1", 1.0),
            _edge("S1", "U2", 5.0),
            _edge("S2", "U1", 1.0),
        ],
    )

    errors = validate_selection(_selection(snapshot, ["S1"]), snapshot)

    assert "underutilized_feasible_work:S2" in errors


def test_underutilization_does_not_force_work_without_an_idle_resource():
    snapshot = _snapshot(
        [_task("S1"), _task("S2")],
        [_resource("U1")],
        [_edge("S1", "U1", 1.0), _edge("S2", "U1", 1.0)],
    )

    assert validate_selection(_selection(snapshot, ["S1"]), snapshot) == ()


def test_hidden_candidate_cannot_be_selected_or_create_underutilization_witness():
    snapshot = _snapshot(
        [_task("S1"), _task("S2")],
        [_resource("U1"), _resource("U2")],
        [_edge("S1", "U1", 1.0), _edge("S2", "U2", 1.0)],
    )

    assert "selected_task_not_visible:S2" in validate_selection(
        _selection(snapshot, ["S2"]), snapshot, visible_task_ids=frozenset({"S1"})
    )
    assert validate_selection(
        _selection(snapshot, ["S1"]), snapshot, visible_task_ids=frozenset({"S1"})
    ) == ()


def test_scheduler_rejects_selection_hidden_by_this_prompt_payload():
    snapshot = _snapshot(
        [_task("S1"), _task("S2")],
        [_resource("U1"), _resource("U2")],
        [_edge("S1", "U1", 1.0), _edge("S2", "U2", 1.0)],
    )
    scheduler = MissionScheduler(
        max_tasks_in_prompt=1,
        selection_provider=lambda current, _payload: _selection(current, ["S2"]),
    )

    assert scheduler.decide(snapshot) is None
    assert scheduler.last_selection_errors == ("selected_task_not_visible:S2",)


def test_approved_active_task_can_be_selected_on_its_original_id():
    active = TaskRecord(
        "Q1", "search", "approved", (10, 10, 14, 14), None, (), None,
        "call-1", 0.0, None, None, "preempted",
    )
    snapshot = _snapshot(
        [],
        [_resource("U1")],
        [_edge("Q1", "U1", 1.0)],
        available=("U1",),
        active_tasks=(active,),
    )

    assert validate_selection(_selection(snapshot, ["Q1"]), snapshot) == ()
    assert pair_selected_tasks(
        MissionSelection(
            "mission-selection/v1", snapshot.snapshot_id, ("Q1",), (), None, "",
        ),
        snapshot,
    ) == (Assignment("Q1", "U1", 2, None),)


def test_terminal_task_record_does_not_shadow_reusable_candidate():
    blocked = TaskRecord(
        "Q1", "search", "blocked", (10, 10, 14, 14), None, (), None,
        "call-1", 0.0, 0.0, 1.0, "coverage_incomplete",
    )
    snapshot = _snapshot(
        [_task("Q1", bbox=(10, 10, 14, 14))],
        [_resource("U1")],
        [_edge("Q1", "U1", 1.0)],
        available=("U1",),
        active_tasks=(blocked,),
    )

    assert validate_selection(_selection(snapshot, ["Q1"]), snapshot) == ()


def test_selection_rejects_transit_that_exhausts_resource_range():
    task = _task("Q1", kind="probe", contact_id="C1")
    snapshot = _snapshot(
        [task],
        [_resource("U1", remaining=100.0)],
        [FeasibleEdge("Q1", "U1", 100.0, 5.0, 5.0, 2.0, "map:U1:Q1")],
        available=("U1",),
    )

    assert "infeasible_assignment" in validate_selection(
        _selection(snapshot, ["Q1"]), snapshot
    )


def test_selection_rejects_preemption_without_selected_work():
    snapshot = _snapshot(
        [],
        [_resource("U1", operation="coverage", current_task_id="S1")],
        [],
        available=(),
        preemptible=("U1",),
    )

    assert "preempt_requires_selected_task" in validate_selection(
        _selection(snapshot, [], preempt=("U1",)), snapshot
    )


def test_scheduler_prompt_contains_complete_snapshot_and_returns_batch():
    task = _task("Q1", kind="probe", contact_id="C1")
    snapshot = _snapshot(
        [task], [_resource("U1")], [_edge("Q1", "U1", 1.0)], available=("U1",)
    )

    class Gateway:
        def __init__(self):
            self.payload = None
            self.call_log = [{
                "call_id": "call-1",
                "role": "decision_maker",
                "model": "LongCat-2.0",
                "attempts": [],
                "success": True,
                "failure_category": None,
            }]

        def request_json(self, **kwargs):
            from src.mission.llm_gateway import ModelResult

            self.payload = kwargs["user_payload"]
            self.call_log[0]["attempts"].append({
                "attempt": 1,
                "messages": [
                    {"role": "system", "content": kwargs["system_prompt"]},
                    {
                        "role": "user",
                        "content": json.dumps(kwargs["user_payload"], ensure_ascii=False),
                    },
                ],
                "raw_output": json.dumps(
                    _selection(snapshot, ["Q1"]), ensure_ascii=False,
                ),
                "errors": [],
            })
            return ModelResult(
                "call-1",
                True,
                _selection(snapshot, ["Q1"]),
                (),
                None,
            )

    gateway = Gateway()
    scheduler = MissionScheduler(gateway=gateway)
    batch = scheduler.decide(snapshot)

    assert batch == type(batch)(snapshot.snapshot_id, (Assignment("Q1", "U1", 2, None),), "call-1")
    serialized = gateway.payload["snapshot"]
    assert serialized["resources"]
    assert serialized["feasible_edges"]
    assert serialized["active_tasks"] == []
    assert serialized["contacts"] == []
    assert serialized["intents"] == []
    assert serialized["planning_map_version"] == 7
    trace = scheduler.selection_interaction()
    assert trace["model"] == "LongCat-2.0"
    assert trace["system_prompt"]
    assert '"Q1"' in trace["user_prompt"]
    assert '"Q1"' in trace["response"]
    assert trace["validation"] == {"is_valid": True, "errors": []}
    assert trace["attempts"][0]["response"] == trace["response"]


def test_scheduler_prompt_compacts_large_feasible_edge_graph():
    tasks = [_task(f"Q{index}") for index in range(1, 13)]
    resources = [_resource("U1"), _resource("U2")]
    edges = [
        _edge(task.task_id, resource.uav_id, float(index + 1))
        for index, task in enumerate(tasks)
        for resource in resources
    ]
    snapshot = _snapshot(tasks, resources, edges, available=("U1", "U2"))

    scheduler = MissionScheduler(
        max_tasks_in_prompt=4,
        selection_provider=lambda current, _payload: _selection(
            current, ["Q1", "Q2"]
        ),
    )
    assert scheduler.decide(snapshot) is not None

    prompt_edges = scheduler.last_selection_payload["snapshot"]["feasible_edges"]

    assert prompt_edges
    assert len(prompt_edges) <= 4
    assert all("route_cache_key" not in edge for edge in prompt_edges)
    assert all(
        set(edge) == {"task_id", "uav_options"}
        for edge in prompt_edges
    )
    assert all(
        set(option) == {"uav_id", "transit_time_min", "total_range_cells"}
        for edge in prompt_edges
        for option in edge["uav_options"]
    )
    assert next(edge for edge in prompt_edges if edge["task_id"] == "Q1") == {
        "task_id": "Q1",
        "uav_options": [
            {"uav_id": "U1", "transit_time_min": 1.0, "total_range_cells": 12.0},
            {"uav_id": "U2", "transit_time_min": 1.0, "total_range_cells": 12.0},
        ],
    }


def test_scheduler_prompt_filters_overlapping_search_candidates():
    tasks = [
        _task("Q1", bbox=(1, 1, 5, 5)),
        _task("Q2", bbox=(3, 3, 7, 7)),
        _task("Q3", bbox=(9, 9, 13, 13)),
    ]
    snapshot = _snapshot(
        tasks,
        [_resource("U1")],
        [_edge(task.task_id, "U1", 1.0) for task in tasks],
        available=("U1",),
    )

    scheduler = MissionScheduler(
        max_tasks_in_prompt=2,
        selection_provider=lambda current, _payload: _selection(current, ["Q1"]),
    )
    assert scheduler.decide(snapshot) is not None

    prompt_snapshot = scheduler.last_selection_payload["snapshot"]
    prompt_candidates = prompt_snapshot["candidates"]
    prompt_ids = {candidate["task_id"] for candidate in prompt_candidates}

    assert "Q1" in prompt_ids
    assert "Q2" not in prompt_ids
    assert "Q3" in prompt_ids
    assert set(prompt_snapshot["prompt_sources"]) <= prompt_ids
    assert set(prompt_snapshot["prompt_skip_cycles"]) <= prompt_ids
    assert {
        edge["task_id"] for edge in prompt_snapshot["feasible_edges"]
    } <= prompt_ids
    assert "Q2" not in {
        edge["task_id"] for edge in prompt_snapshot["feasible_edges"]
    }
    assert prompt_snapshot["prompt_geometry_filtered"] is True


def test_scheduler_prompt_distinguishes_count_truncation_from_geometry_filter():
    tasks = [_task(f"Q{index}") for index in range(1, 4)]
    snapshot = _snapshot(
        tasks,
        [_resource("U1")],
        [_edge(task.task_id, "U1", 1.0) for task in tasks],
        available=("U1",),
    )

    scheduler = MissionScheduler(
        max_tasks_in_prompt=2,
        selection_provider=lambda current, _payload: _selection(current, ["Q1"]),
    )
    assert scheduler.decide(snapshot) is not None

    prompt_snapshot = scheduler.last_selection_payload["snapshot"]

    assert prompt_snapshot["candidates_truncated"] is True
    assert prompt_snapshot["prompt_geometry_filtered"] is False


def test_scheduler_rejects_response_after_absolute_deadline():
    task = _task("Q1", kind="probe", contact_id="C1")
    snapshot = _snapshot(
        [task], [_resource("U1")], [_edge("Q1", "U1", 1.0)], available=("U1",)
    )

    class SlowGateway:
        def __init__(self):
            self.kwargs = None

        def request_json(self, **kwargs):
            from src.mission.llm_gateway import ModelResult

            self.kwargs = kwargs
            time.sleep(0.02)
            return ModelResult(
                "late-call",
                True,
                _selection(snapshot, ["Q1"]),
                (),
                None,
            )

    gateway = SlowGateway()
    scheduler = MissionScheduler(
        gateway=gateway,
        postprocess_reserve_seconds=0.001,
    )
    deadline = time.perf_counter() + 0.005

    batch = scheduler.decide(snapshot, deadline_monotonic=deadline)

    assert batch is None
    assert scheduler.last_selection_success is False
    assert scheduler.last_selection_errors == ("decision_deadline_exceeded",)
    assert gateway.kwargs["deadline_monotonic"] == deadline
    assert gateway.kwargs["transport_deadline_monotonic"] == pytest.approx(
        deadline - 0.001
    )


def test_task_allocator_exposes_unified_snapshot_and_preserves_gateway():
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.task_allocator import TaskAllocator

    gateway = object()
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=gateway)

    snapshot = allocator.build_mission_snapshot(now_min=3.0)

    assert allocator.llm_client.gateway is gateway
    assert allocator.mission_scheduler.gateway is gateway
    assert snapshot.resources
    assert snapshot.uav_generations
    assert snapshot.planning_map_version == allocator.sm.obstacle_version
    assert snapshot.sim_time_min == 3.0


def test_task_allocator_edge_budget_includes_transit_and_returns_from_goal():
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.task_allocator import TaskAllocator

    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    task = TaskCandidate(
        "Q1", "search", (10, 10, 14, 14), None, (), ("U1",), 0.0,
        "medium", 5.0, 1.0, 0.5,
    )
    resource = UavResource(
        "U1", (8.0, 12.0), 0.0, 1.0, 100.0, "idle", None, 0, 0.0,
    )

    edges = allocator._mission_edges((task,), (resource,), (), allocator.sm.obstacle_version)

    assert len(edges) == 1
    edge = edges[0]
    target = (12.0, 12.0)
    expected_return = math.dist(target, allocator.sm.get_base_positions()[0])
    current_to_base = math.dist(resource.position_cells, allocator.sm.get_base_positions()[0])
    assert edge.return_range_cells >= expected_return - 1e-6
    assert edge.return_range_cells != pytest.approx(current_to_base)
    assert edge.mission_range_cells >= (
        task.estimated_duration_min * resource.speed_cells_min
    )
    assert (
        edge.transit_time_min * resource.speed_cells_min
        + edge.mission_range_cells
        + edge.return_range_cells
        + edge.reserve_range_cells
    ) <= resource.remaining_range_cells


def test_task_allocator_ignores_unreachable_bases_when_computing_return_range(monkeypatch):
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.task_allocator import TaskAllocator

    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    task = TaskCandidate(
        "Q-probe", "probe", (10, 10, 14, 14), None, (), ("U1",), 0.0,
        "high", 5.0, 1.0, 0.5,
    )
    resource = UavResource(
        "U1", (8.0, 12.0), 0.0, 1.0, 100.0, "idle", None, 0, 0.0,
    )
    monkeypatch.setattr(allocator.sm, "get_base_positions", lambda: ((1, 1), (2, 2)))
    monkeypatch.setattr(
        allocator,
        "_mission_route_metrics",
        lambda *_args: (1.0, 2.0, (12.0, 12.0)),
    )

    def return_distance(_target, _resource, base, _map_version):
        return None if base == (1.0, 1.0) else 3.0

    monkeypatch.setattr(allocator, "_return_route_distance", return_distance)

    edges = allocator._mission_edges(
        (task,), (resource,), (), allocator.sm.obstacle_version,
    )

    assert len(edges) == 1
    assert edges[0].return_range_cells == pytest.approx(3.0)


def test_task_allocator_search_edge_uses_complete_route_and_final_endpoint(monkeypatch):
    from types import SimpleNamespace

    from src.schedule.config_loader import ConfigLoader
    from src.schedule.task_allocator import TaskAllocator

    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    task = TaskCandidate(
        "Q1", "search", (10, 10, 14, 14), None, (), ("U1",), 0.0,
        "medium", 5.0, 1.0, 0.5,
    )
    resource = UavResource(
        "U1", (8.0, 12.0), 0.0, 1.0, 100.0, "idle", None, 0, 0.0,
    )
    route = SimpleNamespace(
        path=((8.0, 12.0, 0.0), (10.0, 12.0, 0.0), (10.0, 14.0, 0.0)),
        transit_end_index=1,
        scanned_swath_count=1,
    )
    monkeypatch.setattr("src.schedule.task_allocator.plan_search_route", lambda _request: route)
    captured = {}

    def return_distance(target, _resource, _base, _map_version):
        captured["target"] = target
        return 3.0

    monkeypatch.setattr(allocator, "_return_route_distance", return_distance)

    edges = allocator._mission_edges(
        (task,), (resource,), (), allocator.sm.obstacle_version,
    )

    assert len(edges) == 1
    assert edges[0].transit_time_min == pytest.approx(2.0)
    assert edges[0].mission_range_cells == pytest.approx(2.0)
    assert captured["target"] == (10.0, 14.0)

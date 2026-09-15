from dataclasses import replace
import math

import pytest

from src.mission.contracts import (
    ContactSnapshot,
    Intent,
    IntentStatus,
    MissionSelection,
    TaskCandidate,
    UavResource,
)
from src.mission.mission_scheduler import (
    Assignment,
    FeasibleEdge,
    MissionScheduler,
    MissionSnapshot,
    TaskRecord,
    UavResource,
    pair_selected_tasks,
    validate_selection,
)


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
    assert "empty_selection_requires_defer_reason" in validate_selection(
        _selection(snapshot, []), snapshot
    )
    assert validate_selection(
        _selection(snapshot, [], defer="hold for higher priority contact"), snapshot
    ) == ()


def test_scheduler_prompt_contains_complete_snapshot_and_returns_batch():
    task = _task("Q1", kind="probe", contact_id="C1")
    snapshot = _snapshot(
        [task], [_resource("U1")], [_edge("Q1", "U1", 1.0)], available=("U1",)
    )

    class Gateway:
        def __init__(self):
            self.payload = None

        def request_json(self, **kwargs):
            from src.mission.llm_gateway import ModelResult

            self.payload = kwargs["user_payload"]
            return ModelResult(
                "call-1",
                True,
                _selection(snapshot, ["Q1"]),
                (),
                None,
            )

    gateway = Gateway()
    batch = MissionScheduler(gateway=gateway).decide(snapshot)

    assert batch == type(batch)(snapshot.snapshot_id, (Assignment("Q1", "U1", 2, None),), "call-1")
    serialized = gateway.payload["snapshot"]
    assert serialized["resources"]
    assert serialized["feasible_edges"]
    assert serialized["active_tasks"] == []
    assert serialized["contacts"] == []
    assert serialized["intents"] == []
    assert serialized["planning_map_version"] == 7


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
    assert edge.mission_range_cells == pytest.approx(task.estimated_duration_min * resource.speed_cells_min)
    assert (
        edge.transit_time_min * resource.speed_cells_min
        + edge.mission_range_cells
        + edge.return_range_cells
        + edge.reserve_range_cells
    ) <= resource.remaining_range_cells

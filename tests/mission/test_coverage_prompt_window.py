from dataclasses import replace

import numpy as np
import pytest

from src.mission.contracts import (
    FeasibleEdge,
    MissionSelection,
    TaskCandidate,
    UavResource,
)
from src.mission.coverage_policy import CoveragePolicy, build_coverage_constraint
from src.mission.contracts import CoverageConstraint, MissionSnapshot
from src.mission.mission_scheduler import MissionScheduler, SELECTION_SCHEMA


def _task(task_id, kind="search", bbox=None, priority="medium"):
    return TaskCandidate(
        task_id,
        kind,
        bbox,
        None,
        (),
        ("U1",),
        0.0,
        priority,
        1.0,
        1.0,
        0.0,
    )


def _resource(uav_id="U1"):
    return UavResource(
        uav_id, (1.0, 1.0), 0.0, 1.0, 100.0, "idle", None, 0, 0.0,
    )


def _edge(task_id, uav_id="U1"):
    return FeasibleEdge(task_id, uav_id, 1.0, 1.0, 1.0, 0.0, f"{uav_id}:{task_id}")


@pytest.mark.parametrize("capacity", [1, 3])
def test_zero_ordinary_reserve_never_exceeds_prompt_capacity(capacity):
    urgent = tuple(_task(f"P{i}", "probe") for i in range(capacity))
    window = CoveragePolicy(np.ones((4, 4), dtype=bool)).select_window(
        (*urgent, _task("S", bbox=(0, 0, 2, 2))),
        ordinary_reserve=0, capacity=capacity, now_min=0,
    )
    assert window.tasks == urgent
    assert window.representative_task_ids == ()


def test_zero_ordinary_reserve_still_fills_unused_capacity():
    task = _task("S", bbox=(0, 0, 2, 2))
    window = CoveragePolicy(np.ones((4, 4), dtype=bool)).select_window(
        (task,), ordinary_reserve=0, capacity=1, now_min=0,
    )
    assert window.tasks == (task,)


def test_zone_window_preserves_sar_urgency_before_zone_id():
    from src.mission.coverage_zones import ZonePartition

    fixed = np.ones((6, 2), dtype=bool)
    zones = ZonePartition(fixed, 3, 1)
    tasks = tuple(_task(f"S{i}", bbox=(2 * i, 0, 2 * i + 1, 1)) for i in range(3))
    last = np.full(fixed.shape, 120.0)
    last[4, 0] = -np.inf
    last[2, 0] = 0.0
    policy = CoveragePolicy(fixed)
    ranked = policy.rank_search_candidates(
        tasks, now_min=120, last_sar=last,
        estimated_minutes={task.task_id: 1.0 for task in tasks},
    )
    window = policy.select_window(
        ranked, ordinary_reserve=2, capacity=2, now_min=120, zones=zones,
    )
    assert tuple(task.task_id for task in window.tasks) == ("S2", "S1")


def test_window_reserves_ordinary_candidates_and_non_overlapping_representatives():
    tasks = [
        _task(f"P{index}", "probe", priority="high")
        for index in range(45)
    ] + [
        _task(f"S{index}", bbox=(index * 2, 0, index * 2 + 1, 1))
        for index in range(100)
    ]

    window = CoveragePolicy(np.ones((240, 2), dtype=bool)).select_window(
        tasks,
        ordinary_reserve=8,
        capacity=40,
        now_min=0.0,
    )

    assert len(window.tasks) == 40
    assert sum(task.kind == "search" for task in window.tasks) >= 8
    assert len(window.representative_task_ids) == 8
    assert dict(window.sources)


def test_window_fills_remaining_capacity_with_non_overlapping_successors():
    tasks = [
        _task(f"S{index}", bbox=(index * 2, 0, index * 2 + 1, 1))
        for index in range(8)
    ] + [
        _task("overlap", bbox=(0, 0, 1, 1)),
        _task("successor", bbox=(20, 0, 21, 1)),
    ]

    window = CoveragePolicy(np.ones((30, 2), dtype=bool)).select_window(
        tasks,
        ordinary_reserve=8,
        capacity=9,
        now_min=0.0,
    )

    assert "successor" in {task.task_id for task in window.tasks}
    assert "overlap" not in {task.task_id for task in window.tasks}


def test_constraint_uses_maximum_matching_not_minimum_of_counts():
    constraint = build_coverage_constraint(
        healthy_count=5,
        active_search_count=0,
        available_ids=("U1", "U2"),
        representatives=("S1", "S2", "S3"),
        edges=(_edge("S1", "U1"), _edge("S2", "U1"), _edge("S3", "U2")),
        fraction=1.0,
    )

    assert constraint.desired_search_count == 5
    assert constraint.required_new_search_count == 2
    assert constraint.must_service_task_ids == ("S1",)
    assert constraint.infeasible_reason == "insufficient_available_resources"


def test_constraint_anchors_budget_to_healthy_fleet_not_only_idle_uavs():
    constraint = build_coverage_constraint(
        healthy_count=10,
        active_search_count=5,
        available_ids=("U6", "U7", "U8", "U9", "U10"),
        representatives=("S1", "S2", "S3", "S4", "S5"),
        edges=tuple(
            _edge(task_id, uav_id)
            for task_id in ("S1", "S2", "S3", "S4", "S5")
            for uav_id in ("U6", "U7", "U8", "U9", "U10")
        ),
        fraction=0.6,
    )

    assert constraint.desired_search_count == 6
    assert constraint.required_new_search_count == 1
    assert constraint.infeasible_reason is None


def test_constraint_reports_infeasible_floor_without_fabricating_a_slot():
    constraint = build_coverage_constraint(
        healthy_count=10,
        active_search_count=0,
        available_ids=("U1", "U2", "U3", "U4"),
        representatives=("S1", "S2"),
        edges=(_edge("S1", "U1"),),
        fraction=0.4,
    )

    assert constraint.required_new_search_count == 1
    assert constraint.infeasible_reason == "insufficient_available_resources"


@pytest.mark.parametrize(
    ("available_count", "active", "reserved", "pending", "edge_count", "expected"),
    [
        (10, 0, 10, 10, 10, 0),
        (10, 4, 7, 3, 10, 3),
        (4, 4, 6, 2, 4, 0),
        (4, 1, 1, 0, 2, 2),
    ],
)
def test_residual_new_search_budget_accounts_for_pending_capacity(
    available_count, active, reserved, pending, edge_count, expected,
):
    uavs = tuple(f"U{index}" for index in range(available_count))
    task_ids = tuple(f"S{index}" for index in range(edge_count))
    edges = tuple(
        _edge(task_id, uavs[index % len(uavs)])
        for index, task_id in enumerate(task_ids)
    )

    constraint = build_coverage_constraint(
        active_search_count=active,
        reserved_search_count=reserved,
        matchable_pending_count=pending,
        available_ids=uavs,
        representatives=task_ids,
        edges=edges,
        fraction=1.0,
    )

    assert constraint.desired_search_count == available_count
    assert constraint.required_new_search_count == expected
    assert constraint.reserved_search_count == reserved
    assert constraint.matchable_pending_count == pending


def test_unmatchable_pending_geometry_is_reserved_but_does_not_satisfy_capacity():
    constraint = build_coverage_constraint(
        active_search_count=1,
        reserved_search_count=2,
        matchable_pending_count=0,
        available_ids=("U1", "U2"),
        representatives=("S-new",),
        edges=(_edge("S-new", "U1"),),
        fraction=1.0,
    )

    assert constraint.required_new_search_count == 1
    assert constraint.reserved_search_count == 2
    assert constraint.matchable_pending_count == 0


def test_validator_requires_oldest_representative_and_floor():
    tasks = (
        _task("S1", bbox=(1, 1, 2, 2), priority="medium"),
        _task("S2", bbox=(3, 1, 4, 2), priority="medium"),
    )
    tasks = tuple(
        replace(task, feasible_uav_ids=("U1", "U2")) for task in tasks
    )
    resources = (_resource("U1"), _resource("U2"))
    edges = (_edge("S1", "U1"), _edge("S2", "U2"))
    snapshot = MissionSnapshot(
        snapshot_id="snapshot-coverage",
        sim_time_min=10.0,
        candidates=tasks,
        available_uav_ids=("U1", "U2"),
        preemptible_uav_ids=(),
        uav_generations=(("U1", 0), ("U2", 0)),
        resources=resources,
        feasible_edges=edges,
        active_tasks=(),
        contacts=(),
        intents=(),
        intent_statuses=(),
        memory_version="baseline",
        planning_map_version=0,
        reviewer_summary="",
        prompt_task_ids=("S1", "S2"),
        prompt_sources=(("S1", "ordinary"), ("S2", "ordinary")),
        coverage_constraint=CoverageConstraint(
            desired_search_count=2,
            active_search_count=0,
            required_new_search_count=2,
            representative_task_ids=("S1", "S2"),
            must_service_task_ids=("S1",),
        ),
    )
    scheduler = MissionScheduler(selection_provider=lambda _snapshot, _payload: {})
    payload = {
        "schema_version": SELECTION_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": ["S2"],
        "preempt_uav_ids": [],
        "defer_reason": None,
        "notes": "",
    }

    errors = scheduler.validate_selection(payload, snapshot)

    assert "coverage_floor_not_met:2:1" in errors
    assert "coverage_oldest_not_selected" in errors


def test_validator_allows_legal_searches_above_coverage_floor():
    tasks = tuple(
        replace(
            task,
            feasible_uav_ids=("U1", "U2"),
        )
        for task in (
            _task("S1", bbox=(1, 1, 2, 2)),
            _task("S2", bbox=(3, 1, 4, 2)),
        )
    )
    resources = (_resource("U1"), _resource("U2"))
    edges = (_edge("S1", "U1"), _edge("S2", "U2"))
    snapshot = MissionSnapshot(
        snapshot_id="snapshot-coverage-floor",
        sim_time_min=10.0,
        candidates=tasks,
        available_uav_ids=("U1", "U2"),
        preemptible_uav_ids=(),
        uav_generations=(("U1", 0), ("U2", 0)),
        resources=resources,
        feasible_edges=edges,
        active_tasks=(),
        contacts=(),
        intents=(),
        intent_statuses=(),
        memory_version="baseline",
        planning_map_version=0,
        reviewer_summary="",
        prompt_task_ids=("S1", "S2"),
        prompt_sources=(("S1", "ordinary"), ("S2", "ordinary")),
        coverage_constraint=CoverageConstraint(
            desired_search_count=2,
            active_search_count=0,
            required_new_search_count=1,
            representative_task_ids=("S1", "S2"),
            must_service_task_ids=("S1",),
        ),
    )
    scheduler = MissionScheduler(selection_provider=lambda _snapshot, _payload: {})
    payload = {
        "schema_version": SELECTION_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": ["S1", "S2"],
        "preempt_uav_ids": [],
        "defer_reason": None,
        "notes": "",
    }

    assert scheduler.validate_selection(payload, snapshot) == ()


def test_validator_allows_partial_floor_when_snapshot_reports_infeasible_resources():
    tasks = (_task("S1", bbox=(1, 1, 2, 2)), _task("S2", bbox=(3, 1, 4, 2)))
    resources = (_resource("U1"),)
    edges = (_edge("S1", "U1"),)
    snapshot = MissionSnapshot(
        snapshot_id="snapshot-infeasible-coverage",
        sim_time_min=10.0,
        candidates=tasks,
        available_uav_ids=("U1",),
        preemptible_uav_ids=(),
        uav_generations=(("U1", 0),),
        resources=resources,
        feasible_edges=edges,
        active_tasks=(),
        contacts=(),
        intents=(),
        intent_statuses=(),
        memory_version="baseline",
        planning_map_version=0,
        reviewer_summary="",
        prompt_task_ids=("S1", "S2"),
        prompt_sources=(("S1", "ordinary"), ("S2", "ordinary")),
        coverage_constraint=CoverageConstraint(
            desired_search_count=2,
            active_search_count=0,
            required_new_search_count=2,
            representative_task_ids=("S1", "S2"),
            must_service_task_ids=("S1",),
            infeasible_reason="insufficient_available_resources",
        ),
    )
    scheduler = MissionScheduler(selection_provider=lambda _snapshot, _payload: {})
    payload = {
        "schema_version": SELECTION_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": ["S1"],
        "preempt_uav_ids": [],
        "defer_reason": None,
        "notes": "",
    }

    errors = scheduler.validate_selection(payload, snapshot)

    assert errors == ()

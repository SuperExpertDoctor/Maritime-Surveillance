import numpy as np

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
        fraction=0.4,
    )

    assert constraint.desired_search_count == 2
    assert constraint.required_new_search_count == 2
    assert constraint.must_service_task_ids == ("S1",)
    assert constraint.infeasible_reason is None


def test_constraint_reports_infeasible_floor_without_fabricating_a_slot():
    constraint = build_coverage_constraint(
        healthy_count=10,
        active_search_count=0,
        available_ids=("U1",),
        representatives=("S1", "S2"),
        edges=(_edge("S1", "U1"),),
        fraction=0.4,
    )

    assert constraint.required_new_search_count == 4
    assert constraint.infeasible_reason == "insufficient_available_resources"


def test_validator_requires_oldest_representative_and_floor():
    tasks = (_task("S1", bbox=(1, 1, 2, 2)), _task("S2", bbox=(3, 1, 4, 2)))
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

    assert "coverage_floor_not_met:2" in errors
    assert "coverage_oldest_not_selected" in errors


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

from __future__ import annotations

from src.mission.contracts import Assignment, AssignmentBatch
from src.mission.mission_scheduler import MissionScheduler
from tests.mission.test_mission_task_lifecycle import _engine


def pending_search_fixture():
    """Create one partially completed ordinary search in the pending state."""
    engine = _engine()
    snapshot = engine.allocator.build_mission_snapshot(0.0)
    edge = next(
        edge
        for edge in snapshot.feasible_edges
        if next(
            candidate
            for candidate in snapshot.candidates
            if candidate.task_id == edge.task_id
        ).kind == "search"
        and edge.uav_id in snapshot.available_uav_ids
    )
    candidate = next(
        candidate
        for candidate in snapshot.candidates
        if candidate.task_id == edge.task_id
    )
    generation = dict(snapshot.uav_generations)[edge.uav_id]
    batch = AssignmentBatch(
        snapshot.snapshot_id,
        (Assignment(candidate.task_id, edge.uav_id, generation, None),),
        "fixture-search-preemption",
    )
    assert engine.apply_assignment_batch(batch)
    region = next(
        region
        for region in engine.allocator.sm.get_search_regions()
        if region.id == candidate.task_id
    )
    region.completion_pct = 37.5
    task = engine.control_coordinator.active_task(edge.uav_id)
    assert task is not None
    engine._close_mission_task(
        edge.uav_id,
        task,
        status="approved",
        reason="preempted",
        current_time=1.0,
        preserve_search=True,
    )
    return engine, candidate.task_id, edge.uav_id, tuple(candidate.bbox)


def _selection_for(snapshot, task_id: str) -> dict:
    return {
        "schema_version": "mission-selection/v1",
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": [task_id],
        "preempt_uav_ids": [],
        "defer_reason": None,
        "notes": "pending compatibility contract",
    }


def assert_search_projection_consistent(engine) -> None:
    regions = {region.id: region for region in engine.allocator.sm.get_search_regions()}
    for record in engine._mission_task_records.values():
        if record.kind != "search":
            continue
        if record.status == "executing":
            assert record.assigned_uav_id is not None
            assert regions[record.task_id].assigned_uav_id == record.assigned_uav_id
            active = engine.control_coordinator.active_task(record.assigned_uav_id)
            assert active is not None
            assert active.task_id == record.task_id
        if record.status == "approved" and record.assigned_uav_id is None:
            assert regions[record.task_id].status == "active"
            assert regions[record.task_id].assigned_uav_id is None


def test_preempted_ordinary_search_is_pending_not_executing():
    engine, task_id, _uav_id, bbox = pending_search_fixture()
    region = next(
        region
        for region in engine.allocator.sm.get_search_regions()
        if region.id == task_id
    )
    record = engine._mission_task_records[task_id]

    assert (region.status, region.assigned_uav_id, tuple(region.bbox)) == (
        "active",
        None,
        bbox,
    )
    assert (record.status, record.assigned_uav_id) == ("approved", None)
    assert_search_projection_consistent(engine)


def test_pending_geometry_cannot_be_required_and_rejected_in_same_snapshot():
    engine, task_id, _uav_id, _bbox = pending_search_fixture()
    snapshot = engine.allocator.build_mission_snapshot(
        active_tasks=tuple(engine._mission_task_records.values()),
    )
    constraint = snapshot.coverage_constraint
    assert constraint is not None

    required = set(constraint.must_service_task_ids)
    assert task_id not in required
    scheduler = MissionScheduler(selection_provider=lambda *_: None)
    errors_by_candidate = {
        candidate.task_id: scheduler.validate_selection(
            _selection_for(snapshot, candidate.task_id), snapshot,
        )
        for candidate in snapshot.candidates
        if candidate.kind == "search"
    }
    assert not any(
        task_id in required
        and any("overlapping_active_search" in error for error in errors)
        for task_id, errors in errors_by_candidate.items()
    )

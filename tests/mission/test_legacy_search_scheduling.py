from __future__ import annotations

from src.mission.contracts import Assignment, AssignmentBatch, TaskRecord
from src.mission.mission_scheduler import MissionScheduler
from src.schedule.datatypes import BBox, Region
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
    generation = engine.allocator.sm.coverage_task_generation(candidate.task_id)
    assert generation is not None
    progress = engine.allocator.sm.coverage_service.progress(
        candidate.task_id,
        generation,
        uav_id=edge.uav_id,
    )
    assert progress is not None
    engine.allocator.sm.coverage_service.record(
        candidate.task_id,
        generation,
        (progress.required_cells[0],),
        at_min=0.5,
        uav_id=edge.uav_id,
    )
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


def test_pending_search_is_a_reserved_audit_region_not_a_new_llm_candidate():
    engine, task_id, _uav_id, _bbox = pending_search_fixture()
    snapshot = engine.allocator.build_mission_snapshot(
        active_tasks=tuple(engine._mission_task_records.values()),
    )

    assert snapshot.pending_search_task_ids == (task_id,)
    assert task_id in {record.task_id for record in snapshot.active_tasks}
    assert task_id not in {candidate.task_id for candidate in snapshot.candidates}
    assert snapshot.coverage_constraint is not None
    assert snapshot.coverage_constraint.reserved_search_count >= 1
    assert snapshot.coverage_constraint.matchable_pending_count == 1
    assert task_id not in snapshot.coverage_constraint.must_service_task_ids


def _two_pending_searches():
    engine = _engine()
    snapshot = engine.allocator.build_mission_snapshot(0.0)
    selected = []
    used_tasks = set()
    used_uavs = set()
    candidates = {candidate.task_id: candidate for candidate in snapshot.candidates}
    for edge in snapshot.feasible_edges:
        candidate = candidates[edge.task_id]
        if (
            candidate.kind == "search"
            and edge.uav_id in snapshot.available_uav_ids
            and edge.task_id not in used_tasks
            and edge.uav_id not in used_uavs
        ):
            selected.append((candidate, edge))
            used_tasks.add(edge.task_id)
            used_uavs.add(edge.uav_id)
        if len(selected) == 2:
            break
    assert len(selected) == 2
    assignments = tuple(
        Assignment(
            candidate.task_id,
            edge.uav_id,
            dict(snapshot.uav_generations)[edge.uav_id],
            None,
        )
        for candidate, edge in selected
    )
    assert engine.apply_assignment_batch(
        AssignmentBatch(snapshot.snapshot_id, assignments, "fixture-two-searches")
    )
    for assignment in assignments:
        task = engine.control_coordinator.active_task(assignment.uav_id)
        assert task is not None
        engine._close_mission_task(
            assignment.uav_id,
            task,
            status="approved",
            reason="preempted",
            current_time=1.0,
            preserve_search=True,
        )
    return engine, tuple(sorted(used_tasks))


def test_pending_matching_is_max_cardinality_then_min_cost_and_read_only():
    engine, pending_ids = _two_pending_searches()
    before_regions = [
        (region.id, region.assigned_uav_id, region.completion_pct)
        for region in engine.allocator.sm.get_search_regions()
    ]
    before_records = dict(engine._mission_task_records)
    before_tasks = tuple(
        (uav.id, uav.status, uav.assigned_region)
        for uav in engine.uavs
    )

    first = engine.allocator.build_pending_search_batch(
        1.0,
        active_tasks=tuple(engine._mission_task_records.values()),
    )
    second = engine.allocator.build_pending_search_batch(
        1.0,
        active_tasks=tuple(reversed(tuple(engine._mission_task_records.values()))),
    )

    assert first is not None and second is not None
    assert tuple(item.task_id for item in first.assignments) == pending_ids
    assert [
        (item.task_id, item.uav_id, item.expected_generation, item.previous_task_id)
        for item in first.assignments
    ] == [
        (item.task_id, item.uav_id, item.expected_generation, item.previous_task_id)
        for item in second.assignments
    ]
    assert len(first.assignments) == 2
    assert before_regions == [
        (region.id, region.assigned_uav_id, region.completion_pct)
        for region in engine.allocator.sm.get_search_regions()
    ]
    assert before_records == engine._mission_task_records
    assert before_tasks == tuple(
        (uav.id, uav.status, uav.assigned_region)
        for uav in engine.uavs
    )


def test_pending_matching_excludes_urgent_records_and_unavailable_uavs():
    engine, pending_ids = _two_pending_searches()
    urgent_id = "urgent-direction-search"
    engine.allocator.sm.set_search_regions([
        *engine.allocator.sm.get_search_regions(),
        Region(urgent_id, BBox(2, 2, 4, 4), "search"),
    ])
    engine._mission_task_records[urgent_id] = TaskRecord(
        urgent_id,
        "direction_search",
        "approved",
        (2, 2, 4, 4),
        None,
        (),
        None,
        "fixture",
        0.0,
        None,
        None,
        None,
    )
    for uav_id in ("UAV-1", "UAV-2"):
        engine.allocator.sm.update_uav_status(
            uav_id,
            "returning" if uav_id == "UAV-1" else "refueling",
            engine.allocator.sm.get_uav(uav_id).position,
        )

    batch = engine.allocator.build_pending_search_batch(
        1.0,
        active_tasks=tuple(engine._mission_task_records.values()),
    )

    assert batch is not None
    assert tuple(item.task_id for item in batch.assignments) == pending_ids
    assert urgent_id not in {item.task_id for item in batch.assignments}
    assert {item.uav_id for item in batch.assignments}.isdisjoint({"UAV-1", "UAV-2"})


def test_unmatchable_pending_geometry_remains_pending():
    engine, task_id, _uav_id, bbox = pending_search_fixture()
    for uav in engine.uavs:
        if uav.id != "UAV-10":
            engine.allocator.sm.update_uav_status(
                uav.id,
                "returning",
                uav.position,
            )
    batch = engine.allocator.build_pending_search_batch(
        1.0,
        active_tasks=tuple(engine._mission_task_records.values()),
    )

    assert batch is None
    region = next(
        region
        for region in engine.allocator.sm.get_search_regions()
        if region.id == task_id
    )
    assert (region.status, region.assigned_uav_id, tuple(region.bbox)) == (
        "active",
        None,
        bbox,
    )


def test_pending_reassignment_preserves_region_and_sar_progress():
    engine, task_id, old_uav_id, bbox = pending_search_fixture()
    old_generation = engine.allocator.sm.coverage_task_generation(task_id)
    assert old_generation is not None
    progress = engine.allocator.sm.coverage_service.progress(task_id, old_generation)
    assert progress is not None
    preserved_cell = progress.scanned_cells[0]

    reassigned = engine._apply_pending_search_reassignments(1.0)

    assert reassigned == 1
    record = engine._mission_task_records[task_id]
    assert record.status == "executing"
    assert record.assigned_uav_id is not None
    assert record.assigned_uav_id != old_uav_id
    region = next(
        region
        for region in engine.allocator.sm.get_search_regions()
        if region.id == task_id
    )
    assert (region.id, tuple(region.bbox), region.completion_pct) == (
        task_id,
        bbox,
        37.5,
    )
    assert region.assigned_uav_id == record.assigned_uav_id
    active = engine.control_coordinator.active_task(record.assigned_uav_id)
    assert active is not None and active.task_id == task_id
    new_generation = engine.allocator.sm.coverage_task_generation(task_id)
    assert new_generation is not None
    assert new_generation == engine.control_coordinator.current_lease(
        record.assigned_uav_id
    ).generation
    new_progress = engine.allocator.sm.coverage_service.progress(
        task_id,
        new_generation,
        uav_id=record.assigned_uav_id,
    )
    assert new_progress is not None
    assert preserved_cell in new_progress.scanned_cells
    assert len([
        item for item in engine.allocator.sm.get_search_regions()
        if item.id == task_id
    ]) == 1


def test_pending_reassignment_route_failure_keeps_pending_state(monkeypatch):
    engine, task_id, _old_uav_id, _bbox = pending_search_fixture()
    before_record = engine._mission_task_records[task_id]
    before_regions = tuple(engine.allocator.sm.get_search_regions())

    def fail_route(*_args, **_kwargs):
        raise RuntimeError("forced pending route failure")

    monkeypatch.setattr("src.env.simulation.plan_search_route", fail_route)

    assert engine._apply_pending_search_reassignments(1.0) == 0
    assert engine._mission_task_records[task_id] == before_record
    assert engine.allocator.sm.get_search_regions() == list(before_regions)
    assert all(
        engine.control_coordinator.active_task(uav.id) is None
        for uav in engine.uavs
        if uav.id in {"UAV-1", "UAV-2"}
    )


def test_pending_reassignment_coordinator_failure_keeps_pending_state(monkeypatch):
    engine, task_id, _old_uav_id, _bbox = pending_search_fixture()
    before_record = engine._mission_task_records[task_id]
    before_regions = tuple(engine.allocator.sm.get_search_regions())

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("forced pending coordinator failure")

    monkeypatch.setattr(
        engine.control_coordinator,
        "assign_tasks_atomically",
        fail_commit,
    )

    assert engine._apply_pending_search_reassignments(1.0) == 0
    assert engine._mission_task_records[task_id] == before_record
    assert engine.allocator.sm.get_search_regions() == list(before_regions)
    assert all(
        engine.control_coordinator.active_task(uav.id) is None
        for uav in engine.uavs
        if uav.id in {"UAV-1", "UAV-2"}
    )

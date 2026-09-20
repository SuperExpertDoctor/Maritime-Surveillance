from __future__ import annotations

import pytest

from src.control.common.contracts import ControlEvent, ControlTask, OperationMode
from src.env.simulation import SimulationEngine
from src.mission.contracts import Assignment, AssignmentBatch, TaskRecord
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord, Region
from tests.mission.replay_restoration_helpers import probe_batch


class OfflineGateway:
    def request_json(self, **_kwargs):
        return ModelResult(
            call_id="offline",
            success=False,
            payload=None,
            errors=("offline",),
            failure_category="transport",
        )

    def request_text(self, **_kwargs):
        return ModelResult(
            call_id="offline-text",
            success=False,
            payload=None,
            errors=("offline",),
            failure_category="transport",
        )


def _engine() -> SimulationEngine:
    return SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=OfflineGateway())


def assert_owned_records(engine: SimulationEngine) -> None:
    for record in engine._mission_task_records.values():
        if record.status not in {"approved", "executing"} or record.assigned_uav_id is None:
            continue
        task = engine.control_coordinator.active_task(record.assigned_uav_id)
        assert task is not None
        assert task.task_id == record.task_id
        entity = next(uav for uav in engine.uavs if uav.id == record.assigned_uav_id)
        assert entity.status not in {"holding", "returning", "refueling"}


def _coverage_task_fixture() -> tuple[SimulationEngine, object, ControlTask]:
    engine = _engine()
    fixed = engine._intent_searchable_mask()
    bbox = None
    cols, rows = fixed.shape
    for col in range(cols - 1):
        for row in range(rows):
            if fixed[col, row] and fixed[col + 1, row]:
                bbox = BBox(col, row, col + 2, row + 1)
                break
        if bbox is not None:
            break
    assert bbox is not None
    uav = engine.uavs[0]
    task = ControlTask("coverage-fixture", OperationMode.COVERAGE, bbox)
    lease = engine.control_coordinator.start_work(
        uav.id,
        sortie_number=1,
        current_time=0.0,
        dt_min=1.0,
        task=task,
    )
    engine._coordinator_tasks[uav.id] = task
    engine.allocator.sm.set_search_regions([
        Region(task.task_id, bbox, "search", assigned_uav_id=uav.id),
    ])
    engine._mission_task_records[task.task_id] = TaskRecord(
        task.task_id,
        "search",
        "approved",
        tuple(bbox),
        None,
        (),
        uav.id,
        "fixture",
        0.0,
        None,
        None,
        None,
    )
    engine._start_coverage_service_task(
        uav, task, 0.0, generation=lease.generation,
    )
    return engine, uav, task


def _finish_route(engine, uav, task, generation: int) -> None:
    event = ControlEvent(
        1,
        engine.clock.time + 1.0,
        "coverage_route_finished",
        "controller",
        uav.id,
        {"task_id": task.task_id, "generation": generation},
    )
    engine._queue_pending_coverage_completion(uav, event, task, generation)
    engine._finalize_coverage_completions(engine.clock.time + 1.0)


def test_holding_closes_old_record_before_control_switch():
    engine = _engine()
    batch = probe_batch(engine, count=1)
    assert engine.apply_assignment_batch(batch)
    assignment = batch.assignments[0]
    task = engine.control_coordinator.active_task(assignment.uav_id)
    assert task is not None
    record = engine._mission_task_records[task.task_id]

    uav = next(uav for uav in engine.uavs if uav.id == assignment.uav_id)
    engine._promote_work_controller_to_holding(uav, 1.0)

    closed = engine._mission_task_records[task.task_id]
    assert closed.status == "blocked"
    assert closed.assigned_uav_id is None
    assert closed.finished_at_min == 1.0
    assert closed.release_reason == "holding"
    assert engine.control_coordinator.active_task(assignment.uav_id).task_type is OperationMode.HOLDING
    assert engine.allocator.sm.get_probe_session(task.probe_id) is None
    assert engine.allocator.sm.contacts.snapshot(task.target_contact_id).assigned_uav_id is None
    assert record.assigned_uav_id == assignment.uav_id
    assert_owned_records(engine)


def test_close_mission_task_is_idempotent_and_does_not_close_replacement():
    engine = _engine()
    batch = probe_batch(engine, count=1)
    assert engine.apply_assignment_batch(batch)
    assignment = batch.assignments[0]
    task = engine.control_coordinator.active_task(assignment.uav_id)
    assert task is not None

    engine._close_mission_task(
        assignment.uav_id,
        task,
        status="blocked",
        reason="probe_blocked",
        current_time=1.0,
    )
    first_events = engine.allocator.sm.get_recent_events(0.0)
    engine._close_mission_task(
        assignment.uav_id,
        task,
        status="blocked",
        reason="probe_blocked",
        current_time=2.0,
    )

    assert engine._mission_task_records[task.task_id].finished_at_min == 1.0
    assert engine.allocator.sm.get_recent_events(0.0) == first_events
    assert engine.allocator.sm.contacts.snapshot(task.target_contact_id).assigned_uav_id is None

    uav = next(uav for uav in engine.uavs if uav.id == assignment.uav_id)
    replacement = engine._promote_work_controller_to_holding(uav, 3.0)
    assert replacement is None
    assert engine.control_coordinator.active_task(assignment.uav_id).task_type is OperationMode.HOLDING


def test_task_sar_completion_uses_the_final_footprint_and_enters_holding_next_tick():
    engine, uav, task = _coverage_task_fixture()
    generation = engine.control_coordinator.current_lease(uav.id).generation
    progress = engine.allocator.sm.coverage_service.progress(task.task_id, generation)
    assert progress is not None

    engine.allocator.sm.coverage_service.record(
        task.task_id,
        generation,
        progress.required_cells,
        at_min=1.0,
    )
    _finish_route(engine, uav, task, generation)

    assert engine._mission_task_records[task.task_id].status == "completed"
    region = engine.allocator.sm.get_search_regions()[0]
    assert region.completion_pct == 100.0
    assert region.completion_basis == "task_sar"
    assert uav.completed_searches_since_refuel == 1

    engine.control_coordinator.step_uav(uav, current_time=2.0, dt_min=1.0)
    assert engine.control_coordinator.active_task(uav.id).task_type is OperationMode.HOLDING


def test_incomplete_route_remains_pending_with_missing_cells_and_never_counted_complete():
    engine, uav, task = _coverage_task_fixture()
    generation = engine.control_coordinator.current_lease(uav.id).generation
    progress = engine.allocator.sm.coverage_service.progress(task.task_id, generation)
    assert progress is not None
    engine.allocator.sm.coverage_service.record(
        task.task_id,
        generation,
        progress.required_cells[:1],
        at_min=1.0,
    )

    _finish_route(engine, uav, task, generation)

    record = engine._mission_task_records[task.task_id]
    assert record.status == "approved"
    assert record.assigned_uav_id is None
    assert record.finished_at_min is None
    assert record.release_reason == "coverage_incomplete"
    assert uav.completed_searches_since_refuel == 0
    region = engine.allocator.sm.get_search_regions()[0]
    assert region.status == "active"
    assert region.assigned_uav_id is None
    assert region.completion_basis == "task_sar"
    assert region.completion_pct < 100.0
    incomplete = next(
        event for event in engine.allocator.sm.get_recent_events(0.0)
        if event["type"] == "coverage_incomplete"
    )
    assert incomplete["data"]["missing_cells"]


def test_search_projection_rejects_illegal_pending_assignee():
    engine, uav, task = _coverage_task_fixture()

    with pytest.raises(ValueError, match="pending search must be unassigned"):
        engine._set_search_task_projection(
            task.task_id,
            state="pending",
            uav_id=uav.id,
            current_time=1.0,
            reason="test",
        )


def test_search_state_invariant_detects_region_record_mismatch():
    engine, uav, task = _coverage_task_fixture()
    region = engine.allocator.sm.get_search_regions()[0]
    region.assigned_uav_id = "UAV-2"

    with pytest.raises(ValueError, match="region_record_assignee_mismatch"):
        engine._validate_mission_state_invariants(strict=True)


def test_production_invariant_failure_pauses_once_and_emits_structured_event():
    engine, _uav, _task = _coverage_task_fixture()
    engine.allocator.sm.get_search_regions()[0].assigned_uav_id = "UAV-2"

    first = engine._validate_mission_state_invariants()
    second = engine._validate_mission_state_invariants()

    assert first == second
    assert engine.runtime_status == "paused_safety"
    assert engine.blocked_role == "mission_state_invariant"
    failures = [
        event for event in engine.allocator.sm.get_recent_events(0.0)
        if event["type"] == "mission_state_invariant_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["data"]["violations"]


def test_completion_without_generation_cannot_complete_current_task():
    engine, uav, task = _coverage_task_fixture()
    generation = engine.control_coordinator.current_lease(uav.id).generation
    event = ControlEvent(
        1,
        1.0,
        "coverage_route_finished",
        "controller",
        uav.id,
        {"task_id": task.task_id},
    )

    engine._queue_pending_coverage_completion(uav, event, task, generation)
    engine._finalize_coverage_completions(1.0)

    assert engine._mission_task_records[task.task_id].status == "approved"
    assert not any(
        item["task_id"] == task.task_id
        for item in engine._pending_coverage_completions
    )


def test_state_step_does_not_replace_task_sar_progress_with_legacy_scan_times():
    engine, _uav, task = _coverage_task_fixture()
    generation = engine.control_coordinator.current_lease("UAV-1").generation
    progress = engine.allocator.sm.coverage_service.progress(task.task_id, generation)
    assert progress is not None
    engine.allocator.sm.coverage_service.record(
        task.task_id,
        generation,
        progress.required_cells[:1],
        at_min=1.0,
    )
    for cell in progress.required_cells:
        engine.allocator.sm.scan_cell(
            GridCoord(*cell),
            1.0,
        )

    engine.allocator.sm.step(1.0)

    region = engine.allocator.sm.get_search_regions()[0]
    assert region.completion_basis == "task_sar"
    assert region.completion_pct == 50.0


def test_search_preemption_keeps_region_and_completion_for_reassignment():
    engine = _engine()
    snapshot = engine.allocator.build_mission_snapshot(0.0)
    edge = next(
        edge for edge in snapshot.feasible_edges
        if next(
            item for item in snapshot.candidates
            if item.task_id == edge.task_id
        ).kind == "search"
        and edge.uav_id in snapshot.available_uav_ids
    )
    candidate = next(item for item in snapshot.candidates if item.task_id == edge.task_id)
    generation = dict(snapshot.uav_generations)[edge.uav_id]
    batch = AssignmentBatch(
        snapshot.snapshot_id,
        (Assignment(candidate.task_id, edge.uav_id, generation, None),),
        "fixture-search-preemption",
    )
    assert engine.apply_assignment_batch(batch)
    region = next(
        region for region in engine.allocator.sm.get_search_regions()
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

    record = engine._mission_task_records[task.task_id]
    assert record.status == "approved"
    assert record.assigned_uav_id is None
    assert record.finished_at_min is None
    assert region.status == "active"
    assert region.assigned_uav_id is None
    assert region.completion_pct == 37.5
    assert_owned_records(engine)

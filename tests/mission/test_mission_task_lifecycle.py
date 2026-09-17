from __future__ import annotations

from src.control.common.contracts import OperationMode
from src.env.simulation import SimulationEngine
from src.mission.contracts import Assignment, AssignmentBatch
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
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

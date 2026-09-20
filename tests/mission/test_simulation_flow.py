from __future__ import annotations

from dataclasses import replace

from src.env.simulation import SimulationEngine
from src.mission.contracts import Assignment, AssignmentBatch
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.trigger_manager import TriggerDecision
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


def _probe_batch(engine: SimulationEngine) -> tuple[AssignmentBatch, str]:
    snapshot = engine.allocator.build_mission_snapshot(0.0)
    candidate = next(item for item in snapshot.candidates if item.kind == "probe")
    edge = next(item for item in snapshot.feasible_edges if item.task_id == candidate.task_id)
    batch = AssignmentBatch(
        snapshot.snapshot_id,
        (
            Assignment(
                candidate.task_id,
                edge.uav_id,
                next(
                    generation
                    for uav_id, generation in snapshot.uav_generations
                    if uav_id == edge.uav_id
                ),
                None,
            ),
        ),
        "fixture-selection",
    )
    return batch, candidate.contact_id


def test_assignment_batch_starts_probe_and_reserves_contact():
    engine = _engine()
    batch, contact_id = _probe_batch(engine)

    assert engine.apply_assignment_batch(batch)

    assignment = batch.assignments[0]
    lease = engine.control_coordinator.current_lease(assignment.uav_id)
    task = engine.control_coordinator.active_task(assignment.uav_id)
    contact = engine.allocator.sm.contacts.snapshot(contact_id)

    assert lease.owner.value == "heuristic"
    assert lease.generation == assignment.expected_generation + 1
    assert task is not None
    assert task.task_id == assignment.task_id
    assert task.probe_id is not None
    assert contact.assigned_uav_id == assignment.uav_id
    assert contact.active_probe_id == task.probe_id
    assert engine.allocator.sm.get_probe_session(task.probe_id) is not None


def test_batch_probe_ids_are_unique_and_sessions_match_tasks():
    engine = _engine()
    batch = probe_batch(engine)

    assert engine.apply_assignment_batch(batch)

    tasks = [
        engine.control_coordinator.active_task(assignment.uav_id)
        for assignment in batch.assignments
    ]
    assert len({task.probe_id for task in tasks}) == 6
    sessions = {
        probe.probe_id: probe
        for probe in engine.allocator.sm.get_probe_sessions()
    }
    assert len(sessions) == 6
    for assignment, task in zip(batch.assignments, tasks):
        probe = sessions[task.probe_id]
        assert (probe.uav_id, probe.contact_id) == (
            assignment.uav_id,
            task.target_contact_id,
        )
        contact = engine.allocator.sm.contacts.snapshot(task.target_contact_id)
        assert (contact.assigned_uav_id, contact.active_probe_id) == (
            assignment.uav_id,
            task.probe_id,
        )


def test_probe_ids_are_unique_across_batches_in_one_episode():
    engine = _engine()
    first = probe_batch(engine, count=3)

    assert engine.apply_assignment_batch(first)
    second = probe_batch(engine, count=3)
    assert engine.apply_assignment_batch(second)

    tasks = [
        engine.control_coordinator.active_task(assignment.uav_id)
        for assignment in (*first.assignments, *second.assignments)
    ]
    assert len({task.probe_id for task in tasks}) == 6
    assert engine._next_probe_number == 7


def test_probe_redispatch_preserves_existing_session_phase():
    engine = _engine()
    first = probe_batch(engine, count=1)
    assert engine.apply_assignment_batch(first)
    assignment = first.assignments[0]
    task = engine.control_coordinator.active_task(assignment.uav_id)
    original = engine.allocator.sm.get_probe_session(task.probe_id)
    advanced = replace(
        original,
        phase="near",
        baseline_started_at_min=1.0,
        near_sample_ids=("EO-1",),
        close_exposure_min=2.0,
    )
    engine.allocator.sm.set_probe_session(advanced)

    record = engine._mission_task_records[task.task_id]
    snapshot = engine.allocator.build_mission_snapshot(
        engine.clock.time,
        active_tasks=(record,),
    )
    generation = dict(snapshot.uav_generations)[assignment.uav_id]
    redispatch = AssignmentBatch(
        snapshot.snapshot_id,
        (
            Assignment(
                task.task_id,
                assignment.uav_id,
                generation,
                task.task_id,
            ),
        ),
        "fixture-redispatch",
    )

    assert engine.apply_assignment_batch(redispatch)
    assert engine.allocator.sm.get_probe_session(task.probe_id) == advanced
    assert engine._next_probe_number == 2


def test_failed_assignment_batch_restores_contact_state_and_probe_counter(monkeypatch):
    engine = _engine()
    batch = probe_batch(engine, count=2)
    snapshot = engine.allocator.last_mission_snapshot
    contact_ids = [
        next(
            candidate.contact_id
            for candidate in snapshot.candidates
            if candidate.task_id == assignment.task_id
        )
        for assignment in batch.assignments
    ]
    before_contacts = {
        contact_id: engine.allocator.sm.contacts.snapshot(contact_id)
        for contact_id in contact_ids
    }
    before_events = engine.allocator.sm.contacts.events
    before_leases = {
        assignment.uav_id: engine.control_coordinator.current_lease(assignment.uav_id)
        for assignment in batch.assignments
    }

    def fail_before_install(*_args, **_kwargs):
        raise RuntimeError("forced pre-commit failure")

    monkeypatch.setattr(
        engine.control_coordinator,
        "assign_tasks_atomically",
        fail_before_install,
    )

    assert not engine.apply_assignment_batch(batch)
    assert engine._next_probe_number == 1
    assert engine.allocator.sm.contacts.events == before_events
    assert {
        contact_id: engine.allocator.sm.contacts.snapshot(contact_id)
        for contact_id in contact_ids
    } == before_contacts
    assert {
        assignment.uav_id: engine.control_coordinator.current_lease(assignment.uav_id)
        for assignment in batch.assignments
    } == before_leases
    assert engine.allocator.sm.get_probe_sessions() == ()
    assert engine._mission_task_records == {}
    assert not any(
        event["type"] == "mission_assignment_committed"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )


def test_invalid_second_assignment_does_not_consume_probe_id_or_reserve_first():
    engine = _engine()
    valid = probe_batch(engine, count=2)
    invalid = Assignment("missing-task", "UAV-2", 0, None)
    batch = AssignmentBatch(
        valid.snapshot_id,
        (valid.assignments[0], invalid),
        "fixture-invalid-second",
    )
    contact_id = next(
        candidate.contact_id
        for candidate in engine.allocator.last_mission_snapshot.candidates
        if candidate.task_id == valid.assignments[0].task_id
    )

    assert not engine.apply_assignment_batch(batch)
    assert engine._next_probe_number == 1
    assert engine.allocator.sm.get_probe_sessions() == ()
    contact = engine.allocator.sm.contacts.snapshot(contact_id)
    assert contact.assigned_uav_id is None
    assert contact.active_probe_id is None


def test_expired_handoff_is_rejected_before_assignment_installation():
    engine = _engine()
    batch, contact_id = _probe_batch(engine)
    assignment = batch.assignments[0]
    engine.handoff_manager.require(
        contact_id,
        source_uav_id="UAV-source",
        required_at_min=0.0,
        last_position=(10.0, 10.0),
    )
    engine.clock.time = 6.0
    before_lease = engine.control_coordinator.current_lease(assignment.uav_id)

    assert not engine.apply_assignment_batch(batch)

    assert engine.control_coordinator.current_lease(assignment.uav_id) == before_lease
    assert engine.control_coordinator.active_task(assignment.uav_id) is None
    contact = engine.allocator.sm.contacts.snapshot(contact_id)
    assert contact.assigned_uav_id is None
    assert engine.handoff_manager.attempts()[0].state == "required"


def test_reset_clears_probe_sessions_and_restarts_episode_counter():
    engine = _engine()
    batch, _ = _probe_batch(engine)
    assert engine.apply_assignment_batch(batch)
    assert engine.allocator.sm.get_probe_sessions()
    assert engine._next_probe_number == 2
    previous_episode = engine.episode_id

    engine.reset(seed=43)

    assert engine.episode_id != previous_episode
    assert engine.allocator.sm.get_probe_sessions() == ()
    assert engine._next_probe_number == 1


def test_assignment_batch_rejects_reused_snapshot_without_second_mutation():
    engine = _engine()
    batch, _ = _probe_batch(engine)
    assert engine.apply_assignment_batch(batch)
    assignment = batch.assignments[0]
    before = engine.control_coordinator.current_lease(assignment.uav_id)
    before_task = engine.control_coordinator.active_task(assignment.uav_id)

    assert not engine.apply_assignment_batch(batch)

    assert engine.control_coordinator.current_lease(assignment.uav_id) == before
    assert engine.control_coordinator.active_task(assignment.uav_id) == before_task


def test_decision_maker_failure_preserves_existing_task():
    engine = _engine()
    batch, _ = _probe_batch(engine)
    assert engine.apply_assignment_batch(batch)
    assignment = batch.assignments[0]
    before = engine.control_coordinator.current_lease(assignment.uav_id)
    before_task = engine.control_coordinator.active_task(assignment.uav_id)
    engine.red_commander.decide = lambda _snapshot: None
    engine.allocator.trigger_manager.check = lambda _time: TriggerDecision("heavy")

    result, returned_batch = engine.allocator.mission_step(
        1.0,
    )

    assert returned_batch is None
    assert result["action"] == "mission_selection_unavailable"
    assert engine.control_coordinator.current_lease(assignment.uav_id) == before
    assert engine.control_coordinator.active_task(assignment.uav_id) == before_task


def test_failed_mission_decision_emits_failure_and_retries_after_one_minute():
    engine = _engine()

    result, returned_batch = engine.allocator.mission_step(1.0)

    assert returned_batch is None
    assert result["action"] == "mission_selection_unavailable"
    trace = result["llm_cycle"]
    assert {
        "system_prompt", "user_prompt", "response", "attempts",
        "validation", "information_version", "trigger_reason",
        "prompt_candidate_ids", "timing",
    } <= set(trace)
    assert trace["success"] is False
    assert trace["validation"]["errors"] == ["model_selection_unavailable"]
    assert trace["user_prompt"]
    assert trace["timing"]["total_seconds"] >= 0.0
    failure = next(
        event
        for event in engine.allocator.sm.get_recent_events(1.0)
        if event["type"] == "decision_failed"
    )
    assert failure["data"]["reason"] == "model_selection_unavailable"
    assert failure["data"]["retry_at_min"] == 2.0
    selection_failure = next(
        event
        for event in engine.allocator.sm.get_recent_events(1.0)
        if event["type"] == "mission_selection_failed"
    )
    assert {
        "snapshot_id", "failure_category", "error_codes", "available_count",
    } <= set(selection_failure["data"])
    assert selection_failure["data"]["snapshot_id"] == result["snapshot_id"]
    assert selection_failure["data"]["failure_category"] == "transport"
    assert selection_failure["data"]["error_codes"] == ["model_selection_unavailable"]
    assert "LONGCAT_API_KEY" not in str(selection_failure)
    assert engine.allocator.trigger_manager.check(1.99).trigger_type == "none"
    assert engine.allocator.trigger_manager.check(2.0).trigger_type == "heavy"

from __future__ import annotations

from src.env.simulation import SimulationEngine
from src.mission.contracts import Assignment, AssignmentBatch
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.trigger_manager import TriggerDecision


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
    assert engine.allocator.trigger_manager.check(1.99).trigger_type == "none"
    assert engine.allocator.trigger_manager.check(2.0).trigger_type == "heavy"

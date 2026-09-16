import pytest

from src.mission.handoff import HandoffManager


@pytest.fixture
def system():
    manager = HandoffManager()
    manager.register_contact("C1", status="observing")
    return manager


def test_returning_observer_keeps_contact_task_incomplete(system):
    handoff = system.require(
        "C1",
        source_uav_id="U1",
        required_at_min=10.0,
        last_position=(10.0, 10.0),
        velocity_cells_min=(0.5, 0.0),
        last_observed_at_min=8.0,
    )

    assert system.task("C1").status == "handoff_required"
    assert system.task("C1").completed_at_min is None
    assert handoff.state == "required"


def test_handoff_center_uses_contact_projection_not_uav_or_base(system):
    handoff = system.require(
        "C1",
        source_uav_id="U1",
        required_at_min=10.0,
        last_position=(10.0, 10.0),
        velocity_cells_min=(0.5, 0.0),
        last_observed_at_min=8.0,
    )

    evidence = system.evidence(handoff.handoff_id)
    assert evidence.mean == pytest.approx((11.0, 10.0))
    assert evidence.mean != (2.0, 2.0)


def test_assignment_then_eo_lock_is_required_for_success(system):
    handoff = system.require(
        "C1", source_uav_id="U1", required_at_min=10.0,
        last_position=(10.0, 10.0), velocity_cells_min=(0.0, 0.0),
        last_observed_at_min=10.0,
    )
    pending = system.commit_assignment(handoff.handoff_id, "U2", 12.0)
    assert pending.state == "pending"
    assert system.task("C1").status == "handoff_pending"
    succeeded = system.acquire_eo_lock(handoff.handoff_id, "U2", 13.0)
    assert succeeded.state == "succeeded"
    assert system.task("C1").status == "observing"
    assert system.task("C1").completed_at_min is None


def test_repeated_warning_is_idempotent_and_deadlines_fail_attempt(system):
    first = system.require(
        "C1", source_uav_id="U1", required_at_min=10.0,
        last_position=(10.0, 10.0), velocity_cells_min=(0.0, 0.0),
        last_observed_at_min=10.0,
    )
    repeated = system.require(
        "C1", source_uav_id="U1", required_at_min=11.0,
        last_position=(10.0, 10.0), velocity_cells_min=(0.0, 0.0),
        last_observed_at_min=11.0,
    )
    assert repeated.handoff_id == first.handoff_id
    assert len(system.attempts()) == 1

    system.commit_assignment(first.handoff_id, "U2", 12.0)
    failed = system.advance(23.0)[0]
    assert failed.state == "failed"
    assert system.task("C1").status == "handoff_required"
    retry = system.require(
        "C1", source_uav_id="U1", required_at_min=24.0,
        last_position=(11.0, 10.0), velocity_cells_min=(0.0, 0.0),
        last_observed_at_min=24.0,
    )
    assert retry.handoff_id != first.handoff_id


def test_bearing_only_handoff_does_not_fabricate_a_point(system):
    handoff = system.require(
        "C1", source_uav_id="U1", required_at_min=5.0,
        bearing=(2.0, 1.0), last_observed_at_min=5.0,
    )
    evidence = system.evidence(handoff.handoff_id)
    assert evidence.mean is None
    assert evidence.direction == pytest.approx((2.0, 1.0))


def test_fail_for_contact_only_fails_live_attempts_and_keeps_evidence(system):
    first = system.require(
        "C1", source_uav_id="U1", required_at_min=10.0,
        last_position=(10.0, 10.0), last_observed_at_min=10.0,
    )
    system.commit_assignment(first.handoff_id, "U2", 11.0)
    succeeded = system.require(
        "C1", source_uav_id="U1", required_at_min=12.0,
        last_position=(11.0, 10.0), last_observed_at_min=12.0,
    )
    assert succeeded.handoff_id == first.handoff_id
    evidence = system.evidence(first.handoff_id)

    failed = system.fail_for_contact("C1", 13.0, "vessel_removed")

    assert [attempt.handoff_id for attempt in failed] == [first.handoff_id]
    assert system.attempts()[0].state == "failed"
    assert system.attempts()[0].failure_reason == "vessel_removed"
    assert system.evidence(first.handoff_id) == evidence

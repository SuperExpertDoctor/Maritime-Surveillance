from src.control.common.contracts import ControlOwner
import pytest

from src.control.common.ownership import ControlOwnership, ControlOwnershipError


def test_replacing_heuristic_task_invalidates_old_lease():
    ownership = ControlOwnership(["UAV-1"])
    first = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 1.0)
    second = ownership.replace(first, ControlOwner.HEURISTIC, "tracking:G1", 2.0)

    assert second.generation == first.generation + 1
    assert not ownership.accepts(first)
    assert ownership.accepts(second)


def test_learning_task_events_do_not_replace_sortie_lease():
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire("UAV-1", ControlOwner.LEARNING, "rl:UAV-1", 1.0)

    assert ownership.current("UAV-1") == lease
    assert ownership.accepts(lease)


def test_acquire_rejects_active_lease_with_system_ownership_details():
    ownership = ControlOwnership(["UAV-1"])
    active = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 1.0)

    with pytest.raises(ControlOwnershipError) as raised:
        ownership.acquire("UAV-1", ControlOwner.LEARNING, "rl:UAV-1", 2.0)

    error = raised.value
    assert error.uav_id == "UAV-1"
    assert error.current_owner is ControlOwner.HEURISTIC
    assert error.current_generation == active.generation
    assert "SYSTEM ownership required" in str(error)
    assert "UAV-1" in str(error)
    assert "heuristic" in str(error)
    assert f"generation {active.generation}" in str(error)


def test_release_rejects_stale_generation_with_details():
    ownership = ControlOwnership(["UAV-1"])
    first = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 1.0)
    current = ownership.replace(first, ControlOwner.HEURISTIC, "tracking:G1", 2.0)

    with pytest.raises(ControlOwnershipError) as raised:
        ownership.release_to_system(first, 3.0)

    error = raised.value
    assert error.uav_id == "UAV-1"
    assert error.expected_generation == first.generation
    assert error.actual_generation == current.generation
    assert f"expected generation {first.generation}" in str(error)
    assert f"current generation {current.generation}" in str(error)


def test_release_requires_current_generation():
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 1.0)
    ownership.release_to_system(lease, 10.0)

    assert ownership.current("UAV-1").owner is ControlOwner.SYSTEM

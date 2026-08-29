from src.control.common.contracts import ControlOwner
from src.control.common.ownership import ControlOwnership


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


def test_release_requires_current_generation():
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 1.0)
    ownership.release_to_system(lease, 10.0)

    assert ownership.current("UAV-1").owner is ControlOwner.SYSTEM

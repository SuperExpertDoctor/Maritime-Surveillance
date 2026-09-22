import pytest

from src.mission.contracts import VesselCommand
from src.mission.vessel_commands import (
    CommandConflict,
    VesselCommandQueue,
)


def _create(command_id="v-1"):
    return VesselCommand(
        command_id=command_id,
        episode_id="episode-1",
        operation="create",
        vessel_id=None,
        expected_revision=None,
        vessel_class="type_ii",
        position_cells=(12.5, 8.5),
    )


def test_create_command_is_idempotent_until_simulation_thread_drains():
    queue = VesselCommandQueue(maxsize=2)
    first = queue.enqueue(_create())
    repeat = queue.enqueue(_create())

    assert first == repeat
    assert len(queue.pending()) == 1
    assert queue.drain() == (_create(),)


def test_command_id_reuse_with_changed_payload_is_rejected():
    queue = VesselCommandQueue(maxsize=2)
    queue.enqueue(_create())
    changed = VesselCommand(
        "v-1", "episode-1", "create", None, None, "type_i", (12.5, 8.5)
    )
    with pytest.raises(CommandConflict):
        queue.enqueue(changed)


def test_delete_uses_revision_cas_and_queue_is_bounded():
    queue = VesselCommandQueue(maxsize=1)
    delete = VesselCommand("d-1", "episode-1", "delete", "scenario-vessel-1", 2, None, None)
    queue.enqueue(delete)
    with pytest.raises(RuntimeError):
        queue.enqueue(_create("v-2"))


def test_set_ais_requires_only_id_revision_and_boolean():
    command = VesselCommand(
        "a-1", "episode-1", "set_ais", "Ship-2", 4, None, None, False,
    )
    assert VesselCommandQueue().enqueue(command).status == "queued"


@pytest.mark.parametrize("field,value", [
    ("command_id", 123), ("command_id", " "), ("episode_id", True),
    ("position_cells", "12"), ("position_cells", {1: 2, 3: 4}),
    ("position_cells", (10 ** 400, 1)),
])
def test_malformed_commands_are_rejected_before_queueing(field, value):
    from dataclasses import replace
    queue = VesselCommandQueue()
    with pytest.raises(ValueError):
        queue.enqueue(replace(_create(), **{field: value}))
    assert queue.pending() == ()


@pytest.mark.parametrize("vessel_id", [123, True, " "])
def test_delete_requires_nonempty_string_vessel_id(vessel_id):
    with pytest.raises(ValueError):
        VesselCommandQueue().enqueue(VesselCommand(
            "d-1", "episode-1", "delete", vessel_id, 1, None, None,
        ))

from __future__ import annotations

import pytest

from src.mission.contracts import IntentCommand, CommandResult
from src.mission.intent_commands import (
    CommandConflict,
    IntentCommandQueue,
    QueueFull,
)


def _command(command_id: str = "cmd-1", *, label: str = "focus") -> IntentCommand:
    return IntentCommand(
        command_id=command_id,
        episode_id="episode-1",
        operation="create",
        intent_id=None,
        expected_revision=None,
        payload={
            "label": label,
            "bbox": [2, 2, 6, 6],
            "mode": "search_priority",
            "priority": "high",
            "weight": 1.0,
            "valid_duration_min": 20.0,
            "revisit_interval_min": None,
        },
    )


def test_queue_is_idempotent_and_completes_commands():
    queue = IntentCommandQueue(maxsize=2)

    first = queue.enqueue(_command())
    duplicate = queue.enqueue(_command())

    assert first == duplicate
    assert first.status == "queued"
    assert queue.drain() == (_command(),)

    applied = CommandResult("cmd-1", "applied", None, None)
    queue.complete(applied)
    assert queue.get("cmd-1") == applied


def test_same_command_id_with_different_payload_is_rejected():
    queue = IntentCommandQueue(maxsize=2)
    queue.enqueue(_command())

    with pytest.raises(CommandConflict):
        queue.enqueue(_command(label="different"))


def test_queue_capacity_is_bounded_until_a_command_is_drained():
    queue = IntentCommandQueue(maxsize=1)
    queue.enqueue(_command())

    with pytest.raises(QueueFull):
        queue.enqueue(_command("cmd-2"))

    queue.drain()
    queue.enqueue(_command("cmd-2"))


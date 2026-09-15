"""Thread-safe command queues for operator intent and runtime mutations."""

from __future__ import annotations

from collections import deque
import copy
import hashlib
import json
from threading import RLock

from src.mission.contracts import (
    CommandResult,
    IntentCommand,
    RuntimeCommand,
)


class CommandConflict(ValueError):
    """The command ID was already used for a different command payload."""


class QueueFull(RuntimeError):
    """The bounded command queue has no free slot."""


class _CommandQueue:
    def __init__(self, maxsize: int) -> None:
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError("maxsize must be a positive integer")
        self.maxsize = maxsize
        self._lock = RLock()
        self._pending: deque[str] = deque()
        self._commands: dict[str, object] = {}
        self._hashes: dict[str, str] = {}
        self._results: dict[str, CommandResult] = {}

    def _enqueue(self, command: object, command_id: str) -> CommandResult:
        payload_hash = _command_hash(command)
        with self._lock:
            previous_hash = self._hashes.get(command_id)
            if previous_hash is not None:
                if previous_hash != payload_hash:
                    raise CommandConflict(f"command_id already used: {command_id}")
                return copy.deepcopy(self._results[command_id])
            if len(self._pending) >= self.maxsize:
                raise QueueFull("command queue is full")
            self._commands[command_id] = copy.deepcopy(command)
            self._hashes[command_id] = payload_hash
            self._results[command_id] = CommandResult(
                command_id, "queued", None, None,
            )
            self._pending.append(command_id)
            return copy.deepcopy(self._results[command_id])

    def _drain(self) -> tuple[object, ...]:
        with self._lock:
            command_ids = tuple(self._pending)
            self._pending.clear()
            return tuple(copy.deepcopy(self._commands[item]) for item in command_ids)

    def _complete(self, result: CommandResult) -> None:
        if not isinstance(result, CommandResult):
            raise TypeError("result must be a CommandResult")
        if result.status not in {"applied", "rejected"}:
            raise ValueError("completed result must be applied or rejected")
        with self._lock:
            if result.command_id not in self._commands:
                raise KeyError(f"unknown command: {result.command_id}")
            self._results[result.command_id] = copy.deepcopy(result)

    def _get(self, command_id: str) -> CommandResult | None:
        with self._lock:
            result = self._results.get(command_id)
            return copy.deepcopy(result) if result is not None else None

    def _pending_results(self) -> tuple[CommandResult, ...]:
        with self._lock:
            return tuple(
                copy.deepcopy(self._results[command_id])
                for command_id in self._pending
            )


class IntentCommandQueue(_CommandQueue):
    """Bounded, idempotent queue consumed by the simulation thread."""

    def enqueue(self, command: IntentCommand) -> CommandResult:
        _validate_intent_command(command)
        return self._enqueue(command, command.command_id)

    def drain(self) -> tuple[IntentCommand, ...]:
        return self._drain()  # type: ignore[return-value]

    def complete(self, result: CommandResult) -> None:
        self._complete(result)

    def get(self, command_id: str) -> CommandResult | None:
        return self._get(command_id)

    def pending(self) -> tuple[CommandResult, ...]:
        return self._pending_results()


class RuntimeCommandQueue(_CommandQueue):
    """Separate queue for retry/abort commands."""

    def enqueue(self, command: RuntimeCommand) -> CommandResult:
        _validate_runtime_command(command)
        return self._enqueue(command, command.command_id)

    def drain(self) -> tuple[RuntimeCommand, ...]:
        return self._drain()  # type: ignore[return-value]

    def complete(self, result: CommandResult) -> None:
        self._complete(result)

    def get(self, command_id: str) -> CommandResult | None:
        return self._get(command_id)

    def pending(self) -> tuple[CommandResult, ...]:
        return self._pending_results()


class IntentCommandService:
    """The narrow server-facing view of a live engine.

    The service exposes queues and published read models, while mutation is
    still performed by ``SimulationEngine.apply_pending_*`` on its own thread.
    """

    def __init__(self, engine, *, replay_mode: bool = False) -> None:
        self._engine = engine
        self.replay_mode = bool(replay_mode)

    @property
    def episode_id(self) -> str:
        return self._engine.episode_id

    @property
    def queue(self) -> IntentCommandQueue:
        return self._engine.intent_commands

    @property
    def runtime_queue(self) -> RuntimeCommandQueue:
        return self._engine.runtime_commands

    @property
    def runtime_status(self) -> str:
        return self._engine.runtime_status

    def published_intents(self) -> dict:
        return self._engine.published_intent_snapshot()

    def get_command_result(self, command_id: str) -> CommandResult | None:
        """Look up live commands and retained reset tombstones."""
        result = self.queue.get(command_id)
        if result is not None:
            return result
        result = self.runtime_queue.get(command_id)
        if result is not None:
            return result
        retired = getattr(self._engine, "retired_command_results", {})
        result = retired.get(command_id)
        return copy.deepcopy(result) if result is not None else None


def _validate_intent_command(command: IntentCommand) -> None:
    if not isinstance(command, IntentCommand):
        raise TypeError("command must be an IntentCommand")
    _nonempty(command.command_id, "command_id")
    _nonempty(command.episode_id, "episode_id")
    if command.operation not in {"create", "update", "cancel"}:
        raise ValueError("invalid intent operation")
    if command.operation == "create":
        if command.intent_id is not None or command.expected_revision is not None:
            raise ValueError("create command cannot carry intent_id or revision")
    else:
        _nonempty(command.intent_id, "intent_id")
        if (
            isinstance(command.expected_revision, bool)
            or not isinstance(command.expected_revision, int)
            or command.expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
    try:
        json.dumps(command.payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain JSON finite values") from exc


def _validate_runtime_command(command: RuntimeCommand) -> None:
    if not isinstance(command, RuntimeCommand):
        raise TypeError("command must be a RuntimeCommand")
    _nonempty(command.command_id, "command_id")
    _nonempty(command.episode_id, "episode_id")
    if command.operation not in {"retry", "abort"}:
        raise ValueError("invalid runtime operation")


def _nonempty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _command_hash(command: object) -> str:
    if isinstance(command, IntentCommand):
        value = {
            "command_id": command.command_id,
            "episode_id": command.episode_id,
            "operation": command.operation,
            "intent_id": command.intent_id,
            "expected_revision": command.expected_revision,
            "payload": command.payload,
        }
    elif isinstance(command, RuntimeCommand):
        value = {
            "command_id": command.command_id,
            "episode_id": command.episode_id,
            "operation": command.operation,
        }
    else:
        raise TypeError("unsupported command type")
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CommandConflict",
    "IntentCommandQueue",
    "IntentCommandService",
    "QueueFull",
    "RuntimeCommandQueue",
]

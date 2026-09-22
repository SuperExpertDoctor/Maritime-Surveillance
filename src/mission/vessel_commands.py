"""Thread-safe, idempotent queue for initialization vessel edits."""
from __future__ import annotations

from collections import deque
import copy
import hashlib
import json
import math
from threading import RLock
from dataclasses import dataclass

from src.mission.contracts import VesselCommand


class CommandConflict(ValueError):
    pass


@dataclass(frozen=True)
class VesselCommandResult:
    command_id: str
    status: str
    vessel_id: str | None
    revision: int | None
    error_code: str | None = None


class VesselCommandQueue:
    def __init__(self, maxsize: int = 64):
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError("maxsize must be a positive integer")
        self.maxsize = maxsize
        self._lock = RLock()
        self._pending: deque[str] = deque()
        self._commands: dict[str, VesselCommand] = {}
        self._hashes: dict[str, str] = {}
        self._results: dict[str, VesselCommandResult] = {}

    def enqueue(self, command: VesselCommand) -> VesselCommandResult:
        _validate(command)
        encoded = json.dumps(_payload(command), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._lock:
            previous = self._hashes.get(command.command_id)
            if previous is not None:
                if previous != digest:
                    raise CommandConflict(f"command_id already used: {command.command_id}")
                return copy.deepcopy(self._results[command.command_id])
            if len(self._pending) >= self.maxsize:
                raise RuntimeError("vessel command queue is full")
            self._commands[command.command_id] = copy.deepcopy(command)
            self._hashes[command.command_id] = digest
            result = VesselCommandResult(command.command_id, "queued", command.vessel_id, None)
            self._results[command.command_id] = result
            self._pending.append(command.command_id)
            return copy.deepcopy(result)

    def drain(self) -> tuple[VesselCommand, ...]:
        with self._lock:
            ids = tuple(self._pending)
            self._pending.clear()
            return tuple(copy.deepcopy(self._commands[item]) for item in ids)

    def complete(self, result: VesselCommandResult) -> None:
        if result.status not in {"applied", "rejected"}:
            raise ValueError("completed vessel result must be applied or rejected")
        with self._lock:
            if result.command_id not in self._commands:
                raise KeyError(f"unknown vessel command: {result.command_id}")
            self._results[result.command_id] = copy.deepcopy(result)

    def get(self, command_id: str) -> VesselCommandResult | None:
        with self._lock:
            result = self._results.get(command_id)
            return copy.deepcopy(result) if result is not None else None

    def pending(self) -> tuple[VesselCommandResult, ...]:
        with self._lock:
            return tuple(copy.deepcopy(self._results[item]) for item in self._pending)


def _payload(command: VesselCommand) -> dict:
    return {
        "command_id": command.command_id,
        "episode_id": command.episode_id,
        "operation": command.operation,
        "vessel_id": command.vessel_id,
        "expected_revision": command.expected_revision,
        "vessel_class": command.vessel_class,
        "position_cells": command.position_cells,
        "ais_enabled": command.ais_enabled,
    }


def _validate(command: VesselCommand) -> None:
    if not isinstance(command, VesselCommand):
        raise TypeError("command must be VesselCommand")
    if any(not isinstance(value, str) or not value.strip()
           for value in (command.command_id, command.episode_id)):
        raise ValueError("command_id and episode_id are required")
    if command.vessel_id is not None and (
        not isinstance(command.vessel_id, str) or not command.vessel_id.strip()
    ):
        raise ValueError("vessel_id must be a nonempty string")
    if command.operation == "create":
        if command.vessel_id is not None or command.expected_revision is not None:
            raise ValueError("create command cannot contain vessel_id or revision")
        if command.vessel_class not in {"type_i", "type_ii"}:
            raise ValueError("create command requires vessel_class")
        if command.ais_enabled is not None:
            raise ValueError("create command cannot contain ais_enabled")
        if (not isinstance(command.position_cells, (tuple, list))
                or len(command.position_cells) != 2):
            raise ValueError("create command requires position_cells")
        try:
            finite = all(type(value) in (int, float) and math.isfinite(value)
                         for value in command.position_cells)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("position_cells must be finite")
    elif command.operation == "delete":
        if (not command.vessel_id or command.vessel_class is not None
                or command.position_cells is not None
                or command.ais_enabled is not None):
            raise ValueError("delete command requires only vessel_id and revision")
        if (isinstance(command.expected_revision, bool)
                or not isinstance(command.expected_revision, int)
                or command.expected_revision < 1):
            raise ValueError("delete command requires positive expected_revision")
    elif command.operation == "set_ais":
        if (
            not command.vessel_id
            or isinstance(command.expected_revision, bool)
            or not isinstance(command.expected_revision, int)
            or command.expected_revision < 1
            or type(command.ais_enabled) is not bool
            or command.vessel_class is not None
            or command.position_cells is not None
        ):
            raise ValueError("set_ais requires vessel_id, revision, and ais_enabled")
    else:
        raise ValueError("invalid vessel command operation")


__all__ = ["CommandConflict", "VesselCommandQueue", "VesselCommandResult"]

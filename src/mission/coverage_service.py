"""Task-scoped SAR footprint accounting and completion validation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math
from numbers import Integral, Real

import numpy as np


Cell = tuple[int, int]
BBox = tuple[int, int, int, int]


@dataclass(frozen=True)
class CoverageTaskProgress:
    """Immutable progress for one task assignment generation."""

    task_id: str
    generation: int
    uav_id: str
    bbox: BBox
    started_at_min: float
    required_cells: tuple[Cell, ...]
    scanned_cells: tuple[Cell, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _task_id(self.task_id, "task_id"))
        object.__setattr__(self, "generation", _generation(self.generation))
        object.__setattr__(self, "uav_id", _task_id(self.uav_id, "uav_id"))
        object.__setattr__(self, "bbox", _bbox(self.bbox))
        object.__setattr__(self, "started_at_min", _time(self.started_at_min, "started_at_min"))
        object.__setattr__(
            self,
            "required_cells",
            _cell_tuple(self.required_cells, "required_cells"),
        )
        object.__setattr__(
            self,
            "scanned_cells",
            _cell_tuple(self.scanned_cells, "scanned_cells"),
        )


@dataclass(frozen=True)
class CoverageCompletion:
    """Immutable result of validating a route-finished coverage task."""

    complete: bool
    scanned_cells: int
    required_cells: int
    missing_cells: tuple[Cell, ...]
    completion_pct: float

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise ValueError("complete: expected boolean")
        scanned = _count(self.scanned_cells, "scanned_cells")
        required = _count(self.required_cells, "required_cells")
        if scanned > required:
            raise ValueError("scanned_cells cannot exceed required_cells")
        missing = _cell_tuple(self.missing_cells, "missing_cells")
        percentage = _percentage(self.completion_pct, "completion_pct")
        object.__setattr__(self, "scanned_cells", scanned)
        object.__setattr__(self, "required_cells", required)
        object.__setattr__(self, "missing_cells", missing)
        object.__setattr__(self, "completion_pct", percentage)


@dataclass
class _TaskState:
    task_id: str
    generation: int
    uav_id: str
    bbox: BBox
    started_at_min: float
    required_cells: frozenset[Cell]
    scanned_cells: set[Cell]
    closed: bool = False
    close_reason: str | None = None
    completion: CoverageCompletion | None = None


class CoverageService:
    """Validate task completion from actual SAR cells for one fixed domain.

    The service deliberately owns no controller, lease, region, or scheduler
    state.  A task generation is the only identity used to attribute a SAR
    footprint to a task.
    """

    def __init__(self, fixed_mask: np.ndarray) -> None:
        fixed = np.asarray(fixed_mask, dtype=bool)
        if fixed.ndim != 2 or not fixed.size:
            raise ValueError("fixed_mask: expected a non-empty two-dimensional mask")
        self._fixed_mask = fixed.copy()
        self._fixed_mask.setflags(write=False)
        self._tasks: dict[tuple[str, int, str], _TaskState] = {}
        self._latest_generation: dict[tuple[str, str], int] = {}
        self._latest_key: dict[str, tuple[str, int, str]] = {}
        self._audit_signals: list[dict[str, object]] = []

    def start(
        self,
        task_id: str,
        generation: int,
        uav_id: str,
        bbox: tuple[int, int, int, int],
        at_min: float,
        *,
        initial_scanned_cells: Iterable[tuple[int, int]] = (),
    ) -> None:
        task_id, generation, uav_id, bbox, started_at_min, required_cells = (
            self._validated_start(task_id, generation, uav_id, bbox, at_min)
        )
        key = (task_id, generation, uav_id)
        initial_cells = set(_cell_tuple(initial_scanned_cells, "initial_scanned_cells"))

        for state in self._tasks.values():
            if state.task_id == task_id and not state.closed:
                state.closed = True
                state.close_reason = "replaced"

        self._tasks[key] = _TaskState(
            task_id=task_id,
            generation=generation,
            uav_id=uav_id,
            bbox=bbox,
            started_at_min=started_at_min,
            required_cells=frozenset(required_cells),
            scanned_cells=initial_cells & set(required_cells),
        )
        self._latest_generation[(task_id, uav_id)] = generation
        self._latest_key[task_id] = key

    def validate_start(
        self,
        task_id: str,
        generation: int,
        uav_id: str,
        bbox: tuple[int, int, int, int],
        at_min: float,
    ) -> None:
        """Validate a start without changing the task ledger."""
        self._validated_start(task_id, generation, uav_id, bbox, at_min)

    def _validated_start(
        self,
        task_id: str,
        generation: int,
        uav_id: str,
        bbox: tuple[int, int, int, int],
        at_min: float,
    ) -> tuple[str, int, str, BBox, float, frozenset[Cell]]:
        task_id = _task_id(task_id, "task_id")
        generation = _generation(generation)
        uav_id = _task_id(uav_id, "uav_id")
        bbox = _bbox(bbox)
        started_at_min = _time(at_min, "at_min")
        required_cells = self._required_cells(bbox)
        if not required_cells:
            raise ValueError("task bbox has no required fixed cells")

        key = (task_id, generation, uav_id)
        latest = self._latest_generation.get((task_id, uav_id))
        if latest is not None and generation <= latest:
            raise ValueError("generation must increase for a task id")
        if key in self._tasks:
            raise ValueError("task generation has already been started")
        return task_id, generation, uav_id, bbox, started_at_min, required_cells

    def record(
        self,
        task_id: str,
        generation: int,
        cells: Iterable[tuple[int, int]],
        at_min: float,
        *,
        uav_id: str | None = None,
    ) -> None:
        task_id = _task_id(task_id, "task_id")
        generation = _generation(generation)
        at_min = _time(at_min, "at_min")
        state = self._resolve_state(task_id, generation, uav_id)
        if state is None:
            reason = (
                "stale_generation"
                if self._has_task(task_id)
                else "unknown_task"
            )
            self._audit("coverage_record_ignored", task_id, generation, reason, at_min)
            return
        if state.closed:
            latest_key = self._latest_key.get(task_id)
            reason = (
                "stale_generation"
                if latest_key is not None and latest_key[1] != generation
                else "closed"
            )
            self._audit("coverage_record_ignored", task_id, generation, reason, at_min)
            return
        if at_min < state.started_at_min:
            self._audit("coverage_record_ignored", task_id, generation, "before_start", at_min)
            return

        validated_cells = _cell_tuple(cells, "cells")
        cols, rows = self._fixed_mask.shape
        state.scanned_cells.update(
            cell
            for cell in validated_cells
            if 0 <= cell[0] < cols
            and 0 <= cell[1] < rows
            and bool(self._fixed_mask[cell])
            and cell in state.required_cells
        )

    def finish(
        self,
        task_id: str,
        generation: int,
        at_min: float,
        *,
        uav_id: str | None = None,
    ) -> CoverageCompletion:
        task_id = _task_id(task_id, "task_id")
        generation = _generation(generation)
        at_min = _time(at_min, "at_min")
        state = self._resolve_state(task_id, generation, uav_id)
        if state is None:
            raise ValueError("unknown coverage task generation")
        if at_min < state.started_at_min:
            raise ValueError("finish time cannot be earlier than task start")
        if state.completion is not None:
            return state.completion

        scanned = len(state.scanned_cells)
        required = len(state.required_cells)
        missing = tuple(sorted(state.required_cells - state.scanned_cells))
        completion = CoverageCompletion(
            complete=not missing,
            scanned_cells=scanned,
            required_cells=required,
            missing_cells=missing,
            completion_pct=100.0 * scanned / required,
        )
        state.completion = completion
        state.closed = True
        state.close_reason = "finished"
        return completion

    def close(
        self,
        task_id: str,
        generation: int,
        reason: str,
        *,
        uav_id: str | None = None,
    ) -> None:
        task_id = _task_id(task_id, "task_id")
        generation = _generation(generation)
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason: expected a non-empty string")
        state = self._resolve_state(task_id, generation, uav_id)
        if state is None:
            audit_reason = (
                "stale_generation"
                if self._has_task(task_id)
                else "unknown_task"
            )
            self._audit("coverage_close_ignored", task_id, generation, audit_reason, None)
            return
        if state.closed:
            return
        state.closed = True
        state.close_reason = reason

    def progress(
        self,
        task_id: str,
        generation: int,
        *,
        uav_id: str | None = None,
    ) -> CoverageTaskProgress | None:
        task_id = _task_id(task_id, "task_id")
        generation = _generation(generation)
        state = self._resolve_state(task_id, generation, uav_id)
        if state is None:
            return None
        return CoverageTaskProgress(
            task_id=state.task_id,
            generation=state.generation,
            uav_id=state.uav_id,
            bbox=state.bbox,
            started_at_min=state.started_at_min,
            required_cells=tuple(sorted(state.required_cells)),
            scanned_cells=tuple(sorted(state.scanned_cells)),
        )

    def latest_generation(self, task_id: str, *, uav_id: str | None = None) -> int | None:
        """Return the newest generation known for a task, including closed tasks."""
        task_id = _task_id(task_id, "task_id")
        if uav_id is not None:
            uav_id = _task_id(uav_id, "uav_id")
            return self._latest_generation.get((task_id, uav_id))
        latest_key = self._latest_key.get(task_id)
        return latest_key[1] if latest_key is not None else None

    def _has_task(self, task_id: str) -> bool:
        return any(state.task_id == task_id for state in self._tasks.values())

    def _resolve_state(
        self,
        task_id: str,
        generation: int,
        uav_id: str | None,
    ) -> _TaskState | None:
        if uav_id is not None:
            uav_id = _task_id(uav_id, "uav_id")
            return self._tasks.get((task_id, generation, uav_id))
        matches = [
            state for key, state in self._tasks.items()
            if key[0] == task_id and key[1] == generation
        ]
        if not matches:
            return None
        latest_key = self._latest_key.get(task_id)
        if latest_key is not None:
            latest = self._tasks.get(latest_key)
            if latest is not None and latest.generation == generation:
                return latest
        return matches[-1]

    @property
    def audit_signals(self) -> tuple[dict[str, object], ...]:
        """Return detached, JSON-ready audit signals for ignored events."""
        return tuple(dict(signal) for signal in self._audit_signals)

    @property
    def audit_events(self) -> tuple[dict[str, object], ...]:
        """Compatibility alias for consumers that call audit records events."""
        return self.audit_signals

    def _required_cells(self, bbox: BBox) -> frozenset[Cell]:
        c0, r0, c1, r1 = bbox
        cols, rows = self._fixed_mask.shape
        start_col = max(0, c0)
        end_col = min(cols, c1)
        start_row = max(0, r0)
        end_row = min(rows, r1)
        return frozenset(
            (col, row)
            for col in range(start_col, end_col)
            for row in range(start_row, end_row)
            if bool(self._fixed_mask[col, row])
        )

    def _audit(
        self,
        event_type: str,
        task_id: str,
        generation: int,
        reason: str,
        at_min: float | None,
    ) -> None:
        self._audit_signals.append({
            "type": event_type,
            "task_id": task_id,
            "generation": generation,
            "reason": reason,
            "at_min": at_min,
        })


def _task_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: expected a non-empty string")
    return value


def _generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError("generation: expected a non-negative integer")
    return int(value)


def _time(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name}: expected a finite non-negative number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name}: expected a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name}: expected a finite non-negative number")
    return number


def _bbox(value: object) -> BBox:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError("bbox: expected four integer coordinates")
    if any(isinstance(item, bool) or not isinstance(item, Integral) for item in value):
        raise ValueError("bbox: expected four integer coordinates")
    c0, r0, c1, r1 = (int(item) for item in value)
    if c0 >= c1 or r0 >= r1:
        raise ValueError("bbox: expected a non-empty half-open rectangle")
    return c0, r0, c1, r1


def _cell_tuple(value: Iterable[tuple[int, int]], name: str) -> tuple[Cell, ...]:
    if isinstance(value, (str, bytes, dict)):
        raise ValueError(f"{name}: expected coordinate pairs")
    try:
        cells = tuple(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: expected coordinate pairs") from exc
    normalized: list[Cell] = []
    for cell in cells:
        if not isinstance(cell, (tuple, list)) or len(cell) != 2:
            raise ValueError(f"{name}: expected integer coordinate pairs")
        col, row = cell
        if (
            isinstance(col, bool)
            or not isinstance(col, Integral)
            or isinstance(row, bool)
            or not isinstance(row, Integral)
        ):
            raise ValueError(f"{name}: expected integer coordinate pairs")
        normalized.append((int(col), int(row)))
    return tuple(normalized)


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError(f"{name}: expected a non-negative integer")
    return int(value)


def _percentage(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name}: expected a finite percentage")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name}: expected a finite percentage") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 100.0:
        raise ValueError(f"{name}: expected a finite percentage")
    return number


__all__ = ["CoverageCompletion", "CoverageService", "CoverageTaskProgress"]

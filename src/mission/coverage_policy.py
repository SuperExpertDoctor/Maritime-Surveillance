"""Deterministic whole-domain responsibility policy for SAR coverage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any

import numpy as np


_CATEGORY_ORDER = (
    "fresh",
    "deferred_weather",
    "assigned_due",
    "geometry",
    "waiting_due",
)
_UNSEEN = -np.inf


@dataclass(frozen=True)
class CoverageCandidateWindow:
    """One immutable candidate set shared by route checks and the prompt."""

    tasks: tuple[Any, ...]
    representative_task_ids: tuple[str, ...] = ()
    sources: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        tasks = tuple(self.tasks)
        ids = tuple(
            getattr(task, "task_id", None)
            for task in tasks
        )
        if any(not isinstance(task_id, str) or not task_id for task_id in ids):
            raise ValueError("coverage window tasks require non-empty task IDs")
        if len(set(ids)) != len(ids):
            raise ValueError("coverage window task IDs must be unique")
        representative = tuple(self.representative_task_ids)
        if any(task_id not in ids for task_id in representative):
            raise ValueError("coverage representatives must be in the window")
        sources = tuple((str(task_id), str(source)) for task_id, source in self.sources)
        if len({task_id for task_id, _source in sources}) != len(sources):
            raise ValueError("coverage window sources must be unique")
        if any(task_id not in ids for task_id, _source in sources):
            raise ValueError("coverage window source task must be in the window")
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "representative_task_ids", representative)
        object.__setattr__(self, "sources", sources)


class CoveragePolicy:
    """Partition a fixed SAR domain and rank its search candidates.

    The policy intentionally uses only actual SAR timestamps for freshness.
    Other scheduling matrices can decide feasibility and assignment, but they
    cannot make a cell fresh.
    """

    def __init__(self, fixed_mask: np.ndarray, *, primary_window_min: int = 60) -> None:
        fixed = _mask(fixed_mask, "fixed_mask")
        if isinstance(primary_window_min, bool) or not isinstance(primary_window_min, Integral):
            raise ValueError("primary_window_min must be a positive integer")
        primary_window_min = int(primary_window_min)
        if primary_window_min <= 0:
            raise ValueError("primary_window_min must be a positive integer")

        self._fixed_mask = fixed.copy()
        self._fixed_mask.setflags(write=False)
        self.primary_window_min = primary_window_min

    @property
    def fixed_mask(self) -> np.ndarray:
        """Return a detached copy of the fixed responsibility domain."""
        return self._fixed_mask.copy()

    def classify(
        self,
        *,
        now_min: float,
        last_sar: np.ndarray,
        feasible_mask: np.ndarray,
        assigned_mask: np.ndarray,
        legal_candidate_mask: np.ndarray,
    ) -> dict[str, np.ndarray]:
        """Return an exhaustive, mutually exclusive responsibility partition.

        Freshness follows the left-open/right-closed interval
        ``(now_min - primary_window_min, now_min]``.  The remaining fixed
        cells are assigned in precedence order: weather, assigned work,
        geometry exclusion, then waiting for a legal candidate.
        """
        now = _time(now_min, "now_min")
        last = _last_sar(last_sar, self._fixed_mask.shape, now)
        feasible = _mask_like(feasible_mask, self._fixed_mask.shape, "feasible_mask")
        assigned = _mask_like(assigned_mask, self._fixed_mask.shape, "assigned_mask")
        legal = _mask_like(
            legal_candidate_mask,
            self._fixed_mask.shape,
            "legal_candidate_mask",
        )

        fixed = self._fixed_mask
        fresh = fixed & np.isfinite(last) & (last > now - self.primary_window_min)
        remaining = fixed & ~fresh
        deferred_weather = remaining & ~feasible
        remaining &= feasible
        assigned_due = remaining & assigned
        remaining &= ~assigned_due
        geometry = remaining & ~legal
        waiting_due = remaining & legal

        return {
            "fresh": fresh.copy(),
            "deferred_weather": deferred_weather.copy(),
            "assigned_due": assigned_due.copy(),
            "geometry": geometry.copy(),
            "waiting_due": waiting_due.copy(),
        }

    @staticmethod
    def rank_search_candidates(
        candidates: Iterable[Any],
        *,
        now_min: float,
        last_sar: np.ndarray,
        estimated_minutes: Mapping[str, Real],
        primary_window_min: int = 60,
    ) -> tuple[Any, ...]:
        """Return candidates ordered by unseen work, age, density, then bbox."""
        return rank_search_candidates(
            candidates,
            now_min=now_min,
            last_sar=last_sar,
            estimated_minutes=estimated_minutes,
            primary_window_min=primary_window_min,
        )

    def select_window(
        self,
        ranked_candidates: Iterable[Any],
        *,
        ordinary_reserve: int,
        capacity: int,
        now_min: float,
    ) -> CoverageCandidateWindow:
        """Reserve ordinary coverage slots before filling urgent work.

        ``ranked_candidates`` is already ordered by cell responsibility.  The
        method only chooses from that sequence; it never performs geometry or
        changes task identity.  This makes the returned IDs safe to reuse for
        edge construction and prompt serialization.
        """
        if isinstance(ordinary_reserve, bool) or not isinstance(ordinary_reserve, int):
            raise ValueError("ordinary_reserve must be a non-negative integer")
        if ordinary_reserve < 0:
            raise ValueError("ordinary_reserve must be a non-negative integer")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        _time(now_min, "now_min")

        normalized: list[Any] = []
        seen_ids: set[str] = set()
        for task in ranked_candidates:
            task_id = getattr(task, "task_id", None)
            if not isinstance(task_id, str) or not task_id or task_id in seen_ids:
                continue
            seen_ids.add(task_id)
            normalized.append(task)
        # Only ordinary SAR rectangles consume the standing coverage reserve.
        # Direction and passive-investigation tasks are urgent information
        # work even though they carry a search-shaped bbox.
        search_kinds = {"search"}
        ordinary = [
            task for task in normalized
            if getattr(task, "kind", "search") in search_kinds
        ]
        ordinary_ids = {task.task_id for task in ordinary}
        urgent = [task for task in normalized if task.task_id not in ordinary_ids]
        reserve_count = min(ordinary_reserve, capacity, len(ordinary))
        urgent_count = min(capacity - reserve_count, len(urgent))

        representative_ordinary: list[Any] = []
        representative_boxes: list[tuple[float, float, float, float]] = []
        for task in ordinary:
            bbox = getattr(task, "bbox", None)
            if bbox is None:
                continue
            normalized_bbox = tuple(float(value) for value in bbox)
            if any(_boxes_overlap(normalized_bbox, other) for other in representative_boxes):
                continue
            representative_ordinary.append(task)
            representative_boxes.append(normalized_bbox)
            if len(representative_ordinary) >= reserve_count:
                break
        selected = [*urgent[:urgent_count], *representative_ordinary]
        selected_ids = {task.task_id for task in selected}

        def task_box(task: Any) -> tuple[float, float, float, float] | None:
            bbox = getattr(task, "bbox", None)
            if bbox is None:
                return None
            return tuple(float(value) for value in bbox)

        selected_boxes = [
            box for task in selected if (box := task_box(task)) is not None
        ]

        # Keep the prompt useful after the representative reserve.  A dense
        # candidate pool often contains many overlapping windows around the
        # same representative; spending the remaining capacity on those
        # windows leaves otherwise healthy UAVs without legal successor work.
        for task in normalized:
            if len(selected) >= capacity or task.task_id in selected_ids:
                continue
            box = task_box(task)
            if task.task_id not in ordinary_ids or box is None:
                continue
            if any(_boxes_overlap(box, other) for other in selected_boxes):
                continue
            selected.append(task)
            selected_ids.add(task.task_id)
            selected_boxes.append(box)

        for task in normalized:
            if len(selected) >= capacity:
                break
            if task.task_id in selected_ids or task.task_id in ordinary_ids:
                continue
            selected.append(task)
            selected_ids.add(task.task_id)

        ordinary_selected = [
            task for task in selected if task.task_id in ordinary_ids
        ]
        representatives: list[str] = []
        representative_boxes: list[tuple[float, float, float, float]] = []
        for task in ordinary_selected:
            bbox = getattr(task, "bbox", None)
            if bbox is None:
                continue
            normalized_bbox = tuple(float(value) for value in bbox)
            if any(_boxes_overlap(normalized_bbox, other) for other in representative_boxes):
                continue
            representatives.append(task.task_id)
            representative_boxes.append(normalized_bbox)

        sources = []
        urgent_ids = {task.task_id for task in urgent}
        for task in selected:
            source = "urgent" if task.task_id in urgent_ids else "ordinary"
            sources.append((task.task_id, source))
        return CoverageCandidateWindow(
            tasks=tuple(selected),
            representative_task_ids=tuple(representatives),
            sources=tuple(sources),
        )


def build_coverage_constraint(
    *,
    healthy_count: int,
    active_search_count: int,
    available_ids: Iterable[str],
    representatives: Iterable[Any],
    edges: Iterable[Any],
    fraction: float,
):
    """Build a feasible search-resource floor using maximum matching.

    The return type is imported lazily to keep this policy module independent
    from the broader mission contract graph.
    """
    from src.mission.contracts import CoverageConstraint

    if any(
        isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0
        for value in (healthy_count, active_search_count)
    ):
        raise ValueError("healthy_count and active_search_count must be non-negative integers")
    if isinstance(fraction, bool) or not isinstance(fraction, Real):
        raise ValueError("fraction must be finite and in (0, 1]")
    fraction = float(fraction)
    if not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be finite and in (0, 1]")

    available = tuple(dict.fromkeys(str(item) for item in available_ids))
    available_set = set(available)
    representative_ids = tuple(
        getattr(item, "task_id", item) for item in representatives
    )
    if any(not isinstance(item, str) or not item for item in representative_ids):
        raise ValueError("representatives must contain task IDs or task objects")
    representative_ids = tuple(dict.fromkeys(representative_ids))
    adjacency = {task_id: [] for task_id in representative_ids}
    for edge in edges:
        if isinstance(edge, Mapping):
            task_id = edge.get("task_id")
            uav_id = edge.get("uav_id")
        else:
            task_id = getattr(edge, "task_id", None)
            uav_id = getattr(edge, "uav_id", None)
        if task_id in adjacency and uav_id in available_set:
            adjacency[task_id].append(uav_id)
    for task_id in adjacency:
        adjacency[task_id] = sorted(set(adjacency[task_id]))

    matched_uav: dict[str, str] = {}

    def visit(task_id: str, seen: set[str]) -> bool:
        for uav_id in adjacency[task_id]:
            if uav_id in seen:
                continue
            seen.add(uav_id)
            previous = matched_uav.get(uav_id)
            if previous is None or visit(previous, seen):
                matched_uav[uav_id] = task_id
                return True
        return False

    for task_id in representative_ids:
        visit(task_id, set())
    feasible_slots = len(matched_uav)
    desired = int(math.ceil(int(healthy_count) * fraction))
    outstanding = max(desired - int(active_search_count), 0)
    if outstanding == 0:
        required = 0
        infeasible_reason = None
    elif feasible_slots == 0:
        # No legal edge means the model may defer without fabricating a slot.
        required = 0
        infeasible_reason = "no_feasible_search_edges"
    else:
        required = outstanding
        infeasible_reason = (
            None if feasible_slots >= outstanding
            else "insufficient_available_resources"
        )
    must_service = ()
    if required:
        must_service = next(
            (task_id for task_id in representative_ids if adjacency[task_id]),
            (),
        )
        must_service = (must_service,) if must_service else ()
    return CoverageConstraint(
        desired_search_count=desired,
        active_search_count=int(active_search_count),
        required_new_search_count=required,
        representative_task_ids=representative_ids,
        must_service_task_ids=must_service,
        infeasible_reason=infeasible_reason,
    )


def rank_search_candidates(
    candidates: Iterable[Any],
    *,
    now_min: float,
    last_sar: np.ndarray,
    estimated_minutes: Mapping[str, Real],
    primary_window_min: int = 60,
) -> tuple[Any, ...]:
    """Rank search candidates without changing their identity or order data.

    The key is ``(unseen first, oldest due time, regular work, due density,
    shorter task, bbox)``.  An unseen cell has episode age zero; otherwise age
    is represented by its actual SAR timestamp.  Residual ``fragment:*``
    candidates are deliberately considered after complete search rectangles
    at the same freshness so short edge strips do not consume a sortie before
    the broad domain pass is complete.  Missing route estimates use the
    candidate cell count as a conservative one-cell-per-minute estimate.
    """
    now = _time(now_min, "now_min")
    if (
        isinstance(primary_window_min, bool)
        or not isinstance(primary_window_min, Integral)
        or int(primary_window_min) <= 0
    ):
        raise ValueError("primary_window_min must be a positive integer")
    primary_window_min = int(primary_window_min)
    last = _last_sar(last_sar, np.asarray(last_sar).shape, now)
    if not isinstance(estimated_minutes, Mapping):
        raise ValueError("estimated_minutes must be a mapping")

    normalized: list[tuple[Any, str, tuple[int, int, int, int], tuple[tuple[int, int], ...]]] = []
    for candidate in tuple(candidates):
        task_id = _candidate_value(candidate, "task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("candidate task_id must be a non-empty string")
        bbox = _bbox(_candidate_value(candidate, "bbox"), task_id)
        cells = _candidate_cells(candidate, bbox, last.shape, task_id)
        normalized.append((candidate, task_id, bbox, cells))

    ranked = sorted(
        normalized,
        key=lambda item: _rank_key(
            item[1],
            item[2],
            item[3],
            now,
            last,
            estimated_minutes,
            primary_window_min,
        ),
    )
    return tuple(item[0] for item in ranked)


def _rank_key(
    task_id: str,
    bbox: tuple[int, int, int, int],
    cells: tuple[tuple[int, int], ...],
    now: float,
    last: np.ndarray,
    estimated_minutes: Mapping[str, Real],
    primary_window_min: int,
) -> tuple[int, float, int, float, int, tuple[int, int, int, int]]:
    timestamps = np.asarray([last[col, row] for col, row in cells], dtype=float)
    unseen = ~np.isfinite(timestamps)
    due = unseen | (timestamps <= now - primary_window_min)
    unseen_count = int(np.count_nonzero(unseen))
    due_count = int(np.count_nonzero(due))
    if unseen_count:
        oldest_due = 0.0
    elif due_count:
        oldest_due = float(np.min(timestamps[due]))
    else:
        oldest_due = now

    estimate = estimated_minutes.get(task_id)
    if estimate is None:
        estimate_value = float(max(1, len(cells)))
    else:
        if isinstance(estimate, bool) or not isinstance(estimate, Real):
            raise ValueError(f"estimated_minutes[{task_id!r}] must be finite and positive")
        estimate_value = float(estimate)
        if not math.isfinite(estimate_value) or estimate_value <= 0:
            raise ValueError(f"estimated_minutes[{task_id!r}] must be finite and positive")

    density = due_count / max(estimate_value, 1e-6)
    fragment = int(task_id.startswith("fragment:"))
    return (
        0 if unseen_count else 1,
        oldest_due,
        fragment,
        -density,
        len(cells),
        bbox,
    )


def _boxes_overlap(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return not (
        left[2] <= right[0]
        or right[2] <= left[0]
        or left[3] <= right[1]
        or right[3] <= left[1]
    )


def _candidate_value(candidate: Any, name: str) -> Any:
    if isinstance(candidate, Mapping):
        if name not in candidate:
            raise ValueError(f"candidate missing {name}")
        return candidate[name]
    try:
        return getattr(candidate, name)
    except AttributeError as exc:
        raise ValueError(f"candidate missing {name}") from exc


def _candidate_cells(
    candidate: Any,
    bbox: tuple[int, int, int, int],
    shape: tuple[int, ...],
    task_id: str,
) -> tuple[tuple[int, int], ...]:
    raw_cells: Any
    if isinstance(candidate, Mapping):
        raw_cells = candidate.get("cells")
    else:
        raw_cells = getattr(candidate, "cells", None)
    if raw_cells is None:
        raw_cells = tuple(
            (col, row)
            for col in range(bbox[0], bbox[2])
            for row in range(bbox[1], bbox[3])
        )

    try:
        cells = tuple(raw_cells)
    except TypeError as exc:
        raise ValueError(f"candidate {task_id!r} cells must be iterable") from exc
    if not cells:
        raise ValueError(f"candidate {task_id!r} must contain at least one cell")

    cols, rows = shape
    normalized: list[tuple[int, int]] = []
    for cell in cells:
        if not isinstance(cell, (tuple, list)) or len(cell) != 2:
            raise ValueError(f"candidate {task_id!r} has invalid cell")
        col, row = cell
        if (
            isinstance(col, bool)
            or isinstance(row, bool)
            or not isinstance(col, Integral)
            or not isinstance(row, Integral)
            or not 0 <= int(col) < cols
            or not 0 <= int(row) < rows
        ):
            raise ValueError(f"candidate {task_id!r} cell is outside last_sar")
        normalized.append((int(col), int(row)))
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"candidate {task_id!r} contains duplicate cells")
    return tuple(normalized)


def _bbox(value: Any, task_id: str) -> tuple[int, int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError(f"candidate {task_id!r} bbox must contain four integers")
    values = tuple(value)
    if any(isinstance(item, bool) or not isinstance(item, Integral) for item in values):
        raise ValueError(f"candidate {task_id!r} bbox must contain four integers")
    result = tuple(int(item) for item in values)
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError(f"candidate {task_id!r} bbox must have positive area")
    return result


def _mask(value: Any, name: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=bool)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a two-dimensional mask") from exc
    if array.ndim != 2:
        raise ValueError(f"{name} must be a two-dimensional mask")
    return array


def _mask_like(value: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    array = _mask(value, name)
    if array.shape != shape:
        raise ValueError(f"{name} shape must match fixed_mask")
    return array.copy()


def _last_sar(value: Any, shape: tuple[int, ...], now: float) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("last_sar must be a numeric two-dimensional matrix") from exc
    if array.ndim != 2:
        raise ValueError("last_sar must be a numeric two-dimensional matrix")
    if array.shape != shape:
        raise ValueError("last_sar shape must match fixed_mask")
    if np.isnan(array).any() or np.isposinf(array).any():
        raise ValueError("last_sar must contain finite non-negative times or -inf")
    finite = np.isfinite(array)
    if np.any(array[finite] < 0) or np.any(array[finite] > now):
        raise ValueError("last_sar timestamps must be between zero and now_min")
    return array.copy()


def _time(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


__all__ = [
    "CoverageCandidateWindow",
    "CoveragePolicy",
    "build_coverage_constraint",
    "rank_search_candidates",
]

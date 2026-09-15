"""Validated operator focus areas and their scheduling contribution."""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any, Iterable

import numpy as np

from src.mission.contracts import Intent, IntentStatus


_CREATE_FIELDS = {
    "label", "bbox", "mode", "priority", "weight",
    "valid_duration_min", "revisit_interval_min",
}
_UPDATE_FIELDS = _CREATE_FIELDS
_PRIORITY_MULTIPLIERS = {"high": 3.0, "medium": 2.0, "low": 1.0}


class IntentStore:
    """Own mutable intent lifecycle state while exposing frozen snapshots."""

    def __init__(
        self,
        searchable_mask: np.ndarray,
        config: Any | None = None,
    ) -> None:
        mask = np.asarray(searchable_mask, dtype=bool)
        if mask.ndim != 2 or not mask.size:
            raise ValueError("searchable_mask: expected a non-empty 2D mask")
        self._searchable_mask = mask.copy()
        self._shape = mask.shape
        self._max_active = int(getattr(config, "max_active_intents", 20))
        self._default_duration = float(getattr(config, "default_valid_duration_min", 120.0))
        self._default_revisit = float(getattr(config, "default_revisit_interval_min", 20.0))
        self._default_weight = float(getattr(config, "default_weight", 0.5))
        self._freshness_threshold = float(getattr(config, "freshness_threshold", 0.7))
        self._intents: dict[str, Intent] = {}
        self._next_id = 1

    def create(self, data: dict, now_min: float) -> Intent:
        now = _finite_nonnegative(now_min, "now_min")
        if not isinstance(data, dict):
            raise ValueError("intent data: expected mapping")
        _reject_unknown(data, _CREATE_FIELDS, "intent data")
        if len(self.active()) >= self._max_active:
            raise ValueError("maximum active intents reached")
        fields = self._validated_fields(data, creating=True)
        intent = Intent(
            intent_id=f"I{self._next_id:04d}",
            revision=1,
            created_at_min=now,
            expires_at_min=now + fields.pop("valid_duration_min"),
            lifecycle="active",
            **fields,
        )
        self._intents[intent.intent_id] = intent
        self._next_id += 1
        return intent

    def update(
        self,
        intent_id: str,
        expected_revision: int,
        changes: dict,
        now_min: float,
    ) -> Intent:
        now = _finite_nonnegative(now_min, "now_min")
        intent = self._current_active(intent_id, expected_revision)
        if not isinstance(changes, dict) or not changes:
            raise ValueError("intent changes: expected a non-empty mapping")
        _reject_unknown(changes, _UPDATE_FIELDS, "intent changes")
        merged = {
            "label": intent.label,
            "bbox": intent.bbox,
            "mode": intent.mode,
            "priority": intent.priority,
            "weight": intent.weight,
            "revisit_interval_min": intent.revisit_interval_min,
        }
        merged.update(changes)
        fields = self._validated_fields(merged, creating=False, supplied=changes)
        duration = changes.get("valid_duration_min")
        expires_at = intent.expires_at_min
        if duration is not None:
            expires_at = now + _positive_finite(duration, "valid_duration_min")
        updated = replace(
            intent,
            revision=intent.revision + 1,
            expires_at_min=expires_at,
            **fields,
        )
        self._intents[intent_id] = updated
        return updated

    def cancel(self, intent_id: str, expected_revision: int, now_min: float) -> Intent:
        _finite_nonnegative(now_min, "now_min")
        intent = self._current_active(intent_id, expected_revision)
        cancelled = replace(intent, revision=intent.revision + 1, lifecycle="cancelled")
        self._intents[intent_id] = cancelled
        return cancelled

    def expire(self, now_min: float) -> tuple[Intent, ...]:
        now = _finite_nonnegative(now_min, "now_min")
        expired = []
        for intent_id, intent in tuple(self._intents.items()):
            if intent.lifecycle == "active" and now >= intent.expires_at_min:
                intent = replace(intent, lifecycle="expired")
                self._intents[intent_id] = intent
                expired.append(intent)
        return tuple(expired)

    def active(self) -> tuple[Intent, ...]:
        return tuple(intent for intent in self.intents() if intent.lifecycle == "active")

    def intents(self) -> tuple[Intent, ...]:
        return tuple(self._intents.values())

    def evaluate(
        self,
        info: np.ndarray,
        last_scan: np.ndarray,
        searchable_mask: np.ndarray,
        tasks: Iterable[Any],
        now_min: float,
    ) -> tuple[IntentStatus, ...]:
        now = _finite_nonnegative(now_min, "now_min")
        info_array, scans = _metric_inputs(
            info,
            last_scan,
            searchable_mask,
            self._shape,
        )
        task_list = tuple(tasks)
        statuses = []
        for intent in self.intents():
            c0, r0, c1, r1 = intent.bbox
            # The supplied mask represents current feasibility (for example,
            # a weather closure). Intent metrics always retain the water mask
            # captured at store construction as their denominator.
            mask = self._searchable_mask[c0:c1, r0:r1]
            scan_patch = scans[c0:c1, r0:r1]
            info_patch = info_array[c0:c1, r0:r1]
            scanned = np.isfinite(scan_patch) & mask
            searchable_cells = int(mask.sum())
            scanned_cells = int(scanned.sum())
            unseen_cells = searchable_cells - scanned_cells
            ages = np.maximum(now - scan_patch, 0.0)
            if intent.revisit_interval_min is None:
                fresh = np.zeros_like(mask, dtype=bool)
            else:
                fresh = (
                    scanned
                    & (ages <= intent.revisit_interval_min)
                    & (info_patch >= self._freshness_threshold)
                )
            fresh_cells = int(fresh.sum())
            coverage = scanned_cells / searchable_cells if searchable_cells else 0.0
            freshness = fresh_cells / searchable_cells if searchable_cells else 0.0
            finite_ages = ages[scanned]
            max_age = (
                None
                if unseen_cells or not finite_ages.size
                else float(finite_ages.max())
            )
            statuses.append(IntentStatus(
                intent_id=intent.intent_id,
                revision=intent.revision,
                evaluated_at_min=now,
                searchable_cells=searchable_cells,
                scanned_cells=scanned_cells,
                unseen_cells=unseen_cells,
                fresh_cells=fresh_cells,
                coverage_ratio=coverage,
                freshness_ratio=freshness,
                max_scan_age_min=max_age,
                assigned_task_ids=_assigned_task_ids(intent.intent_id, task_list),
                unmet_reason=_unmet_reason(intent, coverage, freshness, task_list),
            ))
        return tuple(statuses)

    def _current_active(self, intent_id: str, expected_revision: int) -> Intent:
        if not isinstance(intent_id, str) or intent_id not in self._intents:
            raise ValueError("unknown intent_id")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision: expected integer")
        intent = self._intents[intent_id]
        if intent.revision != expected_revision:
            raise ValueError("intent revision conflict")
        if intent.lifecycle != "active":
            raise ValueError("intent is not active")
        return intent

    def _validated_fields(
        self,
        data: dict,
        *,
        creating: bool,
        supplied: dict | None = None,
    ) -> dict:
        label = _label(data.get("label"))
        bbox = _bbox(data.get("bbox"), self._shape, self._searchable_mask)
        mode = data.get("mode")
        if mode not in ("search_priority", "maintain_freshness"):
            raise ValueError("mode: invalid value")
        priority = data.get("priority")
        if priority not in _PRIORITY_MULTIPLIERS:
            raise ValueError("priority: invalid value")
        weight = data.get("weight", self._default_weight)
        weight = _bounded_finite(weight, "weight", 0.0, 2.0)
        revisit = data.get("revisit_interval_min")
        if mode == "maintain_freshness":
            if revisit is None and creating:
                revisit = self._default_revisit
            revisit = _positive_finite(revisit, "revisit_interval_min")
        elif revisit is not None:
            raise ValueError("revisit_interval_min is only valid for maintain_freshness")
        if creating:
            duration = data.get("valid_duration_min", self._default_duration)
            duration = _positive_finite(duration, "valid_duration_min")
        else:
            duration = None
            if supplied and "valid_duration_min" in supplied:
                duration = _positive_finite(supplied["valid_duration_min"], "valid_duration_min")
        fields = {
            "label": label,
            "bbox": bbox,
            "mode": mode,
            "priority": priority,
            "weight": weight,
            "revisit_interval_min": revisit,
        }
        if creating:
            fields["valid_duration_min"] = duration
        return fields


def build_scheduling_value(
    base_value: np.ndarray,
    intents: Iterable[Intent],
    last_scan: np.ndarray,
    now_min: float,
    searchable_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Return a weighted copy without stacking overlapping intent demand.

    The static searchable-water mask is mandatory at the call boundary. This
    helper has no map context from which it could safely infer land cells.
    """
    now = _finite_nonnegative(now_min, "now_min")
    base = np.asarray(base_value)
    scans = np.asarray(last_scan)
    if base.ndim != 2 or scans.shape != base.shape:
        raise ValueError("base_value and last_scan must be same-shape 2D arrays")
    if not np.isfinite(base).all():
        raise ValueError("base_value: expected finite values")
    if searchable_mask is None:
        raise ValueError("searchable_mask is required to weight water cells safely")
    water = np.asarray(searchable_mask, dtype=bool)
    if water.shape != base.shape:
        raise ValueError("searchable_mask must be a grid-sized matrix")
    result = np.array(base, dtype=float, copy=True)
    demand = np.zeros(base.shape, dtype=float)
    for intent in intents:
        if intent.lifecycle != "active":
            continue
        c0, r0, c1, r1 = intent.bbox
        if c0 < 0 or r0 < 0 or c1 > base.shape[0] or r1 > base.shape[1]:
            raise ValueError("intent bbox outside scheduling value")
        if intent.mode == "search_priority":
            local_demand = np.ones((c1 - c0, r1 - r0), dtype=float)
        else:
            interval = _positive_finite(intent.revisit_interval_min, "revisit_interval_min")
            local_scans = scans[c0:c1, r0:r1]
            local_demand = np.where(
                np.isfinite(local_scans),
                np.clip(np.maximum(now - local_scans, 0.0) / interval, 0.0, 1.0),
                1.0,
            )
        contribution = intent.weight * _PRIORITY_MULTIPLIERS[intent.priority] * local_demand
        local_water = water[c0:c1, r0:r1]
        demand_patch = demand[c0:c1, r0:r1]
        demand_patch[local_water] = np.maximum(
            demand_patch[local_water], contribution[local_water]
        )
    return result + demand


def _assigned_task_ids(intent_id: str, tasks: tuple[Any, ...]) -> tuple[str, ...]:
    task_ids = []
    for task in tasks:
        getter = task.get if isinstance(task, dict) else lambda name, default=None: getattr(task, name, default)
        intent_ids = getter("intent_ids", ())
        if intent_id in intent_ids:
            task_id = getter("task_id", getter("id", None))
            if isinstance(task_id, str):
                task_ids.append(task_id)
    return tuple(sorted(set(task_ids)))


def _unmet_reason(
    intent: Intent,
    coverage: float,
    freshness: float,
    tasks: tuple[Any, ...],
) -> str | None:
    if intent.lifecycle != "active":
        return intent.lifecycle
    unmet = (
        coverage < 0.9
        if intent.mode == "search_priority"
        else freshness < 0.9
    )
    if not unmet:
        return None
    if not any(_is_legal_intent_candidate(intent.intent_id, task) for task in tasks):
        return "no_legal_candidate"
    if intent.mode == "search_priority":
        return "coverage_below_target"
    if intent.mode == "maintain_freshness":
        return "freshness_below_target"
    return None


def _is_legal_intent_candidate(intent_id: str, task: Any) -> bool:
    getter = (
        task.get
        if isinstance(task, dict)
        else lambda name, default=None: getattr(task, name, default)
    )
    if intent_id not in getter("intent_ids", ()):
        return False
    if getter("kind", "search") != "search":
        return False
    return getter("status", "candidate") not in {
        "blocked", "cancelled", "completed", "failed", "infeasible",
    }


def _metric_inputs(
    info: np.ndarray,
    last_scan: np.ndarray,
    searchable_mask: np.ndarray,
    expected_shape: tuple[int, int],
):
    arrays = tuple(np.asarray(value) for value in (info, last_scan, searchable_mask))
    if (
        any(array.ndim != 2 for array in arrays)
        or len({array.shape for array in arrays}) != 1
        or arrays[0].shape != expected_shape
    ):
        raise ValueError("info, last_scan, and searchable_mask must be same-shape grid arrays")
    if not np.isfinite(arrays[0]).all():
        raise ValueError("info: expected finite values")
    return arrays[0], arrays[1]


def _reject_unknown(data: dict, allowed: set[str], context: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{context}: unknown fields: {sorted(unknown)}")


def _label(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("label: expected string")
    value = value.strip()
    if not 1 <= len(value) <= 80:
        raise ValueError("label: expected 1-80 characters")
    return value


def _bbox(value: Any, shape: tuple[int, int], searchable_mask: np.ndarray) -> tuple[int, int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError("bbox: expected four integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError("bbox: expected four integers")
    c0, r0, c1, r1 = value
    cols, rows = shape
    if not (0 <= c0 < c1 <= cols and 0 <= r0 < r1 <= rows):
        raise ValueError("bbox: outside map or empty")
    if not searchable_mask[c0:c1, r0:r1].any():
        raise ValueError("bbox: must cover searchable water")
    return c0, r0, c1, r1


def _finite_nonnegative(value: Any, name: str) -> float:
    return _bounded_finite(value, name, 0.0, math.inf)


def _positive_finite(value: Any, name: str) -> float:
    number = _finite_nonnegative(value, name)
    if number <= 0:
        raise ValueError(f"{name}: expected positive finite number")
    return number


def _bounded_finite(value: Any, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: expected number")
    number = float(value)
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f"{name}: outside allowed range")
    return number


__all__ = ["IntentStore", "build_scheduling_value"]

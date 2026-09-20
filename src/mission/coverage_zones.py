"""Pure, deterministic spatial summaries for rolling SAR coverage."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any
import math

import numpy as np

from src.mission.coverage_policy import _bbox, _last_sar, _mask_like, _time


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


@dataclass(frozen=True)
class ZoneQuotaInput:
    zone_id: str
    candidate_task_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.zone_id, str) or not self.zone_id:
            raise ValueError("zone_id must be a non-empty string")
        ids = tuple(self.candidate_task_ids)
        if any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("candidate_task_ids must contain non-empty strings")
        object.__setattr__(self, "candidate_task_ids", tuple(dict.fromkeys(ids)))


class ZonePartition:
    """Fixed tiles in [col, row] coordinates; final tiles absorb remainders."""

    def __init__(
        self, fixed_mask: np.ndarray, zone_cols: int = 3, zone_rows: int = 3
    ) -> None:
        fixed = np.asarray(fixed_mask, dtype=bool)
        if fixed.ndim != 2 or not fixed.size:
            raise ValueError("fixed_mask must be non-empty and two-dimensional")
        self.zone_cols = _positive(zone_cols, "zone_cols")
        self.zone_rows = _positive(zone_rows, "zone_rows")
        self._fixed = fixed.copy()
        self._fixed.setflags(write=False)
        self._prefix = (
            np.pad(fixed.astype(np.int64), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        )
        self._boxes, self._cells = {}, {}
        cols, rows = fixed.shape
        # More partitions than cells produce empty tiles, which are omitted.
        ce = [i * (cols // self.zone_cols) for i in range(self.zone_cols)] + [cols]
        re = [i * (rows // self.zone_rows) for i in range(self.zone_rows)] + [rows]
        for col in range(self.zone_cols):
            for row in range(self.zone_rows):
                box = (ce[col], re[row], ce[col + 1], re[row + 1])
                cells = tuple(
                    (c, r)
                    for c in range(box[0], box[2])
                    for r in range(box[1], box[3])
                    if fixed[c, r]
                )
                if cells:
                    key = f"zone:{col}:{row}"
                    self._boxes[key], self._cells[key] = box, cells

    @property
    def fixed_mask(self) -> np.ndarray:
        return self._fixed.copy()

    @property
    def zone_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._cells))

    def zone_bbox(self, zone_id: str) -> tuple[int, int, int, int]:
        return self._boxes[zone_id]

    def zone_cells(self, zone_id: str) -> tuple[tuple[int, int], ...]:
        return self._cells[zone_id]

    def contains_bbox(self, zone_id: str, bbox: tuple[int, int, int, int]) -> bool:
        c0, r0, c1, r1 = _bbox(bbox, "zone")
        a, b, c, d = self._boxes[zone_id]
        return a <= c0 < c1 <= c and b <= r0 < r1 <= d

    def zone_of_bbox(self, bbox: tuple[int, int, int, int]) -> str | None:
        c0, r0, c1, r1 = _bbox(bbox, "zone")
        overlaps = []
        for zone in self.zone_ids:
            a, b, c, d = self._boxes[zone]
            left, bottom, right, top = max(a, c0), max(b, r0), min(c, c1), min(d, r1)
            count = 0
            if left < right and bottom < top:
                p = self._prefix
                count = int(
                    p[right, top] - p[left, top] - p[right, bottom] + p[left, bottom]
                )
            overlaps.append((count, zone))
        return (
            min(overlaps, key=lambda pair: (-pair[0], pair[1]))[1]
            if any(count for count, _ in overlaps)
            else None
        )


def build_zone_coverage_summary(
    zones: ZonePartition,
    *,
    now_min: float,
    last_sar: np.ndarray,
    feasible_mask: np.ndarray,
    primary_window_min: int,
    in_flight_tasks: Iterable[Any] = (),
) -> dict:
    """Summarize actual SAR freshness separately from assigned future coverage.

    The caller filters failed UAVs; this pure function filters task lifecycle,
    assignment and kind. Global percentages use the fixed domain denominator.
    """
    now = _time(now_min, "now_min")
    window = _positive(primary_window_min, "primary_window_min")
    fixed = zones.fixed_mask
    last = _last_sar(last_sar, fixed.shape, now)
    feasible = _mask_like(feasible_mask, fixed.shape, "feasible_mask")
    boxes = [
        _bbox(task.bbox, task.task_id)
        for task in in_flight_tasks
        if task.status in {"approved", "executing"}
        and task.assigned_uav_id is not None
        and task.kind in {"search", "direction_search", "investigation"}
        and task.bbox is not None
    ]
    rows = []
    for zone in zones.zone_ids:
        cells = np.asarray(zones.zone_cells(zone))
        times = last[cells[:, 0], cells[:, 1]]
        unseen = ~np.isfinite(times)
        overdue = ~unseen & (times <= now - window)
        masks = [
            (cells[:, 0] >= a)
            & (cells[:, 0] < c)
            & (cells[:, 1] >= b)
            & (cells[:, 1] < d)
            for a, b, c, d in boxes
        ]
        inflight = (
            np.logical_or.reduce(masks) if masks else np.zeros(len(cells), dtype=bool)
        )
        rows.append(
            {
                "zone_id": zone,
                "bbox": list(zones.zone_bbox(zone)),
                "fixed_cells": len(cells),
                "searchable_cells": int(feasible[cells[:, 0], cells[:, 1]].sum()),
                "unseen_cells": int(unseen.sum()),
                "overdue_cells": int(overdue.sum()),
                "gap_fraction": float((unseen | overdue).mean()),
                "effective_gap_fraction": float(
                    ((unseen | overdue) & ~inflight).mean()
                ),
                "in_flight_search_tasks": sum(bool(mask.any()) for mask in masks),
                "in_flight_cells": int(inflight.sum()),
            }
        )
    total = sum(row["fixed_cells"] for row in rows)
    unseen = 100 * sum(row["unseen_cells"] for row in rows) / total if total else 0.0
    overdue = 100 * sum(row["overdue_cells"] for row in rows) / total if total else 0.0
    return {
        "schema_version": "zone-coverage-summary/v1",
        "as_of_min": now,
        "zone_cols": zones.zone_cols,
        "zone_rows": zones.zone_rows,
        "gap_pct": (
            100
            * sum(row["unseen_cells"] + row["overdue_cells"] for row in rows)
            / total
            if total
            else 0.0
        ),
        "unseen_pct": unseen,
        "overdue_pct": overdue,
        "zones": rows,
    }


def build_zone_quota_inputs(
    zones: ZonePartition,
    summary: Mapping[str, Any],
    window_tasks: Iterable[Any],
    *,
    threshold: float,
    max_slots: int | None = None,
) -> tuple[ZoneQuotaInput, ...]:
    """Prioritize deficit zones, using only fully contained visible rectangles."""
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, Real)
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("threshold must be in [0, 1]")
    if max_slots is not None and (
        isinstance(max_slots, bool)
        or not isinstance(max_slots, Integral)
        or max_slots < 0
    ):
        raise ValueError("max_slots must be a non-negative integer")
    deficit = sorted(
        (row for row in summary["zones"] if row["effective_gap_fraction"] >= threshold),
        key=lambda row: (-row["effective_gap_fraction"], row["zone_id"]),
    )
    tasks = tuple(window_tasks)
    return tuple(
        ZoneQuotaInput(
            row["zone_id"],
            tuple(
                task.task_id
                for task in tasks
                if task.kind == "search"
                and task.bbox is not None
                and zones.contains_bbox(row["zone_id"], task.bbox)
            ),
        )
        for row in deficit[:max_slots]
    )

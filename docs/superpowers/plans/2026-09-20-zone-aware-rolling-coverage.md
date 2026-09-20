# Zone-Aware Rolling Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the mission decision-maker from "pick a few pre-generated small rectangles" into a rolling whole-domain coverage planner: per-zone coverage summary in the prompt, spatially dispersed candidate window, gap-adaptive exact-count search budget anchored on available UAVs (restoring a0e7752's parallel fill-all semantics), and a validator-enforced per-zone quota that spreads the selected sub-regions across deficit zones.

**Architecture:** New pure module `src/mission/coverage_zones.py` (zone partition + summary + quota inputs); `coverage_policy.py` gains the adaptive fraction, the exact-count/zone-aware constraint builder, and zone-rotated window selection; `contracts.py` extends `CoverageConstraint` (optional `zone_requirements`/`zone_infeasible`) and `MissionSnapshot` (`coverage_summary`); `task_allocator.py` wires summary → fraction → quota → constraint per decision; `mission_scheduler.py` enforces the exact count and zone quotas in validation and embeds the summary in the prompt payload; the system prompt becomes the "rolling whole-domain coverage planner" and README chapter 2 is corrected to the real architecture.

**Tech Stack:** Python 3, numpy, dataclasses; pytest for tests; existing `CoveragePolicy`/`CoverageMetrics`/`MissionScheduler` patterns; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-20-zone-aware-rolling-coverage-design.md` — the plan argues from the spec; executors read both.

## Global Constraints

- `CoverageConfig` additions (verbatim defaults): `zone_cols: int = 3`, `zone_rows: int = 3`, `search_uav_fraction_max: float = 1.0`, `zone_quota_gap_threshold: float = 0.5`; keep `min_search_uav_fraction: float = 0.4` as the lower bound.
- `fraction = min + (max − min) × gap_pct/100`, clamped; `gap_pct = unseen_pct + overdue_seen_pct` (from `CoverageMetrics.snapshot`; `None` → 0).
- `required_raw = ceil(available_count × fraction)`; `required_new_search_count = min(required_raw, matched_search_slots)`; `infeasible_reason = "insufficient_available_resources"` when `matched_search_slots < required_raw`. The count is EXACT (not a floor) whenever `required_new_search_count > 0` and no `infeasible_reason` — enforced only over candidates with `kind == "search"` and `bbox is not None` (NOT over `direction_search`/`investigation`, which are urgent information work).
- Zone quota: deficit zones = `effective_gap_fraction ≥ zone_quota_gap_threshold`, sorted by gap desc then `zone_id` asc, capped at `required_raw` slots; quota representatives must be window tasks with `kind == "search"` whose bbox is fully contained in the zone (`contains_bbox`). Quota candidates visit the max matching BEFORE global representatives (single matching, no double-booking).
- Hard constraints that MUST stay byte-identical in behavior: no invented bboxes, no overlapping selected search rectangles, feasible-edge/range/generation legality, preemption protection (returning/refueling/safety/probe/tracking), reassignment cooldown, notes ≤ 160 chars, prompt payload ≤ 100 KB, `zones=None` / empty `zone_requirements` paths behave exactly as today.
- Environment: run tests from the main workspace (it has `configs/.env`); fresh git worktrees lack it and ~56 LLM tests fail with a misleading `LLMConfigurationError` — copy `configs/.env` into the worktree if working in one.
- Commit messages follow the repo style (`feat:`/`test:`/`docs:`), one task per commit, with the Co-Authored-By footer per session rules.

## File Structure

| File | Responsibility |
|---|---|
| `src/mission/coverage_zones.py` (new) | `ZonePartition` (fixed-mask zone grid, attribution, containment), `build_zone_coverage_summary` (per-zone unseen/overdue/in-flight + effective gap + aggregates), `build_zone_quota_inputs`, `ZoneQuotaInput`. Pure functions, no scheduler state. |
| `src/mission/coverage_policy.py` | `adaptive_search_fraction`; `build_coverage_constraint` rewritten: available-anchored exact count, `zone_requirements_input` param, single prioritized matching, emits `zone_requirements`/`zone_infeasible`; `select_window(..., zones=None)` zone-rotated representative pass. |
| `src/mission/contracts.py` | `ZoneCoverageRequirement`; `CoverageConstraint.zone_requirements`/`zone_infeasible` (default empty); `MissionSnapshot.coverage_summary` (default `None`). |
| `src/schedule/task_allocator.py` | `build_mission_snapshot` wires: zone summary → adaptive fraction → quota inputs → constraint → snapshot `coverage_summary`; `_coverage_prompt_window` passes `zones` to `select_window`. |
| `src/mission/mission_scheduler.py` | `_validate_selection`: exact-count check + zone quota checks; `_prompt_payload`: embed `coverage_summary`. |
| `src/mission/prompts/mission_scheduler.txt` | Rolling whole-domain coverage planner role + coverage_summary/constraint instructions (hard constraints preserved). |
| `README.md` | Chapter 2 corrected to the real architecture (program-generated geometry + LLM cross-zone selection + deterministic pairing). |
| `tests/mission/test_coverage_zones.py` (new) | Partition invariants, attribution, containment, summary math, quota inputs. |
| `tests/mission/test_coverage_zone_quota.py` (new) | Adaptive fraction, exact-count builder, zone quota priority/degradation. |
| `tests/mission/test_coverage_prompt_window.py` | Rotation tests + updated constraint/validator tests (old semantics replaced). |
| `tests/mission/test_contracts.py` | `ZoneCoverageRequirement`/`CoverageConstraint`/`coverage_summary` validation tests. |
| `tests/mission/test_zone_rolling_acceptance.py` (new) | Spec section 9 acceptance scenarios 1–3 composed deterministically. |

Import direction (no cycles): `coverage_policy` → `coverage_zones`; `task_allocator` → both; `mission_scheduler` → `contracts` only.

---

### Task 1: Zone partition, zone summary, quota inputs (`coverage_zones.py`)

**Files:**
- Create: `src/mission/coverage_zones.py`
- Test: `tests/mission/test_coverage_zones.py`

**Interfaces:**
- Consumes: `numpy`; `src.schedule` types are NOT imported (tasks passed as duck-typed objects with `.task_id`/`.kind`/`.bbox`).
- Produces (used by Tasks 2, 4, 5):
  - `class ZonePartition(fixed_mask, zone_cols, zone_rows)` with `zone_ids: tuple[str,...]`, `zone_bbox(zone_id) -> tuple[int,int,int,int]`, `zone_cells(zone_id) -> tuple[tuple[int,int],...]`, `zone_of_bbox(bbox) -> str | None`, `contains_bbox(zone_id, bbox) -> bool`, `fixed_mask` property.
  - `ZoneQuotaInput(zone_id: str, candidate_task_ids: tuple[str, ...])` (frozen dataclass).
  - `build_zone_coverage_summary(zones, *, now_min, last_sar, feasible_mask, primary_window_min, in_flight_tasks=(), global_snapshot=None) -> dict` with keys `schema_version="zone-coverage-summary/v1"`, `as_of_min`, `zone_cols`, `zone_rows`, `gap_pct`, `unseen_pct`, `overdue_pct`, `zones:[{zone_id, bbox, fixed_cells, searchable_cells, unseen_cells, overdue_cells, gap_fraction, effective_gap_fraction, in_flight_search_tasks, in_flight_cells}]`.
  - `build_zone_quota_inputs(zones, summary, window_tasks, *, threshold, max_slots=None) -> tuple[ZoneQuotaInput, ...]`.

- [ ] **Step 1: Write the failing tests** — create `tests/mission/test_coverage_zones.py`:

```python
import numpy as np
import pytest

from src.mission.coverage_zones import (
    ZonePartition,
    ZoneQuotaInput,
    build_zone_coverage_summary,
    build_zone_quota_inputs,
)


class _Task:
    def __init__(self, task_id, kind="search", bbox=None):
        self.task_id = task_id
        self.kind = kind
        self.bbox = bbox


def test_partition_covers_every_fixed_cell_exactly_once():
    fixed = np.ones((6, 6), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=2)
    assert zones.zone_ids == (
        "zone:0:0", "zone:0:1", "zone:1:0", "zone:1:1", "zone:2:0", "zone:2:1",
    )
    covered = np.zeros_like(fixed, dtype=bool)
    for zone_id in zones.zone_ids:
        for col, row in zones.zone_cells(zone_id):
            assert not covered[col, row]
            covered[col, row] = True
    assert np.array_equal(covered, fixed)
    assert np.array_equal(zones.fixed_mask, fixed)


def test_partition_omits_zones_without_fixed_cells():
    fixed = np.ones((6, 6), dtype=bool)
    fixed[:, 3:] = False  # right half is land
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=1)
    assert zones.zone_ids == ("zone:0:0", "zone:1:0")
    assert zones.zone_bbox("zone:0:0") == (0, 0, 2, 6)


def test_partition_rejects_invalid_config():
    with pytest.raises(ValueError):
        ZonePartition(np.ones((6, 6), dtype=bool), zone_cols=0, zone_rows=2)
    with pytest.raises(ValueError):
        ZonePartition(np.ones((6, 6), dtype=bool), zone_cols=2.5, zone_rows=2)
    with pytest.raises(ValueError):
        ZonePartition(np.zeros((0, 0), dtype=bool), zone_cols=2, zone_rows=2)


def test_zone_of_bbox_attributes_by_max_overlap_with_zone_id_tiebreak():
    zones = ZonePartition(np.ones((6, 6), dtype=bool), zone_cols=2, zone_rows=2)
    # bbox (2,0,5,3) overlaps zone:0:0 by 3 cells and zone:1:0 by 6 cells
    assert zones.zone_of_bbox((2, 0, 5, 3)) == "zone:1:0"
    assert zones.zone_of_bbox((0, 0, 3, 3)) == "zone:0:0"
    assert zones.zone_of_bbox((3, 3, 6, 6)) == "zone:1:1"


def test_contains_bbox_requires_full_containment():
    zones = ZonePartition(np.ones((6, 6), dtype=bool), zone_cols=2, zone_rows=2)
    assert zones.contains_bbox("zone:0:0", (0, 0, 3, 3))
    assert not zones.contains_bbox("zone:0:0", (2, 0, 5, 3))  # crosses zone border


def test_zone_summary_counts_unseen_overdue_and_inflight():
    fixed = np.ones((6, 6), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=2)
    last_sar = np.full((6, 6), -np.inf)
    last_sar[0, 0] = 95.0  # fresh (now=100, window=60)
    last_sar[0, 1] = 20.0  # overdue
    last_sar[1, 0] = 20.0  # overdue
    feasible = np.ones((6, 6), dtype=bool)
    feasible[0, 5] = False  # weather on one zone:0:0 cell

    summary = build_zone_coverage_summary(
        zones,
        now_min=100.0,
        last_sar=last_sar,
        feasible_mask=feasible,
        primary_window_min=60,
        in_flight_tasks=(_Task("inflight", bbox=(0, 0, 2, 1)),),
        global_snapshot=None,
    )

    assert summary["schema_version"] == "zone-coverage-summary/v1"
    assert summary["as_of_min"] == 100.0
    zone00 = next(z for z in summary["zones"] if z["zone_id"] == "zone:0:0")
    assert zone00["bbox"] == [0, 0, 2, 6]
    assert zone00["fixed_cells"] == 12
    assert zone00["searchable_cells"] == 11
    assert zone00["unseen_cells"] == 9
    assert zone00["overdue_cells"] == 2
    assert zone00["gap_fraction"] == pytest.approx(11 / 12)
    # in-flight bbox (0,0,2,1) covers cells (0,0) fresh and (1,0) overdue;
    # only the overdue one reduces the effective gap
    assert zone00["in_flight_search_tasks"] == 1
    assert zone00["in_flight_cells"] == 2
    assert zone00["effective_gap_fraction"] == pytest.approx(10 / 12)
    # aggregate is computed from per-zone counts when no global snapshot:
    # 36 fixed cells total, 3 finite (1 fresh, 2 overdue) -> 33 unseen
    assert summary["gap_pct"] == pytest.approx(100.0 * 35 / 36)
    assert summary["unseen_pct"] == pytest.approx(100.0 * 33 / 36)
    assert summary["overdue_pct"] == pytest.approx(100.0 * 2 / 36)


def test_zone_summary_prefers_global_snapshot_aggregates():
    fixed = np.ones((6, 6), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=2)
    summary = build_zone_coverage_summary(
        zones,
        now_min=100.0,
        last_sar=np.full((6, 6), -np.inf),
        feasible_mask=np.ones((6, 6), dtype=bool),
        primary_window_min=60,
        global_snapshot={"unseen_pct": 80.0, "overdue_seen_pct": 7.5},
    )
    assert summary["gap_pct"] == pytest.approx(87.5)
    assert summary["unseen_pct"] == pytest.approx(80.0)
    assert summary["overdue_pct"] == pytest.approx(7.5)


def test_zone_summary_validates_shapes_and_time():
    zones = ZonePartition(np.ones((6, 6), dtype=bool), zone_cols=3, zone_rows=2)
    with pytest.raises(ValueError):
        build_zone_coverage_summary(
            zones, now_min=100.0,
            last_sar=np.full((5, 6), -np.inf),
            feasible_mask=np.ones((6, 6), dtype=bool),
            primary_window_min=60,
        )
    with pytest.raises(ValueError):
        build_zone_coverage_summary(
            zones, now_min=-1.0,
            last_sar=np.full((6, 6), -np.inf),
            feasible_mask=np.ones((6, 6), dtype=bool),
            primary_window_min=60,
        )
    with pytest.raises(ValueError):
        build_zone_coverage_summary(
            zones, now_min=100.0,
            last_sar=np.full((6, 6), -np.inf),
            feasible_mask=np.ones((6, 6), dtype=bool),
            primary_window_min=0,
        )


def test_quota_inputs_sort_deficit_zones_and_require_full_containment():
    fixed = np.ones((9, 3), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=1)  # three 3x3 zones
    last_sar = np.full((9, 3), -np.inf)
    last_sar[3:, :] = 95.0   # zones 1 and 2 mostly fresh
    last_sar[3:5, :] = -np.inf  # zone:1:0 keeps 6 unseen cells (gap 0.667)
    summary = build_zone_coverage_summary(
        zones, now_min=100.0, last_sar=last_sar,
        feasible_mask=np.ones((9, 3), dtype=bool), primary_window_min=60,
    )
    window = [
        _Task("z0", bbox=(0, 0, 2, 3)),    # fully in zone:0:0
        _Task("z1", bbox=(3, 0, 5, 3)),    # fully in zone:1:0
        _Task("span", bbox=(2, 0, 4, 3)),  # crosses zone:0:0 / zone:1:0
        _Task("probe", "probe", bbox=None),
    ]
    inputs = build_zone_quota_inputs(zones, summary, window, threshold=0.5)
    assert [item.zone_id for item in inputs] == ["zone:0:0", "zone:1:0"]
    assert inputs[0].candidate_task_ids == ("z0",)
    assert inputs[1].candidate_task_ids == ("z1",)
    assert "span" not in inputs[0].candidate_task_ids + inputs[1].candidate_task_ids


def test_quota_inputs_cap_slots_and_reject_bad_arguments():
    fixed = np.ones((9, 3), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=1)
    last_sar = np.full((9, 3), -np.inf)
    summary = build_zone_coverage_summary(
        zones, now_min=100.0, last_sar=last_sar,
        feasible_mask=np.ones((9, 3), dtype=bool), primary_window_min=60,
    )
    window = [
        _Task("z0", bbox=(0, 0, 2, 3)),
        _Task("z1", bbox=(3, 0, 5, 3)),
        _Task("z2", bbox=(6, 0, 8, 3)),
    ]
    capped = build_zone_quota_inputs(zones, summary, window, threshold=0.5, max_slots=2)
    assert [item.zone_id for item in capped] == ["zone:0:0", "zone:1:0"]
    with pytest.raises(ValueError):
        build_zone_quota_inputs(zones, summary, window, threshold=0.5, max_slots=-1)
    with pytest.raises(ValueError):
        build_zone_quota_inputs(zones, summary, window, threshold=1.5)


def test_zone_quota_input_validates_identity():
    with pytest.raises(ValueError):
        ZoneQuotaInput("", ("S1",))
    assert ZoneQuotaInput("z", ("S1", "S1")).candidate_task_ids == ("S1",)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/mission/test_coverage_zones.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.mission.coverage_zones'`.

- [ ] **Step 3: Write the implementation** — create `src/mission/coverage_zones.py`:

```python
"""Deterministic whole-domain zone partition and coverage gap summary."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any

import numpy as np

__all__ = [
    "ZonePartition",
    "ZoneQuotaInput",
    "build_zone_coverage_summary",
    "build_zone_quota_inputs",
]


@dataclass(frozen=True)
class ZoneQuotaInput:
    """One deficit zone's in-window ordinary search candidates."""

    zone_id: str
    candidate_task_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.zone_id, str) or not self.zone_id:
            raise ValueError("zone_id: expected a non-empty string")
        if any(not isinstance(item, str) or not item for item in self.candidate_task_ids):
            raise ValueError("candidate_task_ids: expected non-empty strings")
        object.__setattr__(
            self, "candidate_task_ids", tuple(dict.fromkeys(self.candidate_task_ids)),
        )


class ZonePartition:
    """Split a fixed SAR domain into a uniform grid of rectangular zones."""

    def __init__(self, fixed_mask: np.ndarray, zone_cols: int, zone_rows: int) -> None:
        fixed = np.asarray(fixed_mask, dtype=bool)
        if fixed.ndim != 2 or not fixed.size:
            raise ValueError("fixed_mask: expected a non-empty two-dimensional mask")
        self._zone_cols = _positive_int(zone_cols, "zone_cols")
        self._zone_rows = _positive_int(zone_rows, "zone_rows")
        self._fixed = fixed.copy()
        self._fixed.setflags(write=False)
        cols, rows = fixed.shape
        col_edges = _split_edges(cols, self._zone_cols)
        row_edges = _split_edges(rows, self._zone_rows)
        self._zone_cells: dict[str, tuple[tuple[int, int], ...]] = {}
        self._zone_bbox: dict[str, tuple[int, int, int, int]] = {}
        for col_index in range(self._zone_cols):
            for row_index in range(self._zone_rows):
                c0, c1 = col_edges[col_index], col_edges[col_index + 1]
                r0, r1 = row_edges[row_index], row_edges[row_index + 1]
                cells = tuple(
                    (col, row)
                    for col in range(c0, c1)
                    for row in range(r0, r1)
                    if self._fixed[col, row]
                )
                if not cells:
                    continue
                zone_id = f"zone:{col_index}:{row_index}"
                self._zone_cells[zone_id] = cells
                self._zone_bbox[zone_id] = (c0, r0, c1, r1)

    @property
    def fixed_mask(self) -> np.ndarray:
        """Return a detached copy of the fixed responsibility domain."""
        return self._fixed.copy()

    @property
    def zone_cols(self) -> int:
        return self._zone_cols

    @property
    def zone_rows(self) -> int:
        return self._zone_rows

    @property
    def zone_ids(self) -> tuple[str, ...]:
        """Zone ids in row-major order; zones without fixed cells are omitted."""
        return tuple(self._zone_cells)

    def zone_bbox(self, zone_id: str) -> tuple[int, int, int, int]:
        return self._zone_bbox[zone_id]

    def zone_cells(self, zone_id: str) -> tuple[tuple[int, int], ...]:
        return self._zone_cells[zone_id]

    def zone_of_bbox(self, bbox: Any) -> str | None:
        """Attribute a bbox to the zone with the most overlapping fixed cells.

        Ties break by ascending zone_id. A bbox overlapping no fixed cell
        returns None.
        """
        c0, r0, c1, r1 = _bbox(bbox)
        best_id: str | None = None
        best_overlap = 0
        for zone_id in self._zone_cells:
            overlap = sum(
                1
                for col, row in self._zone_cells[zone_id]
                if c0 <= col < c1 and r0 <= row < r1
            )
            if overlap == 0:
                continue
            if best_id is None or overlap > best_overlap or (
                overlap == best_overlap and zone_id < best_id
            ):
                best_id, best_overlap = zone_id, overlap
        return best_id

    def contains_bbox(self, zone_id: str, bbox: Any) -> bool:
        """Return True when the bbox lies entirely inside the zone tile."""
        zc0, zr0, zc1, zr1 = self._zone_bbox[zone_id]
        c0, r0, c1, r1 = _bbox(bbox)
        return zc0 <= c0 and zr0 <= r0 and c1 <= zc1 and r1 <= zr1


def build_zone_coverage_summary(
    zones: ZonePartition,
    *,
    now_min: float,
    last_sar: np.ndarray,
    feasible_mask: np.ndarray,
    primary_window_min: int,
    in_flight_tasks: Iterable[Any] = (),
    global_snapshot: Mapping[str, object] | None = None,
) -> dict:
    """Per-zone unscanned / overdue / in-flight coverage summary (R1, R5)."""
    now = _time(now_min, "now_min")
    window = _positive_int(primary_window_min, "primary_window_min")
    last = np.asarray(last_sar, dtype=float)
    feasible = np.asarray(feasible_mask, dtype=bool)
    shape = zones.fixed_mask.shape
    if last.shape != shape or feasible.shape != shape:
        raise ValueError("last_sar and feasible_mask must match fixed_mask shape")
    if np.isnan(last).any() or np.isposinf(last).any():
        raise ValueError("last_sar must contain finite values or -inf")

    in_flight_boxes = [
        _bbox(getattr(task, "bbox", None))
        for task in in_flight_tasks
        if getattr(task, "bbox", None) is not None
    ]

    def cell_inside_boxes(col: int, row: int) -> bool:
        return any(
            c0 <= col < c1 and r0 <= row < r1
            for c0, r0, c1, r1 in in_flight_boxes
        )

    zone_rows: list[dict] = []
    for zone_id in zones.zone_ids:
        cells = zones.zone_cells(zone_id)
        fixed_count = len(cells)
        unseen = 0
        overdue = 0
        in_flight_cells = 0
        covered_gap = 0
        searchable = 0
        for col, row in cells:
            if feasible[col, row]:
                searchable += 1
            timestamp = last[col, row]
            is_gap = not math.isfinite(timestamp) or timestamp <= now - window
            if not math.isfinite(timestamp):
                unseen += 1
            elif is_gap:
                overdue += 1
            if cell_inside_boxes(col, row):
                in_flight_cells += 1
                if is_gap:
                    covered_gap += 1
        gap = unseen + overdue
        zone_rows.append({
            "zone_id": zone_id,
            "bbox": list(zones.zone_bbox(zone_id)),
            "fixed_cells": fixed_count,
            "searchable_cells": searchable,
            "unseen_cells": unseen,
            "overdue_cells": overdue,
            "gap_fraction": gap / fixed_count,
            "effective_gap_fraction": (gap - covered_gap) / fixed_count,
            "in_flight_search_tasks": sum(
                1
                for box in in_flight_boxes
                if any(
                    box[0] <= col < box[2] and box[1] <= row < box[3]
                    for col, row in cells
                )
            ),
            "in_flight_cells": in_flight_cells,
        })

    if global_snapshot is not None:
        unseen_pct = float(global_snapshot.get("unseen_pct") or 0.0)
        overdue_pct = float(global_snapshot.get("overdue_seen_pct") or 0.0)
        gap_pct = unseen_pct + overdue_pct
    else:
        fixed_total = sum(row["fixed_cells"] for row in zone_rows)
        if fixed_total:
            unseen_pct = 100.0 * sum(row["unseen_cells"] for row in zone_rows) / fixed_total
            overdue_pct = 100.0 * sum(row["overdue_cells"] for row in zone_rows) / fixed_total
            gap_pct = unseen_pct + overdue_pct
        else:
            unseen_pct = overdue_pct = gap_pct = 0.0

    return {
        "schema_version": "zone-coverage-summary/v1",
        "as_of_min": now,
        "zone_cols": zones.zone_cols,
        "zone_rows": zones.zone_rows,
        "gap_pct": gap_pct,
        "unseen_pct": unseen_pct,
        "overdue_pct": overdue_pct,
        "zones": zone_rows,
    }


def build_zone_quota_inputs(
    zones: ZonePartition,
    summary: Mapping[str, object],
    window_tasks: Iterable[Any],
    *,
    threshold: float,
    max_slots: int | None = None,
) -> tuple[ZoneQuotaInput, ...]:
    """Deficit zones (effective gap >= threshold), gap-desc, capped at max_slots."""
    if not isinstance(threshold, Real) or isinstance(threshold, bool):
        raise ValueError("threshold: expected a finite number in [0, 1]")
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold: expected a finite number in [0, 1]")
    if max_slots is not None:
        max_slots = _non_negative_int(max_slots, "max_slots")

    deficit = [
        zone
        for zone in summary["zones"]
        if float(zone["effective_gap_fraction"]) >= threshold
    ]
    deficit.sort(
        key=lambda item: (-float(item["effective_gap_fraction"]), item["zone_id"]),
    )
    if max_slots is not None:
        deficit = deficit[:max_slots]

    inputs = []
    for zone in deficit:
        zone_id = zone["zone_id"]
        candidates = tuple(
            getattr(task, "task_id")
            for task in window_tasks
            if getattr(task, "kind", "search") == "search"
            and getattr(task, "bbox", None) is not None
            and zones.contains_bbox(zone_id, task.bbox)
        )
        inputs.append(ZoneQuotaInput(zone_id, candidates))
    return tuple(inputs)


def _split_edges(length: int, parts: int) -> tuple[int, ...]:
    return tuple(round(index * length / parts) for index in range(parts + 1))


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError(f"{name}: expected a positive integer")
    return int(value)


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError(f"{name}: expected a non-negative integer")
    return int(value)


def _time(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name}: expected a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name}: expected a finite non-negative number")
    return result


def _bbox(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError("bbox: expected four integers")
    values = tuple(value)
    if any(isinstance(item, bool) or not isinstance(item, Integral) for item in values):
        raise ValueError("bbox: expected four integers")
    c0, r0, c1, r1 = (int(item) for item in values)
    if c0 >= c1 or r0 >= r1:
        raise ValueError("bbox: expected a non-empty half-open rectangle")
    return c0, r0, c1, r1
```

Note: `test_quota_inputs_cap_slots_and_reject_bad_arguments` expects `threshold=1.5` to raise — the validation above rejects it. `test_zone_summary_validates_shapes_and_time` expects `primary_window_min=0` to raise — `_positive_int` rejects it. `now_min=-1.0` — `_time` rejects it. ✅

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/mission/test_coverage_zones.py -q`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mission/coverage_zones.py tests/mission/test_coverage_zones.py
git commit -m "feat: add zone partition, coverage gap summary and quota inputs"
```

---

### Task 2: Adaptive fraction + exact-count zone-aware constraint builder

**Files:**
- Modify: `src/mission/coverage_policy.py` (add `adaptive_search_fraction`; rewrite `build_coverage_constraint`; add import of `ZoneQuotaInput` from `src.mission.coverage_zones` — see below)
- Test: `tests/mission/test_coverage_zone_quota.py` (new); `tests/mission/test_coverage_prompt_window.py` (replace two obsolete tests)

**Interfaces:**
- Consumes: `ZoneQuotaInput`, `ZoneCoverageRequirement`/`CoverageConstraint` (from Task 3 — see note in Step 3: create `ZoneCoverageRequirement` in contracts.py FIRST within this task's implementation step, because the constraint builder imports it; the dedicated contract tests live in Task 3).
- Produces:
  - `adaptive_search_fraction(gap_pct: float | None, *, min_fraction: float = 0.4, max_fraction: float = 1.0) -> float`
  - `build_coverage_constraint(*, available_ids, representatives, edges, fraction, active_search_count=0, zone_requirements_input=()) -> CoverageConstraint` — new semantics: `desired_search_count = ceil(len(available) × fraction)`; `required_new_search_count = min(desired, matched_slots)` (EXACT target); `must_service_task_ids` = matched zone candidates when any zone matched, else the first matched representative (legacy); `zone_requirements` = matched deficit zones; `zone_infeasible` = unmatched zones with reason.

- [ ] **Step 1: Write the failing tests** — create `tests/mission/test_coverage_zone_quota.py`:

```python
import numpy as np
import pytest

from src.mission.coverage_policy import (
    adaptive_search_fraction,
    build_coverage_constraint,
)
from src.mission.coverage_zones import ZoneQuotaInput
from src.mission.contracts import FeasibleEdge


def _edge(task_id, uav_id):
    return FeasibleEdge(task_id, uav_id, 1.0, 1.0, 1.0, 0.0, f"{uav_id}:{task_id}")


def test_adaptive_fraction_monotone_and_clamped():
    assert adaptive_search_fraction(0.0) == 0.4
    assert adaptive_search_fraction(100.0) == 1.0
    assert adaptive_search_fraction(50.0) == pytest.approx(0.7)
    assert adaptive_search_fraction(None) == 0.4
    assert adaptive_search_fraction(100.0, min_fraction=0.3, max_fraction=0.9) == pytest.approx(0.9)
    with pytest.raises(ValueError):
        adaptive_search_fraction(50.0, min_fraction=0.8, max_fraction=0.4)


def test_constraint_anchors_desired_on_available_uavs():
    constraint = build_coverage_constraint(
        available_ids=("U1", "U2"),
        representatives=("S1", "S2", "S3"),
        edges=(_edge("S1", "U1"), _edge("S2", "U1"), _edge("S3", "U2")),
        fraction=1.0,
    )
    assert constraint.desired_search_count == 2
    assert constraint.required_new_search_count == 2
    assert constraint.must_service_task_ids == ("S1",)
    assert constraint.infeasible_reason is None
    assert constraint.zone_requirements == ()
    assert constraint.zone_infeasible == ()


def test_constraint_rounds_desired_and_scales_with_fraction():
    constraint = build_coverage_constraint(
        available_ids=("U1", "U2", "U3"),
        representatives=("S1", "S2", "S3"),
        edges=(_edge("S1", "U1"), _edge("S2", "U2"), _edge("S3", "U3")),
        fraction=0.4,
    )
    assert constraint.desired_search_count == 2  # ceil(3 * 0.4)
    assert constraint.required_new_search_count == 2
    assert constraint.infeasible_reason is None


def test_constraint_infeasible_when_matching_shorter_than_desired():
    constraint = build_coverage_constraint(
        available_ids=("U1", "U2", "U3"),
        representatives=("S1", "S2"),
        edges=(_edge("S1", "U1"), _edge("S2", "U2")),
        fraction=1.0,
    )
    assert constraint.desired_search_count == 3
    assert constraint.required_new_search_count == 2
    assert constraint.infeasible_reason == "insufficient_available_resources"


def test_constraint_zone_quota_visits_before_global_representatives():
    constraint = build_coverage_constraint(
        available_ids=("U1", "U2"),
        representatives=("S1",),
        edges=(_edge("S1", "U1"), _edge("Z1", "U1"), _edge("Z2", "U2")),
        fraction=1.0,
        zone_requirements_input=(
            ZoneQuotaInput("zone:0:0", ("Z1",)),
            ZoneQuotaInput("zone:1:0", ("Z2",)),
        ),
    )
    # Z1 takes U1 first, so S1 cannot be matched; Z2 takes U2.
    assert constraint.required_new_search_count == 2
    assert constraint.must_service_task_ids == ("Z1", "Z2")
    assert "Z1" in constraint.representative_task_ids
    requirements = {item.zone_id: item for item in constraint.zone_requirements}
    assert set(requirements) == {"zone:0:0", "zone:1:0"}
    assert requirements["zone:0:0"].must_service_task_ids == ("Z1",)
    assert requirements["zone:0:0"].infeasible_reason is None
    assert constraint.zone_infeasible == ()


def test_constraint_drops_unmatched_zones_with_audit_reasons():
    constraint = build_coverage_constraint(
        available_ids=("U1",),
        representatives=(),
        edges=(_edge("Z1", "U1"),),
        fraction=1.0,
        zone_requirements_input=(
            ZoneQuotaInput("zone:0:0", ("Z1",)),
            ZoneQuotaInput("zone:1:0", ("Z2",)),  # no edge
            ZoneQuotaInput("zone:2:0", ()),       # no candidates in window
        ),
    )
    assert {item.zone_id for item in constraint.zone_requirements} == {"zone:0:0"}
    infeasible = {item.zone_id: item for item in constraint.zone_infeasible}
    assert set(infeasible) == {"zone:1:0", "zone:2:0"}
    assert infeasible["zone:1:0"].infeasible_reason == "no_feasible_match"
    assert infeasible["zone:2:0"].infeasible_reason == "no_zone_candidates"
    assert constraint.required_new_search_count == 1
    assert constraint.must_service_task_ids == ("Z1",)


def test_constraint_rejects_bad_arguments():
    with pytest.raises(ValueError):
        build_coverage_constraint(
            available_ids=("U1",), representatives=("S1",),
            edges=(), fraction=0.0,
        )
    with pytest.raises(ValueError):
        build_coverage_constraint(
            available_ids=("U1",), representatives=("S1",),
            edges=(), fraction=1.0, active_search_count=-1,
        )
    with pytest.raises(ValueError):
        build_coverage_constraint(
            available_ids=("U1",), representatives=("S1",),
            edges=(), fraction=1.0,
            zone_requirements_input=(ZoneQuotaInput("z", ("S1",)), ZoneQuotaInput("z", ("S2",))),
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/mission/test_coverage_zone_quota.py -q`
Expected: FAIL — `ImportError: cannot import name 'adaptive_search_fraction'` (and existing `build_coverage_constraint` rejects the new arguments: `healthy_count` required).

- [ ] **Step 3: Write the minimal implementation**

3a. Add to `src/mission/contracts.py` (this is the Task 3 deliverable pulled forward — the builder depends on it; the dedicated contract tests are written in Task 3). Insert after the `CoverageConstraint` class (before `MissionSnapshot`, around line 657):

```python
@dataclass(frozen=True)
class ZoneCoverageRequirement:
    """Enforced or audited quota for one deficit zone in a snapshot."""

    zone_id: str
    required_search_count: int
    representative_task_ids: tuple[str, ...]
    must_service_task_ids: tuple[str, ...]
    infeasible_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.zone_id, str) or not self.zone_id:
            raise ValueError("zone_id: expected a non-empty string")
        if (
            isinstance(self.required_search_count, bool)
            or not isinstance(self.required_search_count, int)
            or self.required_search_count < 0
        ):
            raise ValueError("required_search_count: expected a non-negative integer")
        representatives = tuple(self.representative_task_ids)
        must_service = tuple(self.must_service_task_ids)
        if any(not isinstance(item, str) or not item for item in representatives):
            raise ValueError("representative_task_ids: expected non-empty strings")
        if any(item not in representatives for item in must_service):
            raise ValueError("must_service_task_ids must be representatives")
        if self.infeasible_reason is not None and (
            not isinstance(self.infeasible_reason, str) or not self.infeasible_reason
        ):
            raise ValueError("infeasible_reason: expected a non-empty string when set")
        if self.infeasible_reason is None and not must_service:
            raise ValueError("enforced zone requirement needs must_service_task_ids")
        object.__setattr__(self, "representative_task_ids", representatives)
        object.__setattr__(self, "must_service_task_ids", must_service)
```

Extend `CoverageConstraint` with two fields (kw_only defaults, so every existing construction stays valid):

```python
    zone_requirements: tuple[ZoneCoverageRequirement, ...] = field(
        default=(), kw_only=True,
    )
    zone_infeasible: tuple[ZoneCoverageRequirement, ...] = field(
        default=(), kw_only=True,
    )
```

And at the end of `CoverageConstraint.__post_init__` add:

```python
        requirements = tuple(self.zone_requirements)
        if any(not isinstance(item, ZoneCoverageRequirement) for item in requirements):
            raise ValueError("zone_requirements must contain ZoneCoverageRequirement")
        if len({item.zone_id for item in requirements}) != len(requirements):
            raise ValueError("zone_requirements zone_id must be unique")
        if any(item.infeasible_reason is not None for item in requirements):
            raise ValueError("zone_requirements must contain enforced zones only")
        infeasible = tuple(self.zone_infeasible)
        if any(not isinstance(item, ZoneCoverageRequirement) for item in infeasible):
            raise ValueError("zone_infeasible must contain ZoneCoverageRequirement")
        if len({item.zone_id for item in infeasible}) != len(infeasible):
            raise ValueError("zone_infeasible zone_id must be unique")
        if any(item.infeasible_reason is None for item in infeasible):
            raise ValueError("zone_infeasible entries need an infeasible_reason")
        object.__setattr__(self, "zone_requirements", requirements)
        object.__setattr__(self, "zone_infeasible", infeasible)
```

(If `field` is not already imported in contracts.py, add it to the `from dataclasses import ...` import line.)

3b. In `src/mission/coverage_policy.py`, add the import (with the existing imports at the top):

```python
from src.mission.coverage_zones import ZoneQuotaInput
```

Add `adaptive_search_fraction` after `rank_search_candidates`:

```python
def adaptive_search_fraction(
    gap_pct: float | None,
    *,
    min_fraction: float = 0.4,
    max_fraction: float = 1.0,
) -> float:
    """Map the whole-domain gap percentage to a search resource fraction.

    gap=0 returns min_fraction; gap>=100 returns max_fraction; a missing
    percentage (legacy or empty domain) returns min_fraction.
    """
    for name, value in (("min_fraction", min_fraction), ("max_fraction", max_fraction)):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name} must be finite and in (0, 1]")
    min_fraction = float(min_fraction)
    max_fraction = float(max_fraction)
    if not math.isfinite(min_fraction) or not 0.0 < min_fraction <= 1.0:
        raise ValueError("min_fraction must be finite and in (0, 1]")
    if not math.isfinite(max_fraction) or not 0.0 < max_fraction <= 1.0:
        raise ValueError("max_fraction must be finite and in (0, 1]")
    if min_fraction > max_fraction:
        raise ValueError("min_fraction cannot exceed max_fraction")
    if gap_pct is None:
        return min_fraction
    if isinstance(gap_pct, bool) or not isinstance(gap_pct, Real):
        raise ValueError("gap_pct must be a finite number or None")
    gap = float(gap_pct) / 100.0
    gap = max(0.0, min(1.0, gap))
    return min_fraction + (max_fraction - min_fraction) * gap
```

Replace the entire body of `build_coverage_constraint` (keep the function name and position; the old signature parameters `healthy_count` are removed):

```python
def build_coverage_constraint(
    *,
    available_ids: Iterable[str],
    representatives: Iterable[Any],
    edges: Iterable[Any],
    fraction: float,
    active_search_count: int = 0,
    zone_requirements_input: Iterable[ZoneQuotaInput] = (),
):
    """Build an exact-count parallel search constraint with optional zone quotas.

    The desired count anchors on available (idle) UAVs. The returned
    required_new_search_count is an EXACT target: the validator asks the model
    to select exactly that many ordinary search candidates. Deficit-zone
    candidates visit the maximum matching before global representatives so
    quotas never double-book a UAV, and unmatched zones are reported in
    zone_infeasible instead of being demanded.
    """
    from src.mission.contracts import CoverageConstraint, ZoneCoverageRequirement

    if isinstance(fraction, bool) or not isinstance(fraction, Real):
        raise ValueError("fraction must be finite and in (0, 1]")
    fraction = float(fraction)
    if not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be finite and in (0, 1]")
    if (
        isinstance(active_search_count, bool)
        or not isinstance(active_search_count, Integral)
        or active_search_count < 0
    ):
        raise ValueError("active_search_count must be a non-negative integer")

    available = tuple(dict.fromkeys(str(item) for item in available_ids))
    available_set = set(available)
    representative_ids = tuple(dict.fromkeys(
        getattr(item, "task_id", item) for item in representatives
    ))
    if any(not isinstance(item, str) or not item for item in representative_ids):
        raise ValueError("representatives must contain task IDs or task objects")

    zone_inputs: list[tuple[str, tuple[str, ...]]] = []
    seen_zone_ids: set[str] = set()
    for zone_input in zone_requirements_input:
        zone_id = zone_input.zone_id
        candidates = tuple(dict.fromkeys(zone_input.candidate_task_ids))
        if zone_id in seen_zone_ids:
            raise ValueError("zone_requirements_input zone_id must be unique")
        seen_zone_ids.add(zone_id)
        zone_inputs.append((zone_id, candidates))

    adjacency: dict[str, list[str]] = {task_id: [] for task_id in representative_ids}
    for _zone_id, candidates in zone_inputs:
        for task_id in candidates:
            adjacency.setdefault(task_id, [])
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

    # Deficit zones visit first so quota slots take priority over the global
    # representative pool inside the SAME matching.
    zone_matched: dict[str, str] = {}
    for zone_id, candidates in zone_inputs:
        for task_id in candidates:
            if visit(task_id, set()):
                zone_matched[zone_id] = task_id
                break
    for task_id in representative_ids:
        visit(task_id, set())

    desired = int(math.ceil(len(available) * fraction))
    matched_slots = len(matched_uav)
    required = min(desired, matched_slots)
    infeasible_reason = (
        "insufficient_available_resources" if desired > matched_slots else None
    )

    zone_requirements = tuple(
        ZoneCoverageRequirement(
            zone_id=zone_id,
            required_search_count=1,
            representative_task_ids=candidates,
            must_service_task_ids=(task_id,),
            infeasible_reason=None,
        )
        for zone_id, task_id in zone_matched.items()
    )
    zone_infeasible = tuple(
        ZoneCoverageRequirement(
            zone_id=zone_id,
            required_search_count=1,
            representative_task_ids=candidates,
            must_service_task_ids=(),
            infeasible_reason=(
                "no_zone_candidates" if not candidates else "no_feasible_match"
            ),
        )
        for zone_id, candidates in zone_inputs
        if zone_id not in zone_matched
    )

    must_service: list[str] = []
    if zone_matched:
        must_service = list(zone_matched.values())
    elif required and any(adjacency[task_id] for task_id in representative_ids):
        must_service = [
            next(task_id for task_id in representative_ids if adjacency[task_id])
        ]

    merged_representatives = tuple(dict.fromkeys([
        *[task_id for _zone_id, candidates in zone_inputs for task_id in candidates],
        *representative_ids,
    ]))
    return CoverageConstraint(
        desired_search_count=desired,
        active_search_count=int(active_search_count),
        required_new_search_count=required,
        representative_task_ids=merged_representatives,
        must_service_task_ids=tuple(must_service),
        infeasible_reason=infeasible_reason,
        zone_requirements=zone_requirements,
        zone_infeasible=zone_infeasible,
    )
```

- [ ] **Step 4: Replace the two obsolete tests in `tests/mission/test_coverage_prompt_window.py`**

Delete `test_constraint_uses_maximum_matching_not_minimum_of_counts` (lines 82–96) and `test_constraint_reports_infeasible_floor_without_fabricating_a_slot` (lines 98–109); their semantics (healthy-anchored floor) are gone. The new coverage lives in `tests/mission/test_coverage_zone_quota.py`. (The validator tests at lines 112+ stay for now; Task 6 updates `test_validator_requires_oldest_representative_and_floor`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/mission/test_coverage_zone_quota.py tests/mission/test_coverage_prompt_window.py -q`
Expected: PASS for the new suite; PASS for the remaining window tests (`test_window_*`, `test_validator_*` — the validator tests still pass because Task 6 has not changed the validator yet).

- [ ] **Step 6: Commit**

```bash
git add src/mission/coverage_policy.py src/mission/contracts.py tests/mission/test_coverage_zone_quota.py tests/mission/test_coverage_prompt_window.py
git commit -m "feat: anchor search constraint on available UAVs with exact count and zone quotas"
```

---

### Task 3: Contract types — `ZoneCoverageRequirement`, constraint fields, snapshot `coverage_summary`

**Files:**
- Modify: `src/mission/contracts.py` (ZoneCoverageRequirement + CoverageConstraint fields already added in Task 2 Step 3a; this task adds `MissionSnapshot.coverage_summary` and all contract tests)
- Test: `tests/mission/test_contracts.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (used by Tasks 5, 6): `MissionSnapshot.coverage_summary: dict | None` (kw_only, default `None`); `CoverageConstraint.zone_requirements` / `zone_infeasible` (already in place); `_jsonable` in `mission_scheduler.py` serializes both automatically via `asdict`.

- [ ] **Step 1: Write the failing tests** — append to `tests/mission/test_contracts.py`:

```python
def test_zone_requirement_validates_identity_and_must_service():
    from src.mission.contracts import ZoneCoverageRequirement

    with pytest.raises(ValueError):
        ZoneCoverageRequirement("", 1, ("S1",), ("S1",), None)
    with pytest.raises(ValueError):
        ZoneCoverageRequirement("z", 1, ("S1",), ("S2",), None)  # not a representative
    with pytest.raises(ValueError):
        ZoneCoverageRequirement("z", 1, ("S1",), (), None)  # enforced needs must-service
    with pytest.raises(ValueError):
        ZoneCoverageRequirement("z", 1, ("S1",), (), "no_feasible_match", ) if False else ZoneCoverageRequirement("z", 1, (), (), "")  # empty infeasible_reason
    audited = ZoneCoverageRequirement("z", 1, ("S1",), (), "no_feasible_match")
    assert audited.infeasible_reason == "no_feasible_match"
    assert audited.must_service_task_ids == ()
```

Careful with the fourth `pytest.raises` above — simplify it to:

```python
    with pytest.raises(ValueError):
        ZoneCoverageRequirement("z", 1, (), (), "")  # empty infeasible_reason string
```

(Use this simpler form in the actual file.)

```python
def test_coverage_constraint_zone_fields_default_empty_and_validated():
    from src.mission.contracts import CoverageConstraint, ZoneCoverageRequirement

    base = CoverageConstraint(
        desired_search_count=1,
        active_search_count=0,
        required_new_search_count=1,
        representative_task_ids=("S1",),
        must_service_task_ids=("S1",),
    )
    assert base.zone_requirements == ()
    assert base.zone_infeasible == ()
    with pytest.raises(ValueError):
        CoverageConstraint(
            1, 0, 1, ("S1",), ("S1",), None,
            zone_requirements=(ZoneCoverageRequirement("z", 1, ("S1",), (), "bad"),),
        )  # zone_requirements must hold enforced zones only
    with pytest.raises(ValueError):
        CoverageConstraint(
            1, 0, 1, ("S1",), ("S1",), None,
            zone_infeasible=(ZoneCoverageRequirement("z", 1, ("S1",), ("S1",), None),),
        )  # zone_infeasible entries need an infeasible_reason


def test_mission_snapshot_coverage_summary_is_optional_and_typed():
    from src.mission.contracts import MissionSnapshot

    def snapshot(**kwargs):
        defaults = dict(
            snapshot_id="s", sim_time_min=0.0, candidates=(), available_uav_ids=(),
            preemptible_uav_ids=(), uav_generations=(), resources=(), feasible_edges=(),
            active_tasks=(), contacts=(), intents=(), intent_statuses=(),
            memory_version="baseline", planning_map_version=0, reviewer_summary="",
        )
        defaults.update(kwargs)
        return MissionSnapshot(**defaults)

    assert snapshot().coverage_summary is None
    assert snapshot(coverage_summary={"gap_pct": 50.0}).coverage_summary == {"gap_pct": 50.0}
    with pytest.raises(ValueError):
        snapshot(coverage_summary="not-a-dict")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/mission/test_contracts.py -q -k "zone_requirement or zone_fields or coverage_summary"`
Expected: FAIL on `test_mission_snapshot_coverage_summary_is_optional_and_typed` (`AttributeError: coverage_summary` / `TypeError: unexpected keyword argument`). The `ZoneCoverageRequirement` tests already pass from Task 2's pulled-forward implementation.

- [ ] **Step 3: Write the minimal implementation** — in `src/mission/contracts.py`, add to `MissionSnapshot` after the `coverage_constraint` field:

```python
    coverage_summary: dict | None = field(default=None, kw_only=True)
```

and in `MissionSnapshot.__post_init__` (next to the `coverage_constraint` check):

```python
        if self.coverage_summary is not None and not isinstance(self.coverage_summary, dict):
            raise ValueError("coverage_summary must be a dict or None")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/mission/test_contracts.py -q`
Expected: PASS (all contract tests, including the pre-existing ones).

- [ ] **Step 5: Commit**

```bash
git add src/mission/contracts.py tests/mission/test_contracts.py
git commit -m "feat: add zone requirements and coverage summary to mission contracts"
```

---

### Task 4: Zone-rotated representative selection in `select_window`

**Files:**
- Modify: `src/mission/coverage_policy.py` (`select_window` gains `zones: ZonePartition | None = None`; rotation logic only in the representative pass)
- Test: `tests/mission/test_coverage_prompt_window.py`

**Interfaces:**
- Consumes: `ZonePartition` (Task 1).
- Produces: `CoveragePolicy.select_window(ranked_candidates, *, ordinary_reserve, capacity, now_min, zones=None) -> CoverageCandidateWindow` — with `zones=None` output is byte-identical to today; with zones, the ordinary representatives are chosen round-robin across zones (within-zone order keeps the global rank), urgent handling unchanged, the filler pass unchanged.

- [ ] **Step 1: Write the failing tests** — append to `tests/mission/test_coverage_prompt_window.py`:

```python
def test_window_rotates_representatives_across_zones():
    from src.mission.coverage_zones import ZonePartition

    fixed = np.ones((30, 6), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=1)
    tasks = [
        _task(f"S{zone_index}-{slot}", bbox=(zone_index * 10 + slot * 2, 0, zone_index * 10 + slot * 2 + 1, 1))
        for zone_index in range(3)
        for slot in range(3)
    ]
    window = CoveragePolicy(fixed).select_window(
        tasks, ordinary_reserve=6, capacity=10, now_min=0.0, zones=zones,
    )
    ordinary = [task for task in window.tasks if task.kind == "search"]
    first_three = ordinary[:3]
    assert {zones.zone_of_bbox(task.bbox) for task in first_three} == {
        "zone:0:0", "zone:1:0", "zone:2:0",
    }
    # no overlap among representatives (existing invariant kept)
    representatives = [
        task for task in window.tasks
        if task.task_id in window.representative_task_ids
    ]
    for left_index, left in enumerate(representatives):
        for right in representatives[left_index + 1:]:
            assert not _boxes_overlap(left.bbox, right.bbox)


def test_window_without_zones_matches_previous_behavior():
    tasks = [
        _task(f"S{index}", bbox=(index * 2, 0, index * 2 + 1, 1))
        for index in range(12)
    ]
    fixed = np.ones((30, 2), dtype=bool)
    expected = CoveragePolicy(fixed).select_window(
        tasks, ordinary_reserve=4, capacity=6, now_min=0.0,
    )
    with_zones_none = CoveragePolicy(fixed).select_window(
        tasks, ordinary_reserve=4, capacity=6, now_min=0.0, zones=None,
    )
    assert [task.task_id for task in with_zones_none.tasks] == [
        task.task_id for task in expected.tasks
    ]
    assert with_zones_none.representative_task_ids == expected.representative_task_ids
    assert dict(with_zones_none.sources) == dict(expected.sources)


def _boxes_overlap(left, right):
    return not (
        left[2] <= right[0] or right[2] <= left[0]
        or left[3] <= right[1] or right[3] <= left[1]
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/mission/test_coverage_prompt_window.py -q -k "rotates or without_zones"`
Expected: FAIL with `TypeError: select_window() got an unexpected keyword argument 'zones'`.

- [ ] **Step 3: Write the minimal implementation**

In `src/mission/coverage_policy.py`, add the import at the top:

```python
from src.mission.coverage_zones import ZonePartition, ZoneQuotaInput
```

Change the signature and the representative pass of `select_window`:

```python
    def select_window(
        self,
        ranked_candidates: Iterable[Any],
        *,
        ordinary_reserve: int,
        capacity: int,
        now_min: float,
        zones: ZonePartition | None = None,
    ) -> CoverageCandidateWindow:
        """Reserve ordinary coverage slots before filling urgent work.

        With ``zones`` set, the ordinary representatives are chosen
        round-robin across zones (best-ranked candidate per zone first) so
        the prompt shows spatially dispersed options. ``zones=None`` keeps
        the previous global-order behavior exactly.
        """
```

(Keep the docstring's remaining lines, or replace the whole docstring with the above + the original second sentence about identity safety.)

Replace the representative-selection block — the current code:

```python
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
```

with:

```python
        representative_ordinary: list[Any] = []
        representative_boxes: list[tuple[float, float, float, float]] = []

        def representable(task: Any) -> bool:
            box = task_box(task)
            return box is not None and not any(
                _boxes_overlap(box, other) for other in representative_boxes
            )

        def take_representative(task: Any) -> None:
            box = task_box(task)
            assert box is not None
            representative_ordinary.append(task)
            representative_boxes.append(box)

        if zones is None:
            for task in ordinary:
                if len(representative_ordinary) >= reserve_count:
                    break
                if representable(task):
                    take_representative(task)
        else:
            by_zone: dict[str, list[Any]] = {}
            for task in ordinary:
                box = task_box(task)
                if box is None:
                    continue
                zone_id = zones.zone_of_bbox(box)
                if zone_id is not None:
                    by_zone.setdefault(zone_id, []).append(task)
            round_index = 0
            while len(representative_ordinary) < reserve_count:
                took_any = False
                for zone_id in sorted(by_zone):
                    candidates = by_zone[zone_id]
                    if round_index >= len(candidates):
                        continue
                    task = candidates[round_index]
                    if representable(task):
                        take_representative(task)
                        took_any = True
                        if len(representative_ordinary) >= reserve_count:
                            break
                if not took_any:
                    break
                round_index += 1

        selected = [*urgent[:urgent_count], *representative_ordinary]
```

Note `task_box` is already defined later in the method (the local helper) — move its definition above this block (or duplicate it before use; moving is cleaner: relocate the existing `task_box` definition to just after `ordinary_ids`/`urgent` computation).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/mission/test_coverage_prompt_window.py -q`
Expected: PASS (all window tests: rotation, None-compat, and the pre-existing reserve/filler tests).

- [ ] **Step 5: Commit**

```bash
git add src/mission/coverage_policy.py tests/mission/test_coverage_prompt_window.py
git commit -m "feat: rotate prompt-window search representatives across zones"
```

---

### Task 5: Wire summary → fraction → quota → constraint in `build_mission_snapshot`

**Files:**
- Modify: `src/schedule/task_allocator.py` (imports; `_coverage_prompt_window` passes `zones`; `build_mission_snapshot` replaces the inline constraint block with the full wiring; snapshot gains `coverage_summary`)
- Modify: `src/mission/config.py` (`CoverageConfig` new fields + validation)
- Test: `tests/mission/test_config.py` (config), `tests/mission/test_zone_rolling_acceptance.py` (new; the wiring-level assertions are validated here against a real `TaskAllocator` in Task 8 — this task's tests cover config + a pure wiring helper)

**Interfaces:**
- Consumes: `ZonePartition`, `build_zone_coverage_summary`, `build_zone_quota_inputs` (Task 1); `adaptive_search_fraction`, `build_coverage_constraint` (Task 2); `MissionSnapshot.coverage_summary` (Task 3); `select_window(zones=...)` (Task 4). `sm.get_searchable_mask()`, `sm.coverage_metrics`, `sm.is_uav_operational`, `sm.get_all_uavs` already exist on `StateManager`; `_SEARCH_TASK_KINDS` is already defined in this module (line 43).
- Produces: snapshot with non-`None` `coverage_summary` and zone-aware `coverage_constraint` whenever coverage metrics are configured; `None`/unchanged on the legacy path.

- [ ] **Step 1: Write the failing tests**

1a. Append to `tests/mission/test_config.py`:

```python
def test_coverage_config_zone_and_budget_defaults():
    from src.mission.config import CoverageConfig

    coverage = CoverageConfig()
    assert coverage.zone_cols == 3
    assert coverage.zone_rows == 3
    assert coverage.search_uav_fraction_max == 1.0
    assert coverage.zone_quota_gap_threshold == 0.5
    assert coverage.min_search_uav_fraction == 0.4


def test_coverage_config_rejects_bad_zone_and_budget_values():
    from src.mission.config import CoverageConfig

    with pytest.raises(ValueError):
        CoverageConfig(zone_cols=0)
    with pytest.raises(ValueError):
        CoverageConfig(search_uav_fraction_max=0.2)  # below min 0.4
    with pytest.raises(ValueError):
        CoverageConfig(search_uav_fraction_max=1.5)
    with pytest.raises(ValueError):
        CoverageConfig(zone_quota_gap_threshold=-0.1)
```

1b. Create `tests/mission/test_zone_rolling_acceptance.py` with the wiring-composition tests (full file content in Task 8; for THIS task include the first two tests only):

```python
import numpy as np
import pytest

from src.mission.coverage_policy import (
    adaptive_search_fraction,
    build_coverage_constraint,
)
from src.mission.coverage_zones import (
    ZonePartition,
    build_zone_coverage_summary,
    build_zone_quota_inputs,
)
from src.mission.contracts import FeasibleEdge, TaskCandidate


def _edge(task_id, uav_id):
    return FeasibleEdge(task_id, uav_id, 1.0, 1.0, 1.0, 0.0, f"{uav_id}:{task_id}")


def _search(task_id, bbox):
    return TaskCandidate(
        task_id, "search", bbox, None, (), ("U1",), 0.0, "medium", 1.0, 1.0, 0.0,
    )


def test_round_one_full_gap_demands_exact_parallel_search_and_zone_quotas():
    # 30x30 fixed domain, 10 idle UAVs, everything unseen -> gap = 100%.
    fixed = np.ones((30, 30), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=3)
    summary = build_zone_coverage_summary(
        zones, now_min=0.0, last_sar=np.full((30, 30), -np.inf),
        feasible_mask=np.ones((30, 30), dtype=bool), primary_window_min=60,
    )
    assert summary["gap_pct"] == pytest.approx(100.0)
    fraction = adaptive_search_fraction(summary["gap_pct"])
    assert fraction == pytest.approx(1.0)
    available = tuple(f"U{index}" for index in range(10))
    # one legal non-overlapping window candidate per zone (fully contained)
    window = tuple(
        _search(f"z{zindex}", (zindex * 10 + 1, 1, zindex * 10 + 6, 6))
        for zindex in range(9)
    )
    quota_inputs = build_zone_quota_inputs(
        zones, summary, window, threshold=0.5, max_slots=10,
    )
    assert [item.zone_id for item in quota_inputs] == [
        f"zone:{col}:{row}" for col in range(3) for row in range(3)
    ]
    edges = tuple(_edge(task.task_id, uav_id) for task, uav_id in zip(window, available))
    constraint = build_coverage_constraint(
        available_ids=available,
        active_search_count=0,
        representatives=(task.task_id for task in window),
        edges=edges,
        fraction=fraction,
        zone_requirements_input=quota_inputs,
    )
    assert constraint.desired_search_count == 10
    assert constraint.required_new_search_count == 10
    assert constraint.infeasible_reason is None
    assert len(constraint.zone_requirements) == 9
    assert set(constraint.must_service_task_ids) == {task.task_id for task in window}


def test_rolling_completion_lowers_budget_and_moves_quota():
    fixed = np.ones((30, 30), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=3)
    last_sar = np.full((30, 30), -np.inf)
    # after SAR completes, the top-left 2 zones become fresh
    last_sar[:20, :20] = 95.0
    summary = build_zone_coverage_summary(
        zones, now_min=100.0, last_sar=last_sar,
        feasible_mask=np.ones((30, 30), dtype=bool), primary_window_min=60,
    )
    fraction = adaptive_search_fraction(summary["gap_pct"])
    assert fraction < 1.0
    available = tuple(f"U{index}" for index in range(10))
    window = tuple(
        _search(f"z{zindex}", (zindex * 10 + 1, 1, zindex * 10 + 6, 6))
        for zindex in range(9)
    )
    quota_inputs = build_zone_quota_inputs(
        zones, summary, window, threshold=0.5, max_slots=10,
    )
    # zones 0:0 and 1:0 (cols 0-19) are fresh -> no longer deficit
    deficit_ids = {item.zone_id for item in quota_inputs}
    assert "zone:0:0" not in deficit_ids
    assert "zone:1:0" not in deficit_ids
    assert len(deficit_ids) == 5  # zones with any col in 20..29 rows 0..19 + bottom row
```

Careful with the last assertion: fixed 30×30, zones 3×3 → tiles 10×10. last_sar[:20,:20] fresh covers zones (0,0),(0,1),(1,0),(1,1) fully? cols 0-19 = zones 0,1; rows 0-19 = zones 0,1 → zones 0:0,0:1,1:0,1:1 all cells fresh → 4 fresh zones, 5 deficit (zones with col≥20 or row≥20: (2,0),(2,1),(2,2),(0,2),(1,2)) → 5 deficit ✅. Adjust the comment accordingly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/mission/test_config.py -q -k "zone_and_budget" && python -m pytest tests/mission/test_zone_rolling_acceptance.py -q`
Expected: config tests FAIL on missing attributes (`AttributeError: zone_cols`); acceptance tests PASS already (they only use Task 1–2 APIs) — that is fine; they become the regression net for this task's wiring. The actual wiring assertion (snapshot emits `coverage_summary`) lands in Task 8 with the engine fixture.

- [ ] **Step 3: Write the minimal implementation**

3a. In `src/mission/config.py`, extend `CoverageConfig`:

```python
@dataclass(frozen=True)
class CoverageConfig:
    windows_min: tuple[int, ...] = (30, 60, 120)
    primary_window_min: int = 60
    min_search_uav_fraction: float = 0.4
    search_uav_fraction_max: float = 1.0
    zone_cols: int = 3
    zone_rows: int = 3
    zone_quota_gap_threshold: float = 0.5
    ordinary_prompt_reserve: int = 8
    geometry_candidate_budget: int = 120
    no_progress_timeout_min: float = 10.0
    align_timeout_min: float = 8.0
    max_stall_replans: int = 2
    max_consecutive_decision_failures: int = 3
```

and in `__post_init__` after the existing fraction check:

```python
        max_fraction = finite_number(
            self.search_uav_fraction_max, "coverage.search_uav_fraction_max"
        )
        if not 0.0 < max_fraction <= 1.0:
            raise ValueError("coverage.search_uav_fraction_max must be in (0, 1]")
        if max_fraction < fraction:
            raise ValueError(
                "coverage.search_uav_fraction_max cannot be below min_search_uav_fraction"
            )
        _integer(self.zone_cols, "coverage.zone_cols", minimum=1)
        _integer(self.zone_rows, "coverage.zone_rows", minimum=1)
        quota_threshold = finite_number(
            self.zone_quota_gap_threshold, "coverage.zone_quota_gap_threshold"
        )
        if not 0.0 <= quota_threshold <= 1.0:
            raise ValueError("coverage.zone_quota_gap_threshold must be in [0, 1]")
```

(Check that `fraction` and `finite_number` are the exact names used in the existing `__post_init__` body — the current code uses `fraction = finite_number(self.min_search_uav_fraction, ...)`; reuse it.)

3b. In `src/schedule/task_allocator.py` add to the `src.mission.coverage_policy` import block:

```python
    adaptive_search_fraction,
```
and add a new import block:

```python
from src.mission.coverage_zones import (
    ZonePartition,
    build_zone_coverage_summary,
    build_zone_quota_inputs,
)
```

3c. In `_coverage_prompt_window`, in the coverage branch (after `policy = CoveragePolicy(...)`), build the partition and pass it through:

```python
        zone_cols = int(getattr(coverage_config, "zone_cols", 3))
        zone_rows = int(getattr(coverage_config, "zone_rows", 3))
        zones = ZonePartition(metrics.fixed_mask, zone_cols=zone_cols, zone_rows=zone_rows)
```
and change the return to:
```python
        return policy.select_window(
            ranked,
            ordinary_reserve=min(ordinary_reserve, capacity),
            capacity=capacity,
            now_min=now_min,
            zones=zones,
        )
```
(The legacy branch keeps calling `select_window` without `zones` — unchanged.)

3d. In `build_mission_snapshot`, replace the whole coverage-constraint block (from `coverage_constraint = None` down to the closing of `build_coverage_constraint(...)`) with a call to a new helper and the summary:

```python
        coverage_summary, coverage_constraint = self._coverage_summary_and_constraint(
            prompt_window=prompt_window,
            edges=edges,
            available=available,
            active_records=active_records,
            now=now,
        )
```

Add the helper method to `TaskAllocator` (place it right after `_coverage_prompt_window`):

```python
    def _coverage_summary_and_constraint(
        self,
        *,
        prompt_window: CoverageCandidateWindow | None,
        edges: tuple[FeasibleEdge, ...],
        available: tuple[str, ...],
        active_records: tuple[TaskRecord, ...],
        now: float,
    ) -> tuple[dict | None, object | None]:
        """Zone summary + gap-adaptive exact-count constraint for one decision."""
        metrics = getattr(self.sm, "coverage_metrics", None)
        coverage_config = getattr(self.config.mission, "coverage", None)
        if prompt_window is None or metrics is None or coverage_config is None:
            return None, None
        primary_window = int(getattr(coverage_config, "primary_window_min", 60))
        zones = ZonePartition(
            metrics.fixed_mask,
            int(getattr(coverage_config, "zone_cols", 3)),
            int(getattr(coverage_config, "zone_rows", 3)),
        )
        feasible = np.logical_and(metrics.fixed_mask, self.sm.get_searchable_mask())
        global_snapshot = metrics.snapshot(now_min=now, feasible_mask=feasible)
        in_flight = tuple(
            record
            for record in active_records
            if record.status in _ACTIVE_RECORD_STATUSES
            and record.kind in _SEARCH_TASK_KINDS
            and record.bbox is not None
            and record.assigned_uav_id is not None
        )
        summary = build_zone_coverage_summary(
            zones,
            now_min=now,
            last_sar=metrics.last_scan_matrix(),
            feasible_mask=feasible,
            primary_window_min=primary_window,
            in_flight_tasks=in_flight,
            global_snapshot=global_snapshot,
        )
        fraction = adaptive_search_fraction(
            summary["gap_pct"],
            min_fraction=float(getattr(coverage_config, "min_search_uav_fraction", 0.4)),
            max_fraction=float(getattr(coverage_config, "search_uav_fraction_max", 1.0)),
        )
        required_raw = math.ceil(len(available) * fraction)
        quota_inputs = build_zone_quota_inputs(
            zones,
            summary,
            prompt_window.tasks,
            threshold=float(getattr(coverage_config, "zone_quota_gap_threshold", 0.5)),
            max_slots=required_raw,
        )
        active_search_count = sum(
            record.status in _ACTIVE_RECORD_STATUSES
            and record.assigned_uav_id is not None
            and record.kind in _SEARCH_TASK_KINDS
            and self.sm.is_uav_operational(record.assigned_uav_id)
            for record in active_records
        )
        constraint = build_coverage_constraint(
            available_ids=available,
            active_search_count=active_search_count,
            representatives=prompt_window.representative_task_ids,
            edges=edges,
            fraction=fraction,
            zone_requirements_input=quota_inputs,
        )
        return summary, constraint
```

Notes for the executor: `_ACTIVE_RECORD_STATUSES` must be defined in `task_allocator.py` (it currently inlines `{"approved", "executing"}` — add a module constant next to `_SEARCH_TASK_KINDS` if absent: `_ACTIVE_RECORD_STATUSES = {"approved", "executing"}`); the deleted inline block also computed `healthy_count` — it is no longer needed (the anchor moved to `available`), remove it; keep `active_records` filtering as-is.

3e. In `build_mission_snapshot`, pass the summary into the snapshot constructor:

```python
            coverage_constraint=coverage_constraint,
            coverage_summary=coverage_summary,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/mission/test_config.py tests/mission/test_zone_rolling_acceptance.py tests/mission/test_coverage_prompt_window.py tests/mission/test_coverage_zone_quota.py -q`
Expected: PASS. Also run the allocator-touching suites: `python -m pytest tests/mission/test_mission_task_lifecycle.py tests/mission/test_coverage_scan_integration.py -q` — PASS (the wiring is backward compatible: no metrics → `None, None`).

- [ ] **Step 5: Commit**

```bash
git add src/schedule/task_allocator.py src/mission/config.py tests/mission/test_config.py tests/mission/test_zone_rolling_acceptance.py
git commit -m "feat: wire zone summary, adaptive budget and zone quotas into mission snapshots"
```

---

### Task 6: Validator exact count + zone quotas; payload `coverage_summary`

**Files:**
- Modify: `src/mission/mission_scheduler.py` (`_validate_selection` coverage block; `_prompt_payload` embeds `coverage_summary`)
- Test: `tests/mission/test_coverage_prompt_window.py` (update `test_validator_requires_oldest_representative_and_floor`); `tests/mission/test_zone_rolling_acceptance.py` (add validator acceptance tests — full file assembled in Task 8)

**Interfaces:**
- Consumes: `CoverageConstraint.zone_requirements` (Task 3); snapshot `coverage_summary` (Task 3).
- Produces: validation errors `search_count_not_exact:{required}:{selected}`, `zone_quota_not_met:{zone_id}`, `zone_must_service_not_selected:{zone_id}`; `coverage_oldest_not_selected` retained for the global must-service; payload key `snapshot.coverage_summary`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/mission/test_coverage_prompt_window.py` (the validator tests need `MissionSelection`/`TaskCandidate` already imported there; `UavResource`, `FeasibleEdge` too):

```python
def test_validator_enforces_exact_search_count_and_rejects_overbook():
    tasks = (_task("S1", bbox=(1, 1, 2, 2)), _task("S2", bbox=(3, 1, 4, 2)))
    resources = (_resource("U1"), _resource("U2"))
    edges = (_edge("S1", "U1"), _edge("S2", "U2"))
    snapshot = MissionSnapshot(
        snapshot_id="snapshot-exact",
        sim_time_min=10.0,
        candidates=tasks,
        available_uav_ids=("U1", "U2"),
        preemptible_uav_ids=(),
        uav_generations=(("U1", 0), ("U2", 0)),
        resources=resources,
        feasible_edges=edges,
        active_tasks=(),
        contacts=(),
        intents=(),
        intent_statuses=(),
        memory_version="baseline",
        planning_map_version=0,
        reviewer_summary="",
        prompt_task_ids=("S1", "S2"),
        prompt_sources=(("S1", "ordinary"), ("S2", "ordinary")),
        coverage_constraint=CoverageConstraint(
            desired_search_count=2,
            active_search_count=0,
            required_new_search_count=2,
            representative_task_ids=("S1", "S2"),
            must_service_task_ids=("S1",),
        ),
    )
    scheduler = MissionScheduler(selection_provider=lambda _snapshot, _payload: {})

    too_few = {
        "schema_version": SELECTION_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": ["S2"],
        "preempt_uav_ids": [],
        "defer_reason": None,
        "notes": "",
    }
    errors = scheduler.validate_selection(too_few, snapshot)
    assert "search_count_not_exact:2:1" in errors
    assert "coverage_oldest_not_selected" in errors

    exact = {
        **too_few,
        "selected_task_ids": ["S1", "S2"],
    }
    assert scheduler.validate_selection(exact, snapshot) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/mission/test_coverage_prompt_window.py -q -k "exact_search_count"`
Expected: FAIL — `search_count_not_exact:2:1` missing (the old code only emits `coverage_floor_not_met:2`).

- [ ] **Step 3: Write the minimal implementation**

In `_validate_selection` (mission_scheduler.py), replace the current coverage-constraint block:

```python
    coverage_constraint = snapshot.coverage_constraint
    if coverage_constraint is not None:
        selected_representatives = (
            set(selected_task_ids)
            & set(coverage_constraint.representative_task_ids)
        )
        floor_infeasible = coverage_constraint.infeasible_reason is not None
        if (
            not floor_infeasible
            and coverage_constraint.required_new_search_count
            > len(selected_representatives)
        ):
            errors.append(
                "coverage_floor_not_met:"
                f"{coverage_constraint.required_new_search_count}"
            )
        if (
            not floor_infeasible
            and coverage_constraint.required_new_search_count > 0
            and not set(coverage_constraint.must_service_task_ids).issubset(
                selected_representatives
            )
        ):
            errors.append("coverage_oldest_not_selected")
```

with:

```python
    coverage_constraint = snapshot.coverage_constraint
    if coverage_constraint is not None:
        selected_representatives = (
            set(selected_task_ids)
            & set(coverage_constraint.representative_task_ids)
        )
        floor_infeasible = coverage_constraint.infeasible_reason is not None
        if (
            not floor_infeasible
            and coverage_constraint.required_new_search_count > 0
        ):
            selected_search_count = sum(
                1
                for task_id in selected_task_ids
                if task_id in candidates
                and candidates[task_id].kind == "search"
                and candidates[task_id].bbox is not None
            )
            if selected_search_count != coverage_constraint.required_new_search_count:
                errors.append(
                    "search_count_not_exact:"
                    f"{coverage_constraint.required_new_search_count}:"
                    f"{selected_search_count}"
                )
        if (
            not floor_infeasible
            and coverage_constraint.required_new_search_count > 0
            and not set(coverage_constraint.must_service_task_ids).issubset(
                selected_representatives
            )
        ):
            errors.append("coverage_oldest_not_selected")
        for requirement in coverage_constraint.zone_requirements:
            zone_selected = set(selected_task_ids) & set(
                requirement.representative_task_ids
            )
            if len(zone_selected) < requirement.required_search_count:
                errors.append(f"zone_quota_not_met:{requirement.zone_id}")
            if requirement.required_search_count > 0 and not set(
                requirement.must_service_task_ids
            ).issubset(zone_selected):
                errors.append(
                    f"zone_must_service_not_selected:{requirement.zone_id}"
                )
```

In `_prompt_payload`, directly after the `coverage_constraint` embed:

```python
        if snapshot.coverage_constraint is not None:
            full["coverage_constraint"] = _jsonable(snapshot.coverage_constraint)
        if snapshot.coverage_summary is not None:
            full["coverage_summary"] = snapshot.coverage_summary
```

- [ ] **Step 4: Update `test_validator_requires_oldest_representative_and_floor`** (lines 112–156 of `tests/mission/test_coverage_prompt_window.py`)

Its selection `["S2"]` now yields `search_count_not_exact:2:1` instead of `coverage_floor_not_met:2`. Change the two assertions:

```python
    assert "coverage_floor_not_met:2" in errors
    assert "coverage_oldest_not_selected" in errors
```
to:
```python
    assert "search_count_not_exact:2:1" in errors
    assert "coverage_oldest_not_selected" in errors
```

and rename the test to `test_validator_requires_exact_count_and_oldest_representative`. The companion test `test_validator_allows_partial_floor_when_snapshot_reports_infeasible_resources` keeps passing unchanged (infeasible → checks skipped).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/mission/test_coverage_prompt_window.py tests/mission/test_mission_scheduler.py tests/mission/test_coverage_decision_budget.py -q`
Expected: PASS. `test_mission_scheduler.py` and `test_coverage_decision_budget.py` use `coverage_constraint=None` snapshots, so the exact-count path does not affect them.

- [ ] **Step 6: Commit**

```bash
git add src/mission/mission_scheduler.py tests/mission/test_coverage_prompt_window.py
git commit -m "feat: enforce exact search count and zone quotas in selection validation"
```

---

### Task 7: Prompt rewrite + README chapter 2 correction

**Files:**
- Modify: `src/mission/prompts/mission_scheduler.txt`
- Modify: `README.md`
- Test: `tests/mission/test_mission_scheduler.py` (prompt-loading test already exists — verify it still loads the file; add an assertion for the new role line)

**Interfaces:**
- Consumes: payload keys `coverage_summary`, `coverage_constraint` with `zone_requirements`/`zone_infeasible` (Tasks 5, 6).
- Produces: the model-facing contract text. No code interfaces.

- [ ] **Step 1: Write the failing test** — append to `tests/mission/test_mission_scheduler.py`:

```python
def test_system_prompt_describes_rolling_coverage_planner():
    import os
    from src.mission.mission_scheduler import MissionScheduler

    scheduler = MissionScheduler(selection_provider=lambda _snapshot, _payload: {})
    prompt = scheduler.system_prompt
    assert "rolling whole-domain coverage planner" in prompt
    assert "required_new_search_count is an EXACT parallel-allocation target" in prompt
    assert "zone_requirements lists deficit zones" in prompt
    assert "never invent a bbox" in prompt
    assert "160 characters" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/mission/test_mission_scheduler.py -q -k "rolling_coverage"`
Expected: FAIL — current prompt lacks the new lines.

- [ ] **Step 3: Replace `src/mission/prompts/mission_scheduler.txt`** with the full new text (hard-constraint lines preserved verbatim from the current file; new paragraphs marked NEW):

```text
You are the rolling whole-domain coverage planner for a mixed maritime
surveillance mission. Your job each round is not only to service contacts
but to keep the whole searchable domain covered: read the zone coverage
summary, then choose new work that closes the largest coverage gaps while
respecting every hard constraint below.

Return exactly one JSON object with this schema:
{
  "schema_version": "mission-selection/v1",
  "snapshot_id": "the supplied snapshot id",
  "selected_task_ids": ["task ids in priority order"],
  "preempt_uav_ids": ["ordinary-search UAV ids actually being preempted"],
  "defer_reason": null,
  "notes": "short operational explanation, at most 160 characters"
}

Select only supplied candidate task IDs. Search candidates already contain a
legal rectangle; never invent a bbox. Probe and track tasks reference the
supplied contact ID through the task record and have no search geometry.
Never select two supplied search candidates whose rectangles overlap.
Respect feasible_edges, remaining range, UAV generations, active tasks, map
version, contact state, intent status, and reviewer summary. For large snapshots
feasible_edges may be a compact per-task summary with per-UAV transit and total
range options; the server retains and checks the complete legal edge graph after
your selection. Existing approved
or executing tasks remain in force unless an explicitly legal ordinary search
UAV is preempted for a new probe or track. Returning, refueling, safety, active
probe, and valid tracking work must not be preempted.

When coverage_summary is present, use it as your primary planning input. It
reports per-zone unscanned and overdue cell counts, in-flight search tasks,
the effective gap fraction of every zone, and the whole-domain gap
percentage. Prefer candidates in high-gap zones. Zones whose effective gap
is already covered by in-flight tasks need less attention (the effective gap
already accounts for that work).

When coverage_constraint is present:
- required_new_search_count is an EXACT parallel-allocation target: select
  exactly that many ordinary search candidates, no more and no fewer, unless
  the server reports the floor infeasible via infeasible_reason.
- zone_requirements lists deficit zones that must be serviced: include every
  must_service_task_ids entry unless that zone reports an infeasible_reason.
  These zone tasks also count toward required_new_search_count.
Do not invent tasks or append hidden representatives. The validator rejects
notes longer than 160 characters.

The optional strategy_memories in the snapshot are advisory evidence from
validated earlier episodes. Use them only to prioritize the supplied tasks.
They never change sensor ranges, identity thresholds, navigation safety,
resource limits, or the legal feasible graph. Do not infer identities or quote
supporting episode details as current observations. An empty memory list means
baseline scheduling.

The complete snapshot follows in the user message. The resources, feasible
edge costs, active task records, contacts, intents, intent statuses, map
version, and reviewer summary are authoritative. A defer_reason documents why
work is deferred; it does not override a legal feasible task that can use an
otherwise idle UAV. Select all such visible work that can be assigned without
an additional preemption.

selected_task_ids are simultaneous assignments, NOT a sequential task queue.
Each selected task needs a distinct eligible UAV. Never count one UAV twice
or describe future sequential work in notes as justification for overbooking.
Busy search UAVs require explicit preemption; ordinary search and investigation
cannot preempt them unless the supplied policy explicitly permits it.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/mission/test_mission_scheduler.py -q -k "rolling_coverage or prompt"`
Expected: PASS (adjust the `-k` filter to whatever prompt test name exists in the file if needed — run the whole file if unsure).

- [ ] **Step 5: Update README chapter 2** — four edits:

5a. Line 5 intro (replace the sentence):

Old: `基于 LLM（LongCat）的 UAV 编队海上侦察动态任务调度系统。在 300 km × 300 km 海域中，10 架固定翼 UAV 执行区域覆盖搜索（SAR）与目标跟踪监视（EO/IR），LLM 作为全局决策器动态划分搜索区域，Hungarian 算法负责 UAV 与区域的最优配对。所有单 UAV 命令都经过统一的 \`ControlCoordinator\`，默认使用 heuristic 控制策略。`

New: `基于 LLM（LongCat）的 UAV 编队海上侦察动态任务调度系统。在 300 km × 300 km 海域中，10 架固定翼 UAV 执行区域覆盖搜索（SAR）与目标跟踪监视（EO/IR），LLM 作为全局决策器在程序生成的全域候选矩形中滚动选择搜索子区域（分区覆盖摘要 + 缺口自适应预算 + 分区配额硬约束），Hungarian 算法负责 UAV 与区域的最优配对。所有单 UAV 命令都经过统一的 \`ControlCoordinator\`，默认使用 heuristic 控制策略。`

5b. Section 2.1 five-layer chart, line 84 (replace the one label):

Old: `信息场 I(c,r)  ──→  价值场 V(c,r)  ──→  候选区域提取  ──→  LLM 区域划分  ──→  Hungarian 配对  ──→  UAV 路径规划`

New: `信息场 I(c,r)  ──→  价值场 V(c,r)  ──→  候选区域提取  ──→  LLM 跨区选择  ──→  Hungarian 配对  ──→  UAV 路径规划`

5c. Section 2.1 layer-3 box (lines 126–139), replace the bullet block:

Old:
```
│ 第 3 层：LLM 决策器 (LLMClient)                   │
│   ★ 唯一调用 LLM 的环节 ★                        │
│   · PromptBuilder 组装 System + User Prompt     │
│   · System Prompt：角色定义 + 7 条约束 + 输出格式  │
│     - 矩形性、尺寸 20-40格、长宽比≤2:1            │
│     - 不重叠、不覆盖跟踪区、数量≤可用UAV           │
│     - 稳定性(IoU≥0.7)、碎片合并、优先级            │
│   · User Prompt：候选区 + 跟踪区 + 上轮状态 +     │
│     碎片提醒 + UAV 状态 + Reviewer 长期记忆       │
│   · LongCat API → 输出 JSON 区域方案              │
│   · OutputValidator：7 条规则校验                 │
│   · 失败 → 错误回注到下一轮 Prompt → 重试 (≤2次)  │
│   · LLM 只输出"哪些矩形区域"，不做 UAV 配对       │
│   输出：经过校验的区域划分方案                     │
```

New:
```
│ 第 3 层：LLM 决策器 (MissionScheduler)             │
│   ★ 唯一调用 LLM 的环节 ★                        │
│   · System Prompt：滚动全域覆盖规划器角色定义      │
│     - 只选候选任务 ID，禁止自造/修改 bbox         │
│     - 搜索候选互不重叠、航路/资源/抢占硬约束       │
│   · User Prompt：分区覆盖摘要(未扫/超期/在途) +    │
│     候选区 + 跟踪区 + 上轮状态 + 碎片提醒 +        │
│     UAV 状态 + 覆盖约束 + Reviewer 长期记忆        │
│   · 覆盖约束：缺口自适应精确数量(恰好 N 个搜索区)   │
│     + 缺口分区配额(must-service)                  │
│   · OutputValidator：硬约束 + 精确数量 + 配额校验  │
│   · 失败 → 错误回注 → 重试，仍失败则 fail-closed  │
│   · LLM 只输出"选哪些候选矩形"，不做 UAV 配对     │
│   输出：经过校验的任务选择方案                     │
```

5d. Section 2.4 System Prompt sample (lines 252–269), replace the quoted prompt with the new one (excerpt):

```text
你是滚动全域覆盖规划器。任务：300×300km 海域对海侦察与目标跟踪。

【区域选择约束】
1. 只选候选任务 ID；搜索候选自带合法矩形，禁止自造或修改 bbox
2. 搜索候选互不重叠；航路、燃油、抢占规则必须遵守
3. coverage_summary 给出各分区未扫/超期/在途与有效缺口，优先选高缺口区
4. coverage_constraint.required_new_search_count 是精确数量：恰好选 N 个普通搜索候选
5. zone_requirements 的 must_service 必须选中（服务器报告 infeasible 时除外）
6. notes ≤ 160 字符

【输出格式】严格 JSON，无额外文字：
{
  "schema_version": "mission-selection/v1",
  "snapshot_id": "...",
  "selected_task_ids": ["..."],
  "preempt_uav_ids": [],
  "defer_reason": null,
  "notes": "..."
}
```

- [ ] **Step 6: Run the full prompt/README-adjacent suites**

Run: `python -m pytest tests/mission/test_mission_scheduler.py tests/mission/test_end_to_end.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mission/prompts/mission_scheduler.txt README.md tests/mission/test_mission_scheduler.py
git commit -m "docs: recast mission prompt as rolling coverage planner and correct README"
```

---

### Task 8: Acceptance scenarios, wiring verification, full regression

**Files:**
- Test: `tests/mission/test_zone_rolling_acceptance.py` (assemble the full file: the two composition tests from Task 5 + the validator + in-flight tests below)
- Modify: nothing in `src/` unless a regression surfaces.

**Interfaces:**
- Consumes: everything from Tasks 1–7.

- [ ] **Step 1: Assemble `tests/mission/test_zone_rolling_acceptance.py`**

Keep the two tests from Task 5 (fix the second test's comment to match the 4-fresh/5-deficit math). Add the remaining acceptance tests:

```python
def test_round_one_rejects_verify_heavy_selection_and_accepts_spread_one():
    # Spec section 9 scenario 1: full gap -> exact 10 search; "6 verify + 4
    # search" is rejected (wrong count + missing zone must-services); a
    # spread selection passes.
    fixed = np.ones((30, 30), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=3)
    summary = build_zone_coverage_summary(
        zones, now_min=0.0, last_sar=np.full((30, 30), -np.inf),
        feasible_mask=np.ones((30, 30), dtype=bool), primary_window_min=60,
    )
    fraction = adaptive_search_fraction(summary["gap_pct"])
    available = tuple(f"U{index}" for index in range(10))
    search_window = tuple(
        _search(f"z{zindex}", (zindex * 10 + 1, 1, zindex * 10 + 6, 6))
        for zindex in range(9)
    )
    verify_window = tuple(
        _verify(f"V{index}") for index in range(6)
    )
    window = (*verify_window, *search_window)
    quota_inputs = build_zone_quota_inputs(
        zones, summary, window, threshold=0.5, max_slots=10,
    )
    edges = tuple(
        _edge(task.task_id, uav_id)
        for task, uav_id in zip(window, available)
    )
    constraint = build_coverage_constraint(
        available_ids=available,
        active_search_count=0,
        representatives=(task.task_id for task in search_window),
        edges=edges,
        fraction=fraction,
        zone_requirements_input=quota_inputs,
    )
    snapshot = _snapshot(window, available, edges, constraint)

    verify_heavy = [task.task_id for task in verify_window] + [
        task.task_id for task in search_window[:4]
    ]
    errors = _validate(snapshot, verify_heavy)
    assert any(error.startswith("search_count_not_exact") for error in errors)
    assert any(error.startswith("zone_quota_not_met") for error in errors)

    spread = [task.task_id for task in search_window[:9]] + ["V0"]
    assert _validate(snapshot, spread) == []


def test_inflight_coverage_exempts_zone_from_quota():
    # Spec section 9 scenario 3: an in-flight task covers a zone's gap,
    # dropping its effective gap below threshold.
    fixed = np.ones((9, 3), dtype=bool)
    zones = ZonePartition(fixed, zone_cols=3, zone_rows=1)
    last_sar = np.full((9, 3), -np.inf)
    summary = build_zone_coverage_summary(
        zones, now_min=100.0, last_sar=last_sar,
        feasible_mask=np.ones((9, 3), dtype=bool), primary_window_min=60,
        in_flight_tasks=(_inflight("in-flight", (0, 0, 3, 3)),),
    )
    zone00 = next(z for z in summary["zones"] if z["zone_id"] == "zone:0:0")
    assert zone00["effective_gap_fraction"] == pytest.approx(0.0)
    window = (_search("z0", (0, 0, 2, 3)),)
    quota_inputs = build_zone_quota_inputs(zones, summary, window, threshold=0.5)
    assert [item.zone_id for item in quota_inputs] == ["zone:1:0", "zone:2:0"]
```

with the local helpers the file needs (add at the top, next to `_search`):

```python
from src.mission.contracts import CoverageConstraint, MissionSnapshot, TaskRecord, UavResource
from src.mission.mission_scheduler import MissionScheduler, SELECTION_SCHEMA


def _verify(task_id):
    return TaskCandidate(
        task_id, "probe", None, f"contact-{task_id}", (), ("U1",), 0.0, "high", 1.0, 1.0, 0.0,
    )


class _Inflight:
    def __init__(self, task_id, bbox):
        self.task_id = task_id
        self.bbox = bbox


def _inflight(task_id, bbox):
    return _Inflight(task_id, bbox)


def _snapshot(candidates, available, edges, constraint):
    resources = tuple(
        UavResource(uav_id, (0.0, 0.0), 0.0, 1.0, 100.0, "idle", None, 0, 0.0)
        for uav_id in available
    )
    return MissionSnapshot(
        snapshot_id="acceptance",
        sim_time_min=0.0,
        candidates=tuple(candidates),
        available_uav_ids=available,
        preemptible_uav_ids=(),
        uav_generations=tuple((uav_id, 0) for uav_id in available),
        resources=resources,
        feasible_edges=tuple(edges),
        active_tasks=(),
        contacts=(),
        intents=(),
        intent_statuses=(),
        memory_version="baseline",
        planning_map_version=0,
        reviewer_summary="",
        prompt_task_ids=tuple(task.task_id for task in candidates),
        prompt_sources=tuple((task.task_id, "ordinary") for task in candidates),
        coverage_constraint=constraint,
        coverage_summary={"gap_pct": 100.0},
    )


def _validate(snapshot, selected_ids):
    scheduler = MissionScheduler(selection_provider=lambda _snapshot, _payload: {})
    payload = {
        "schema_version": SELECTION_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "selected_task_ids": selected_ids,
        "preempt_uav_ids": [],
        "defer_reason": None,
        "notes": "",
    }
    return scheduler.validate_selection(payload, snapshot)
```

- [ ] **Step 2: Wiring test with the engine fixture**

Append to the same file:

```python
def test_snapshot_wiring_emits_summary_and_zone_quota_constraint():
    # Uses the real StateManager + TaskAllocator wiring (Task 5).
    from tests.mission.test_coverage_scan_integration import _engine

    engine = _engine()
    sm = engine.allocator.sm
    fixed = np.ones((30, 30), dtype=bool)
    sm.configure_coverage_metrics(fixed, episode_id="wiring-episode")
    sm.current_time = 0.0

    snapshot = engine.allocator.build_mission_snapshot()
    assert snapshot.coverage_summary is not None
    assert snapshot.coverage_summary["schema_version"] == "zone-coverage-summary/v1"
    assert len(snapshot.coverage_summary["zones"]) == 9
    constraint = snapshot.coverage_constraint
    assert constraint is not None
    assert constraint.desired_search_count == len(snapshot.available_uav_ids)
    assert constraint.infeasible_reason is None
    assert {item.zone_id for item in constraint.zone_requirements} == {
        zone["zone_id"] for zone in snapshot.coverage_summary["zones"]
    }
    assert set(constraint.must_service_task_ids) <= {
        candidate.task_id for candidate in snapshot.candidates
    }
```

Caveat for the executor: `_engine` builds a real `TaskAllocator` (needs `configs/.env` — run in the main workspace). If `_engine`'s candidate pool does not contain a fully-contained candidate for every zone (obstacles/bases in the fixture map), relax the zone_requirements assertion to `<=` the summary zones and assert `>= 1` zone matched; record the actual count in the commit message. If `configure_coverage_metrics` is not available on that `StateManager` version, use `StateManager(ConfigLoader.load())` + a manually built `TaskAllocator` mirroring `_engine`'s construction — inspect `_engine`'s body first.

- [ ] **Step 3: Run the acceptance suite**

Run: `python -m pytest tests/mission/test_zone_rolling_acceptance.py -v`
Expected: PASS (all acceptance tests).

- [ ] **Step 4: Full regression**

Run: `python -m pytest tests/mission/ tests/regressions/ -q`
Expected: PASS — the Task 1–7 suites plus every pre-existing mission/regression test stay green. If a failure names a coverage-constraint expectation that predates this plan (e.g., another `coverage_floor_not_met` string outside the two files updated above), update that test's expectation to the exact-count error and note it in the commit message.

Also run the schedule suite: `python -m pytest tests/schedule/ -q` (PASS).

- [ ] **Step 5: Verify the acceptance effects end-to-end (manual evidence)**

Run one scenario console pass (optional but recommended): `python -m scripts.persistent_coverage_scenarios --help` to find the scenario entry point, then run the default scenario and confirm in the log that round one's snapshot `coverage_summary.gap_pct ≈ 100`, `coverage_constraint.required_new_search_count == len(available_uav_ids)`, and `zone_requirements` covers all deficit zones. Record the log excerpt in the commit message body.

- [ ] **Step 6: Commit**

```bash
git add tests/mission/test_zone_rolling_acceptance.py
git commit -m "test: zone rolling coverage acceptance scenarios and wiring verification"
```

---

## Self-Review Notes (spec → plan trace)

- R1 zone summary → Task 1 (`build_zone_coverage_summary`) + Task 5 wiring + Task 6 payload embed.
- R2 dispersed window → Task 4 (`select_window zones` rotation).
- R3 exact-count adaptive budget → Task 2 (`adaptive_search_fraction` + builder anchor/ceil/exact) + Task 6 validator.
- R4 zone quota hard constraint → Task 2 (prioritized single matching, `zone_requirements`/`zone_infeasible`), Task 3 (contract), Task 6 (validator enforcement).
- R5 in-flight deduction → Task 1 (`effective_gap_fraction`, `in_flight_tasks`) + Task 8 scenario 3.
- R6 rolling updates → Task 5 (per-decision recomputation) + Task 8 scenario 2; existing heavy triggers untouched.
- R7 compatibility → `zones=None` byte-identical (Task 4 test), constraint default-empty (Task 3 tests), legacy path `None, None` (Task 5 Step 4 regression run), full regression in Task 8 Step 4.
- R8 prompt + README → Task 7.
- Spec section 7 error codes → Task 6 tests assert `search_count_not_exact`, `zone_quota_not_met`, `zone_must_service_not_selected`; infeasible tolerance verified by the unchanged `test_validator_allows_partial_floor_when_snapshot_reports_infeasible_resources`.
- Spec section 9 acceptance scenarios 1–3 → Task 8 Step 1 tests, one per scenario.
- Calculation-order invariant (`required ≥ matched zone count`) → enforced structurally: quota inputs are capped at `required_raw` (Task 5 helper), and `required = min(required_raw, matched_slots) ≥ matched zones` (Task 2 builder); covered by `test_constraint_zone_quota_visits_before_global_representatives` and `test_round_one_full_gap_demands_exact_parallel_search_and_zone_quotas`.

"""Probe why the top band never receives search regions.

Constructs the engine like main.py, advances a few sim minutes without LLM
decisions, then dumps per-cell coverage classes and candidate pool entries
overlapping the band cols 8-18 rows 0-8.
"""
import sys

import numpy as np

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.candidate_extractor import CandidateExtractor
from src.mission.coverage_policy import CoveragePolicy


def main() -> None:
    config = ConfigLoader.load("configs")
    engine = SimulationEngine(config)
    sm = engine.allocator.sm

    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    for _ in range(steps):
        try:
            engine.step()
        except Exception as exc:  # noqa: BLE001 - probe only
            print("step failed:", type(exc).__name__, exc)
            break
        if engine.runtime_status != "running":
            print("status:", engine.runtime_status)
            break

    now = float(sm.current_time)
    print(f"t={now} status={engine.runtime_status}")

    metrics = sm.coverage_metrics
    fixed = np.asarray(metrics.fixed_mask, dtype=bool)
    feasible = np.asarray(sm.get_searchable_mask(), dtype=bool)
    last_sar = np.asarray(metrics.last_scan_matrix(), dtype=float)
    print("last_sar finite:", int(np.isfinite(last_sar).sum()))

    extractor = CandidateExtractor()
    ctx = extractor._coverage_context(sm)
    pool = extractor.extract_pool(sm)
    candidates = list(pool.candidates)
    print("pool candidates:", len(candidates),
          "| unschedulable:", len(pool.unschedulable_cells),
          "| fragment_alerts:", len(pool.fragment_alerts))

    occupied = ~feasible
    assigned = np.zeros(fixed.shape, dtype=bool)
    for region in (
        *sm.get_track_regions(),
        *CandidateExtractor._valid_active_search_regions(sm),
    ):
        b = region.bbox
        occupied[b.col_start:b.col_end, b.row_start:b.row_end] = True
        assigned[b.col_start:b.col_end, b.row_start:b.row_end] = True

    regular_union = np.zeros(fixed.shape, dtype=bool)
    for cand in candidates:
        bbox = cand.get("bbox") if isinstance(cand, dict) else getattr(cand, "bbox", None)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        regular_union[x0:x1, y0:y1] = True

    policy = ctx["policy"] if ctx else CoveragePolicy(fixed)
    legal = regular_union
    classes = policy.classify(
        now_min=now,
        last_sar=ctx["last_sar"] if ctx else last_sar,
        feasible_mask=feasible,
        assigned_mask=assigned,
        legal_candidate_mask=legal,
    )
    for name, mask in classes.items():
        print(f"class {name}: {int(mask.sum())} cells")

    band = np.zeros(fixed.shape, dtype=bool)
    band[8:19, 0:9] = True
    print("\nBAND (x8-18,y0-8) class counts:")
    for name, mask in classes.items():
        print(f"  {name}: {int((mask & band).sum())}/99")
    print("  feasible:", int((feasible & band).sum()))
    print("  ~searchable:", int((~feasible & band).sum()))
    print("  regular_union:", int((regular_union & band).sum()))
    print("  assigned:", int((assigned & band).sum()))
    print("  ever scanned (finite last_sar):", int((np.isfinite(last_sar) & band).sum()))

    hit = []
    for cand in candidates:
        b = getattr(cand, "bbox", None)
        if b is None:
            continue
        x0, y0, x1, y1 = b
        if x0 < 19 and x1 > 8 and y0 < 9:
            hit.append(cand)
    print(f"\ncandidates overlapping band: {len(hit)} / {len(candidates)}")
    for cand in hit[:15]:
        print(" ", cand.task_id, tuple(cand.bbox),
              "cells", len(getattr(cand, "cells", ())),
              "val", round(getattr(cand, "total_value", 0), 3),
              "unseen", round(getattr(cand, "unseen_fraction", 0), 2),
              "kind", getattr(cand, "kind", "?"))
    # also: which band cells are covered by ANY candidate
    covered_band = np.zeros(fixed.shape, dtype=bool)
    for cand in candidates:
        b = getattr(cand, "bbox", None)
        if b is not None:
            covered_band[b[0]:b[2], b[1]:b[3]] = True
    print("band cells covered by any candidate:", int((covered_band & band).sum()), "/99")


if __name__ == "__main__":
    main()

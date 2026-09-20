"""Run the maritime simulation and report the eight-hour acceptance metrics."""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.env.simulation import SimulationEngine  # noqa: E402
from src.schedule.config_loader import ConfigLoader  # noqa: E402
from src.schedule.task_allocator import TaskAllocator  # noqa: E402
from src.mission.coverage_policy import rank_search_candidates  # noqa: E402


CHECKPOINTS = (120, 240, 360, 480)


@dataclass(frozen=True)
class _HotspotCandidate:
    task_id: str
    bbox: tuple[int, int, int, int]

    @property
    def cells(self):
        return tuple(
            (col, row)
            for col in range(self.bbox[0], self.bbox[2])
            for row in range(self.bbox[1], self.bbox[3])
        )


def _hotspot_fixture():
    shape = (614, 611)
    last_sar = np.full(shape, -np.inf, dtype=float)
    last_sar[::7, ::11] = 0.0
    candidates = []
    estimated = {}
    for index in range(256):
        col = 2 + (index * 17) % 570
        row = 2 + (index * 23) % 560
        candidate = _HotspotCandidate(
            f"search:{index}", (col, row, col + 24, row + 24)
        )
        candidates.append(candidate)
        estimated[candidate.task_id] = 12.0
    return candidates, last_sar, estimated


def _head_sha() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return value or None


def evaluate_branch1_hotspots(repeat: int = 10, seed: int = 42) -> dict:
    """Measure the deterministic scheduling/cache hot paths used by Task 9."""
    if repeat < 1:
        raise ValueError("repeat must be positive")
    seed = int(seed)  # The fixture is deterministic; retain the CLI seed contract.
    candidates, last_sar, estimated = _hotspot_fixture()
    rank_times_ms = []
    for _ in range(repeat):
        started = time.perf_counter()
        ranked = rank_search_candidates(
            candidates,
            now_min=120.0,
            last_sar=last_sar,
            estimated_minutes=estimated,
        )
        rank_times_ms.append((time.perf_counter() - started) * 1000.0)
        if len(ranked) != len(candidates):
            raise RuntimeError("hotspot fixture changed candidate cardinality")

    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    snapshot = allocator.build_mission_snapshot(0.0)
    light_times_ms = []
    for index in range(repeat):
        started = time.perf_counter()
        allocator._handle_light_mission_trigger(
            float(index + 1),
            SimpleNamespace(),
            snapshot.active_tasks,
            intents=snapshot.intents,
            intent_statuses=snapshot.intent_statuses,
        )
        light_times_ms.append((time.perf_counter() - started) * 1000.0)

    def percentile(values, fraction):
        ordered = sorted(values)
        position = (len(ordered) - 1) * fraction
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "scenario": "branch1-hotspots",
        "status": "finished",
        "seed": seed,
        "head_sha": _head_sha(),
        "repeat": repeat,
        "fixture_cells": int(last_sar.size),
        "rank_search_candidates_ms": {
            "p50": round(statistics.median(rank_times_ms), 3),
            "p95": round(percentile(rank_times_ms, 0.95), 3),
        },
        "light_snapshot_ms": {
            "p50": round(statistics.median(light_times_ms), 3),
            "p95": round(percentile(light_times_ms, 0.95), 3),
        },
        "cache_peak_entries": {
            "route": len(allocator._mission_route_cache),
            "metrics": len(allocator._mission_route_metrics_cache),
            "geometry": allocator.extractor._pool_geometry_cache_limit,
            "task_cells": 2048,
        },
        "thresholds": {
            "route": 512,
            "metrics": 512,
            "geometry": 2048,
        },
    }


def _timeliness_metrics(engine: SimulationEngine) -> dict[str, float]:
    state = engine.allocator.sm
    searchable = state.get_searchable_mask()
    info = state.get_info_matrix()[searchable]
    last_scan = state.info_field.last_scan_time[searchable]
    age = np.where(np.isfinite(last_scan), state.current_time - last_scan, np.inf)
    return {
        "coverage_pct": round(float(np.isfinite(last_scan).mean() * 100.0), 3),
        "avg_info": round(float(info.mean()), 4),
        "black_pct": round(float((info < state.config.grid.gray_threshold).mean() * 100.0), 3),
        "white_pct": round(float((info > state.config.grid.white_threshold).mean() * 100.0), 3),
        "stale_pct": round(float((age > 60.0).mean() * 100.0), 3),
    }


def _theoretical_limits(engine: SimulationEngine) -> dict[str, float | int]:
    """Upper bounds assuming every UAV spends all time in valid SAR imaging."""
    cfg = engine.config
    searchable_cells = int(engine.allocator.sm.get_searchable_mask().sum())
    swath_cells = cfg.sensor.sar.swath_km / cfg.grid.cell_size_km
    cells_per_hour_per_uav = (
        cfg.uav.cruise_speed_kmh / cfg.grid.cell_size_km * swath_cells
    )
    fleet_cells_per_hour = cfg.uav.count_max * cells_per_hour_per_uav
    white_window_min = (
        cfg.grid.decay_half_life_min
        * math.log2(1.0 / cfg.grid.white_threshold)
    )
    fresh_window_min = (
        cfg.grid.decay_half_life_min
        * math.log2(1.0 / cfg.grid.gray_threshold)
    )
    max_white_pct = min(
        100.0,
        fleet_cells_per_hour * white_window_min / 60.0
        / max(searchable_cells, 1) * 100.0,
    )
    min_black_pct = max(
        0.0,
        100.0 - fleet_cells_per_hour * fresh_window_min / 60.0
        / max(searchable_cells, 1) * 100.0,
    )
    required_white_uavs = math.ceil(
        0.20 * searchable_cells
        / (cells_per_hour_per_uav * white_window_min / 60.0)
    )
    required_black_uavs = math.ceil(
        0.65 * searchable_cells
        / (cells_per_hour_per_uav * fresh_window_min / 60.0)
    )
    return {
        "fleet_cells_per_hour_at_100pct_sar": round(fleet_cells_per_hour, 3),
        "white_window_min": round(white_window_min, 3),
        "fresh_window_min": round(fresh_window_min, 3),
        "max_white_pct_at_100pct_sar": round(max_white_pct, 3),
        "min_black_pct_at_100pct_sar": round(min_black_pct, 3),
        "uavs_needed_for_20pct_white": required_white_uavs,
        "uavs_needed_for_35pct_black": required_black_uavs,
    }


def evaluate(steps: int = 480, seed: int = 42) -> dict:
    config = ConfigLoader.load()
    engine = SimulationEngine(config, seed=seed)
    status_totals: Counter[str] = Counter()
    tracking_samples = 0
    revisit_intervals: list[float] = []
    checkpoints: dict[int, dict[str, float]] = {}
    previous_status = {uav.id: uav.status for uav in engine.uavs}
    sortie_search_minutes = defaultdict(float)
    completed_sortie_searches: list[float] = []

    for _ in range(steps):
        old_scan_times = engine.allocator.sm.info_field.last_scan_time.copy()
        engine.step()
        state = engine.allocator.sm
        current_scan_times = state.info_field.last_scan_time
        rescanned = (
            np.isfinite(old_scan_times)
            & np.isfinite(current_scan_times)
            & (current_scan_times > old_scan_times)
        )
        if np.any(rescanned):
            revisit_intervals.extend(
                (current_scan_times[rescanned] - old_scan_times[rescanned]).tolist()
            )

        for uav in engine.uavs:
            status = uav.status
            status_totals[status] += 1
            tracking_samples += int(status == "tracking")
            if previous_status[uav.id] in ("idle", "refueling") and status == "transit":
                sortie_search_minutes[uav.id] = 0.0
            if status == "searching":
                sortie_search_minutes[uav.id] += 1.0
            if previous_status[uav.id] != "returning" and status == "returning":
                if sortie_search_minutes[uav.id] > 0:
                    completed_sortie_searches.append(sortie_search_minutes[uav.id])
                sortie_search_minutes[uav.id] = 0.0
            previous_status[uav.id] = status

        current_time = int(state.current_time)
        if current_time in CHECKPOINTS:
            checkpoints[current_time] = _timeliness_metrics(engine)

    total_uav_minutes = steps * len(engine.uavs)
    searching_minutes = status_totals["searching"]
    tracking_minutes = status_totals["tracking"]
    airborne_observation_minutes = searching_minutes + tracking_minutes
    refuel_counts = np.asarray([base.refuel_count for base in engine.bases], dtype=float)
    mean_refuels = float(refuel_counts.mean()) if refuel_counts.size else 0.0
    summary = engine.summary()
    episode_outcome = summary.get("episode_outcome", {})
    confusion = episode_outcome.get("classification_confusion", {})
    classified_ships = sum(
        int(sum(row.values())) for row in confusion.values()
        if isinstance(row, dict)
    )
    classification_accuracy = episode_outcome.get("classification_accuracy")
    ais_accuracy_pct = (
        float(classification_accuracy) * 100.0
        if classification_accuracy is not None else None
    )
    final_metrics = _timeliness_metrics(engine)
    final_metrics.update({
        "median_revisit_min": round(float(np.median(revisit_intervals)), 3)
        if revisit_intervals else None,
        "search_time_pct": round(
            searching_minutes / airborne_observation_minutes * 100.0,
            3,
        ) if airborne_observation_minutes else 0.0,
        "idle_holding_pct": round(
            (status_totals["idle"] + status_totals["holding"])
            / total_uav_minutes * 100.0,
            3,
        ) if total_uav_minutes else 0.0,
        "tracking_uav_pct": round(
            tracking_samples / total_uav_minutes * 100.0,
            3,
        ) if total_uav_minutes else 0.0,
        "base_refuel_normalized_variance": round(
            float(np.var(refuel_counts / mean_refuels)), 5,
        ) if mean_refuels else 0.0,
        "avg_effective_search_min_per_sortie": round(
            float(np.mean(completed_sortie_searches)), 3,
        ) if completed_sortie_searches else 0.0,
        "completed_search_sorties": len(completed_sortie_searches),
        "ais_accuracy_pct": round(ais_accuracy_pct, 3)
        if ais_accuracy_pct is not None else 0.0,
        "ais_accuracy_definition": "terminal type_i/type_ii assessment accuracy",
        "classified_ships": classified_ships,
        "mixed_maritime_classification_accuracy": episode_outcome.get(
            "classification_accuracy"
        ),
        "mixed_maritime_terminal_classification_coverage": episode_outcome.get(
            "terminal_classification_coverage"
        ),
        "mixed_maritime_type_ii_misclassified_as_type_i_ratio": episode_outcome.get(
            "type_ii_misclassified_as_type_i_ratio"
        ),
    })
    return {
        "summary": summary,
        "theoretical_limits": _theoretical_limits(engine),
        "checkpoints": checkpoints,
        "final_metrics": final_metrics,
        "status_minutes": dict(sorted(status_totals.items())),
        "base_refuel_counts": dict(zip(
            (base.id for base in engine.bases),
            (int(value) for value in refuel_counts),
        )),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("branch1-hotspots",))
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--steps", type=int, default=480)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.scenario == "branch1-hotspots":
        payload = evaluate_branch1_hotspots(args.repeat, args.seed)
    else:
        payload = evaluate(args.steps, args.seed)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

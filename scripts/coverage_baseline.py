"""Offline coverage-throughput baseline: no LLM, deterministic scheduling only.

Runs the engine with an always-failing gateway so no network latency and the
deterministic fallback path owns tasking — exactly what the live run degrades
to when model calls fail. Reports cumulative SAR coverage_pct, rolling
persistent-coverage windows, and per-status UAV time usage so bottlenecks are
visible.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.env.simulation import SimulationEngine


class OfflineGateway:
    def _fail(self, role):
        return ModelResult(
            call_id=f"offline-{role}",
            success=False,
            payload=None,
            errors=("offline baseline",),
            failure_category="offline",
        )

    def request_json(self, *, role, **kwargs):
        # Red-commander decisions block the sim clock on failure, so the
        # baseline answers them itself with a schema-valid mild evasion plan.
        if role == "red_commander":
            payload = kwargs.get("user_payload") or {}
            snapshot = payload.get("snapshot") or {}
            commands = [
                {
                    "ship_id": ship_id,
                    "heading_offset_deg": 30.0,
                    "speed_kn": 12.0,
                    "zigzag_heading_deg": 0.0,
                    "zigzag_period_min": 10.0,
                    "phase_deg": 0.0,
                    "reason_content": "offline baseline evasive pattern",
                }
                for ship_id, _stage in snapshot.get("active_signature", ())
            ]
            return ModelResult(
                call_id="offline-red_commander",
                success=True,
                payload={
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "valid_for_min": 3.0,
                    "commands": commands,
                    "notes": "offline baseline",
                },
                errors=(),
                failure_category=None,
            )
        return self._fail(role)

    def request_text(self, *, role, **_kwargs):
        return self._fail(role)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=360)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--config", default="configs")
    parser.add_argument("--sample-every", type=int, default=30)
    args = parser.parse_args()

    config = ConfigLoader.load(args.config)
    engine = SimulationEngine(config, seed=args.seed, llm_gateway=OfflineGateway())
    sm = engine.allocator.sm

    fallback_calls = collections.Counter()
    scheduler = engine.allocator.mission_scheduler
    original_fallback = scheduler._deterministic_fallback_batch

    def counting_fallback(snapshot, *, visible_task_ids=None):
        batch = original_fallback(
            snapshot, visible_task_ids=visible_task_ids)
        fallback_calls["attempts"] += 1
        if batch is not None:
            fallback_calls["batches"] += 1
        return batch

    scheduler._deterministic_fallback_batch = counting_fallback

    status_time = collections.Counter()
    coverage_curve = []
    pauses = collections.Counter()
    started = time.monotonic()
    for step in range(args.steps):
        engine.step()
        attempts = 0
        while engine.runtime_status == "paused_model" and attempts < 10:
            pauses["paused_model_retries"] += 1
            attempts += 1
            engine.retry_blocked_decision()
        pauses[f"end_paused_{engine.runtime_status}"] += (
            0 if engine.runtime_status == "running" else 1
        )
        for uav in engine.uavs:
            status_time[uav.status] += 1
        t = sm.current_time
        if (step + 1) % args.sample_every == 0 or step == args.steps - 1:
            stats = sm.get_coverage_stats()
            persistent = sm.get_persistent_coverage_stats() if hasattr(
                sm, "get_persistent_coverage_stats") else None
            row = {
                "sim_min": t,
                "coverage_pct": round(stats["coverage_pct"], 2),
                "scanned_cells": stats["scanned_searchable_cells"],
                "searchable_cells": stats["searchable_cells"],
            }
            if persistent:
                row["rolling"] = {
                    w["minutes"]: w["coverage_pct"] for w in persistent["windows"]
                }
            coverage_curve.append(row)

    summary = engine.summary()
    out = {
        "seed": args.seed,
        "steps": args.steps,
        "wall_seconds": round(time.monotonic() - started, 1),
        "final_coverage_pct": summary["coverage_pct"],
        "searchable_cells": summary["searchable_cells"],
        "scanned_cells": summary["scanned_searchable_cells"],
        "uav_count": len(engine.uavs),
        "status_time_share": {
            k: round(v / (args.steps * len(engine.uavs)), 3)
            for k, v in status_time.most_common()
        },
        "heavy_triggers": summary["heavy_triggers"],
        "paused_model_retries": pauses["paused_model_retries"],
        "fallback": dict(fallback_calls),
        "detected_ships": summary["detected_ships"],
        "coverage_curve": coverage_curve,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

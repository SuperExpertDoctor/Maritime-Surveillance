"""Run the unmocked engine for a measured wall-clock interval and retain evidence."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.vis.backend.frame_builder import build_frame


class AuditedTransport:
    """Log timing without changing the production request or its response."""
    def __init__(self, delegate, path):
        self.delegate, self.path = delegate, path

    def complete(self, **kwargs):
        started = time.monotonic()
        record = {"role": kwargs["role"], "started_utc": datetime.now(timezone.utc).isoformat(),
                  "timeout_seconds": kwargs.get("timeout_seconds")}
        with self.path.open("a") as stream:
            stream.write(json.dumps({**record, "phase": "started"}) + "\n")
        try:
            result = self.delegate.complete(**kwargs)
            record["response_characters"] = len(result)
            return result
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            with self.path.open("a") as stream:
                stream.write(json.dumps({**record, "phase": "finished",
                                        "elapsed_seconds": time.monotonic() - started}) + "\n")


def run(output: Path, seconds: float, seed: int) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    engine = SimulationEngine(ConfigLoader.load(), seed=seed)
    gateway = engine.allocator.llm_client.gateway
    gateway.assert_ready()
    gateway.transport = AuditedTransport(gateway.transport, output / "transport.jsonl")
    manifest = {
        "transport": "live", "seed": seed, "requested_wall_seconds": seconds,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "roles": {"surveillance": "red", "opposing_vessels": "blue",
                  "legacy_opponent_api_role": "red_commander"},
        "scenario": "configured mixed maritime, no synthetic observations or model responses",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    started = time.monotonic()
    count = 0
    observed_events = set()
    event_counts = Counter()
    released_classes = Counter()
    error = None
    with (output / "frames.jsonl").open("w") as frames, (output / "events.jsonl").open("w") as events:
        def collect_events():
            # Include pre-tick events omitted by the frame's open-left window.
            for event in engine.allocator.sm.get_recent_events(0.0):
                key = json.dumps(event, sort_keys=True, ensure_ascii=False)
                if key in observed_events:
                    continue
                events.write(key + "\n")
                observed_events.add(key)
                event_counts[event["type"]] += 1
                if event["type"] == "vessel_created":
                    released_classes[event.get("data", {}).get("vessel_class", "unknown")] += 1
            events.flush()

        try:
            while time.monotonic() - started < seconds:
                before = engine.clock.time
                gateway.set_context(engine.episode_id, "baseline", before)
                result = engine.step()
                frame = build_frame(
                    engine.allocator.sm, engine.allocator.sm.cycle, engine.config,
                    ships=engine.ships, uav_entities=engine.uavs,
                    obstacles=engine.obstacles, bases=engine.bases,
                    llm_cycle=result.get("llm_cycle"),
                )
                frames.write(json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n")
                frames.flush()
                count += 1
                collect_events()
                elapsed = time.monotonic() - started
                progress = {"wall_seconds": round(elapsed, 2), "sim_time_min": engine.clock.time,
                            "runtime_status": engine.runtime_status, "frames": count,
                            "model_calls": len(gateway.call_log),
                            "coverage": frame.get("coverage_metrics")}
                (output / "progress.json").write_text(json.dumps(progress, indent=2) + "\n")
                print(json.dumps({k: v for k, v in progress.items() if k != "coverage"}), flush=True)
                if engine.runtime_status != "running" or engine.clock.time <= before:
                    break
        except Exception as exc:
            error = gateway._redact(f"{type(exc).__name__}: {exc}")
        finally:
            # step/build/serialization may fail after the engine emitted events.
            collect_events()
    elapsed = time.monotonic() - started
    calls = gateway.redact_log(gateway.call_log)
    (output / "model_calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2) + "\n")
    successful = Counter(call["role"] for call in calls if call["success"])
    checks = {
        "wall_duration_met": elapsed >= seconds,
        "runtime_running": engine.runtime_status == "running" and error is None,
        "time_advanced": engine.clock.time > 0 and count > 1,
        "surveillance_llm_called": successful["decision_maker"] > 0,
        "opponent_llm_called": successful["red_commander"] > 0,
        "opponent_maneuvers_installed": event_counts["opponent_maneuver_installed"] > 0,
        "type_i_released": released_classes["type_i"] > 0,
        "type_ii_released": released_classes["type_ii"] > 0,
    }
    report = {"passed": all(checks.values()), "checks": checks, "wall_seconds": elapsed,
              "frames": count, "runtime_status": engine.runtime_status, "error": error,
              "successful_calls_by_role": dict(successful), "event_counts": dict(event_counts),
              "released_classes": dict(released_classes), "summary": engine.summary()}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "summary"}, ensure_ascii=False), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=480.)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.seconds < float("inf"):
        parser.error("seconds must be finite and positive")
    raise SystemExit(0 if run(args.output_dir, args.seconds, args.seed)["passed"] else 1)

"""Capture telemetry-only persistent-coverage baselines from the real engine."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.persistent_coverage_scenarios import (  # noqa: E402
    COVERAGE_SCENARIOS,
    build_coverage_scenario,
    coverage_fixture_source_hash,
)
from scripts.replay_restoration_scenarios import capture_frame  # noqa: E402


def _wall_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _stable_hash(value) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _git_commit() -> str | None:
    value = _git_output("rev-parse", "HEAD").strip()
    return value or None


def _dirty_diff_hash() -> str:
    patch = _git_output("diff", "--binary", "HEAD", "--", ".")
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_stable_json(payload) + "\n")


def _role_bindings(engine) -> dict:
    gateway = engine.allocator.llm_client.gateway
    bindings = {}
    for role in ("decision_maker", "contact_assessor", "red_commander", "reviewer"):
        value = gateway.resolve_binding(role)
        bindings[role] = {
            "model": value.get("model"),
            "provider": value.get("provider"),
            "temperature": value.get("temperature"),
            "max_tokens": value.get("max_tokens"),
            "thinking": value.get("thinking"),
        }
    return bindings


def _event_identity(episode_id: str, event: dict) -> tuple:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return (
        episode_id,
        event.get("type"),
        event.get("time"),
        data.get("uav_id"),
    )


def _capture_seed(scenario: str, seed: int, steps: int, output_dir: Path) -> dict:
    seed_dir = output_dir / f"seed-{seed}"
    seed_dir.mkdir()
    frames_path = seed_dir / "frames.jsonl"
    events_path = seed_dir / "events.jsonl"
    manifest_path = seed_dir / "manifest.json"
    started_at = _wall_time()
    engine = None
    manifest = {
        "schema_version": "persistent-coverage-baseline-seed/v1",
        "baseline_only": True,
        "coverage_gate_status": "not_evaluated",
        "status": "running",
        "scenario": scenario,
        "seed": seed,
        "transport": "fixture",
        "fixture": True,
        "fixture_quality_scope": "deterministic model boundary; not real-LLM quality",
        "fixture_source_sha256": coverage_fixture_source_hash(),
        "git_commit": _git_commit(),
        "dirty_diff_sha256": _dirty_diff_hash(),
        "requested_steps": steps,
        "completed_steps": 0,
        "actual_end_time_min": None,
        "clock_dt_min": None,
        "episode_id": None,
        "config": None,
        "config_sha256": None,
        "role_bindings": None,
        "platform": {
            "python": platform.python_version(),
            "system": platform.platform(),
        },
        "started_at_wall_utc": started_at,
        "artifacts": {"frames": "frames.jsonl", "events": "events.jsonl"},
    }
    _write_json(manifest_path, manifest)

    seen_events: dict[tuple, str] = {}
    frame_count = 0
    event_count = 0

    def record_frame() -> None:
        nonlocal frame_count, event_count
        frame = capture_frame(engine, total_steps=steps)
        _append_jsonl(frames_path, frame)
        frame_count += 1
        for event in frame.get("events", ()):
            identity = _event_identity(engine.episode_id, event)
            payload = _stable_json(event)
            # Only SAR observations require one payload per identity. Runtime
            # history may contain distinct contacts or reasons at the same tick.
            key = identity if event.get("type") == "sar_scan" else (*identity, payload)
            if key in seen_events:
                if seen_events[key] != payload:
                    raise ValueError(f"conflicting event payload for identity {identity!r}")
                continue
            _append_jsonl(events_path, event)
            seen_events[key] = payload
            event_count += 1

    status = "completed"
    stopped_reason = None
    completed_steps = 0
    interrupted = False
    capture_terminal = False
    try:
        engine = build_coverage_scenario(scenario, seed=seed, transport="fixture")
        config = asdict(engine.config)
        manifest.update({
            "actual_end_time_min": float(engine.clock.time),
            "clock_dt_min": float(engine.clock.dt_min),
            "episode_id": engine.episode_id,
            "config": config,
            "config_sha256": _stable_hash(config),
            "role_bindings": _role_bindings(engine),
        })
        _write_json(manifest_path, manifest)
        record_frame()
        for _ in range(steps):
            before = float(engine.clock.time)
            engine.step()
            after = float(engine.clock.time)
            if after > before:
                completed_steps += 1
            record_frame()
            if engine.runtime_status != "running" or after <= before:
                status = "incomplete"
                stopped_reason = (
                    f"runtime_status={engine.runtime_status}; "
                    f"clock advanced from {before} to {after}"
                )
                break
    except KeyboardInterrupt:
        status = "incomplete"
        stopped_reason = "capture interrupted"
        interrupted = True
        capture_terminal = True
    except Exception as exc:
        status = "failed"
        stopped_reason = f"{type(exc).__name__}: {exc}"
        capture_terminal = True

    # A step can advance the clock before failing. Preserve its observed
    # terminal state without counting the unfinished step as completed.
    if capture_terminal and frame_count:
        try:
            record_frame()
        except (Exception, KeyboardInterrupt) as exc:
            interrupted = interrupted or isinstance(exc, KeyboardInterrupt)
            stopped_reason += f"; terminal capture failed: {type(exc).__name__}: {exc}"

    if completed_steps != steps and status == "completed":
        status = "incomplete"
        stopped_reason = "requested step count was not reached"
    if not events_path.exists():
        events_path.write_text("", encoding="utf-8")
    if not frames_path.exists():
        frames_path.write_text("", encoding="utf-8")
    runtime_status = engine.runtime_status if engine is not None else None
    actual_end_time = float(engine.clock.time) if engine is not None else None
    manifest.update({
        "status": status,
        "runtime_status": runtime_status,
        "stopped_reason": stopped_reason,
        "interrupted": interrupted,
        "completed_steps": completed_steps,
        "actual_end_time_min": actual_end_time,
        "frame_count": frame_count,
        "event_count": event_count,
        "finished_at_wall_utc": _wall_time(),
    })
    _write_json(manifest_path, manifest)
    return {
        "seed": seed,
        "status": status,
        "runtime_status": runtime_status,
        "completed_steps": completed_steps,
        "actual_end_time_min": actual_end_time,
        "frame_count": frame_count,
        "event_count": event_count,
        "stopped_reason": stopped_reason,
        "interrupted": interrupted,
        "manifest": str(manifest_path.relative_to(output_dir)),
    }


def capture_baseline(*, scenario: str, seeds: tuple[int, ...], steps: int,
                     output_dir: Path) -> dict:
    if scenario not in COVERAGE_SCENARIOS:
        raise ValueError(f"unknown persistent coverage scenario: {scenario}")
    if isinstance(steps, bool) or steps < 1:
        raise ValueError("steps must be positive")
    if not seeds or len(seeds) != len(set(seeds)):
        raise ValueError("seeds must be non-empty and unique")
    if (output_dir / "manifest.json").exists() or (
        output_dir.exists() and any(output_dir.iterdir())
    ):
        raise FileExistsError(f"refusing to overwrite existing baseline: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    root_manifest = {
        "schema_version": "persistent-coverage-baseline/v1",
        "baseline_only": True,
        "coverage_gate_status": "not_evaluated",
        "status": "running",
        "scenario": scenario,
        "transport": "fixture",
        "seeds": list(seeds),
        "requested_steps_per_seed": steps,
        "git_commit": _git_commit(),
        "dirty_diff_sha256": _dirty_diff_hash(),
        "fixture_source_sha256": coverage_fixture_source_hash(),
        "started_at_wall_utc": _wall_time(),
        "runs": [],
    }
    _write_json(output_dir / "manifest.json", root_manifest)
    runs = []
    for seed in seeds:
        run = _capture_seed(scenario, seed, steps, output_dir)
        runs.append(run)
        root_manifest["runs"] = runs
        _write_json(output_dir / "manifest.json", root_manifest)
        if run["interrupted"]:
            break

    complete = len(runs) == len(seeds) and all(run["status"] == "completed" for run in runs)
    root_manifest.update({
        "status": "completed" if complete else "incomplete",
        "finished_at_wall_utc": _wall_time(),
        "runs": runs,
    })
    summary = {
        "schema_version": "persistent-coverage-baseline-summary/v1",
        "baseline_only": True,
        "coverage_gate_status": "not_evaluated",
        "status": root_manifest["status"],
        "scenario": scenario,
        "requested_steps_per_seed": steps,
        "runs": runs,
    }
    _write_json(output_dir / "summary.json", summary)
    _write_json(output_dir / "manifest.json", root_manifest)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=COVERAGE_SCENARIOS)
    parser.add_argument("--seeds", required=True, nargs="+", type=int)
    parser.add_argument("--steps", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        summary = capture_baseline(
            scenario=args.scenario,
            seeds=tuple(args.seeds),
            steps=args.steps,
            output_dir=args.output_dir,
        )
    except (FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(_stable_json(summary))
    return 0 if summary["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

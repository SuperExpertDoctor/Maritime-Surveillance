"""Evaluate one mixed-maritime scenario or print its live-run budget."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCENARIOS = (
    "mixed-ais",
    "all-civilian",
    "silent-target",
    "disguised-target",
    "island-confounder",
    "no-resources",
    "intent-overlap",
    "model-failure",
)


def _stable_hash(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    return commit or None


def _model_bindings(engine) -> dict:
    gateway = engine.allocator.llm_client.gateway
    bindings = {}
    for role in ("decision_maker", "contact_assessor", "red_commander", "reviewer"):
        binding = gateway.resolve_binding(role)
        bindings[role] = {
            key: binding.get(key)
            for key in ("model", "provider", "temperature", "max_tokens", "thinking")
        }
    return bindings


def _refuse_overwrite(output_dir: Path) -> None:
    if (output_dir / "manifest.json").exists():
        raise SystemExit(f"refusing to overwrite existing run: {output_dir}")


def _role_call_summary(engine) -> dict:
    calls = getattr(engine.allocator.llm_client.gateway, "call_log", ())
    counts = {
        role: {"calls": 0, "failures": 0}
        for role in ("decision_maker", "contact_assessor", "red_commander", "reviewer")
    }
    for call in calls:
        role = str(call.get("role", "unknown"))
        entry = counts.setdefault(role, {"calls": 0, "failures": 0})
        entry["calls"] += 1
        entry["failures"] += int(not call.get("success", False))
    return {"by_role": counts, "total": len(calls)}


def _safe_call_record(call: dict) -> dict:
    """Persist attempt metadata without raw prompts or model responses."""
    attempts = call.get("attempts", ())
    if not isinstance(attempts, (list, tuple)):
        attempts = ()
    return {
        "call_id": call.get("call_id"),
        "role": call.get("role"),
        "episode_id": call.get("episode_id"),
        "snapshot_id": call.get("snapshot_id"),
        "sim_time_min": call.get("sim_time_min", 0.0),
        "memory_version": call.get("memory_version", "baseline"),
        "model": call.get("model"),
        "prompt_hash": _stable_hash(
            attempts[0].get("messages", [])
            if attempts and isinstance(attempts[0], dict) else []
        ),
        "attempts": [
            {
                "attempt": item.get("attempt"),
                "message_hash": _stable_hash(item.get("messages", [])),
                "output_hash": (
                    _stable_hash(item.get("raw_output"))
                    if item.get("raw_output") is not None else None
                ),
                "had_output": bool(item.get("raw_output")),
                "errors": item.get("errors", []),
            }
            for item in attempts
            if isinstance(item, dict)
        ],
        "validation_errors": call.get("validation_errors", []),
        "success": bool(call.get("success", False)),
        "failure_category": call.get("failure_category"),
    }


def _record_call_logs(logger, engine) -> None:
    for call in getattr(engine.allocator.llm_client.gateway, "call_log", ()):
        role = str(call.get("role", "unknown"))
        domain = "red" if role == "red_commander" else "blue"
        logger.append(domain, "decisions", _safe_call_record(call))


def _write_manifest(output_dir: Path, payload: dict) -> None:
    temporary = output_dir / "manifest.tmp"
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output_dir / "manifest.json")


def _dry_run(args) -> dict:
    return {
        "mode": "dry-run",
        "scenario": args.scenario,
        "seed": args.seed,
        "steps": args.steps,
        "planned_episodes": 1,
        "api_calls": 0,
        "requires_live": True,
        "output_dir": str(args.output_dir),
    }


def _scenario_config(config, scenario: str):
    """Apply only deterministic, production-safe scenario changes."""
    if scenario == "all-civilian":
        return replace(config, ship=replace(config.ship, target_ship_count=0))
    if scenario == "silent-target":
        return replace(config, ship=replace(config.ship, target_ais_on_probability=0.0))
    if scenario == "disguised-target":
        return replace(config, ship=replace(config.ship, target_ais_on_probability=1.0))
    if scenario == "island-confounder":
        return replace(
            config,
            environment=replace(
                config.environment, island_count_min=1, island_count_max=1,
            ),
        )
    if scenario == "no-resources":
        # Production keeps the coordinator's ownership set valid while
        # reducing the fleet to one constrained resource.  The exact
        # unavailable-resource fixture remains test-only.
        return replace(config, uav=replace(config.uav, count_max=1))
    return config


def _scenario_intents(engine, scenario: str) -> None:
    if scenario != "intent-overlap":
        return
    for label, bbox in (
        ("north focus", [5, 5, 13, 14]),
        ("overlap focus", [9, 10, 18, 19]),
    ):
        engine.intents.create(
            {
                "label": label,
                "bbox": bbox,
                "mode": "maintain_freshness",
                "priority": "high",
                "weight": 0.5,
                "valid_duration_min": 120.0,
                "revisit_interval_min": 20.0,
            },
            0.0,
        )
    engine.allocator.sm.add_event("intent_changed", {"scenario": scenario})
    engine.allocator.trigger_manager.notify_event(
        "intent_changed", time=0.0, intent_id="scenario-intent",
    )


def _live_run(args) -> dict:
    _refuse_overwrite(args.output_dir)
    from src.env.simulation import SimulationEngine
    from src.mission.episode_logger import EpisodeLogger
    from src.schedule.config_loader import ConfigLoader

    config = _scenario_config(ConfigLoader.load(), args.scenario)
    engine = SimulationEngine(config, seed=args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    episode_logger = EpisodeLogger(args.output_dir / "episodes")
    episode_logger.start({
        "episode_id": engine.episode_id,
        "is_fixture": False,
        "live": True,
        "scenario": args.scenario,
        "seed": args.seed,
        "steps": args.steps,
        "memory_version": engine.allocator.memory_version,
        "config_hash": _stable_hash(config),
        "git_commit": _git_commit(),
        "model_bindings": _model_bindings(engine),
    })
    manifest = {
        "schema_version": "mixed-maritime-evaluation/v1",
        "status": "running",
        "scenario": args.scenario,
        "seed": args.seed,
        "steps": args.steps,
        "is_fixture": False,
        "live": True,
        "scenario_config": {
            "target_ship_count": config.ship.target_ship_count,
            "target_ais_on_probability": config.ship.target_ais_on_probability,
            "island_count_min": config.environment.island_count_min,
            "island_count_max": config.environment.island_count_max,
            "uav_count_max": config.uav.count_max,
        },
        "episode_id": engine.episode_id,
        "config_hash": _stable_hash(config),
        "git_commit": _git_commit(),
        "model_bindings": _model_bindings(engine),
    }
    _write_manifest(args.output_dir, manifest)
    try:
        _scenario_intents(engine, args.scenario)
        summary = engine.run(args.steps)
        _record_call_logs(episode_logger, engine)
        episode_logger.append("evaluation", "outcomes", summary["episode_outcome"])
        episode_logger.finish("completed")
    except Exception as exc:
        episode_logger.finish("failed")
        manifest.update({
            "status": "failed",
            "error_type": type(exc).__name__,
            "episode_log": str(episode_logger.episode_dir),
            "role_calls": _role_call_summary(engine),
        })
        _write_manifest(args.output_dir, manifest)
        return {
            "mode": "live",
            "status": "failed",
            "manifest": str(args.output_dir / "manifest.json"),
            "error_type": type(exc).__name__,
        }

    manifest.update({
        "status": "finished",
        "summary": summary,
        "role_calls": _role_call_summary(engine),
        "episode_log": str(episode_logger.episode_dir),
    })
    _write_manifest(args.output_dir, manifest)
    return {
        "mode": "live",
        "status": "finished",
        "manifest": str(args.output_dir / "manifest.json"),
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="mixed-ais")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--steps", type=int, default=120)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/evaluations/mixed-maritime"))
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be positive")
    payload = _live_run(args) if args.live else _dry_run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

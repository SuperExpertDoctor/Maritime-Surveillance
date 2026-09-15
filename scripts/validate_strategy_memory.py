"""Validate, activate, or roll back a scheduling strategy memory."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

VALIDATION_EPISODES = 60
HOLDOUT_EPISODES = 30
ROLE_CALLS_PER_EPISODE = 4
EVALUATION_SCHEMA = "strategy-evaluation/v1"


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    return commit or None


def _refuse_overwrite(output_dir: Path) -> None:
    if (output_dir / "manifest.json").exists():
        raise SystemExit(f"refusing to overwrite existing evaluation: {output_dir}")


def _run_budget(phase: str) -> int:
    return VALIDATION_EPISODES if phase == "validation" else HOLDOUT_EPISODES


def _dry_run(args) -> dict:
    episodes = _run_budget(args.phase)
    return {
        "mode": "dry-run",
        "phase": args.phase,
        "memory_id": args.memory_id,
        "baseline_version": args.baseline_version,
        "planned_episodes": episodes,
        "api_calls": 0,
        "estimated_role_calls": episodes * ROLE_CALLS_PER_EPISODE,
        "requires_live": True,
        "output_dir": str(args.output_dir),
    }


def _write_manifest(output_dir: Path, payload: dict) -> Path:
    manifest_path = output_dir / "manifest.json"
    temporary = output_dir / "manifest.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest_path


def _episode_seeds(config, phase: str, count: int) -> tuple[int, ...]:
    configured = tuple(
        config.mission.evolution.validation_seeds
        if phase == "validation"
        else config.mission.evolution.holdout_seeds
    )
    if not configured:
        raise ValueError(f"no {phase} seeds configured")
    # Keep the configured validation/holdout pools disjoint while allowing the
    # default 60/30 budget to exceed the short seed lists in older configs.
    seeds = []
    for index in range(count):
        source = configured[index % len(configured)]
        cycle = index // len(configured)
        seeds.append(int(source) + cycle * 1_000_003)
    return tuple(seeds)


def _config_hash(config) -> str:
    encoded = json.dumps(asdict(config), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _episode_outcome(summary):
    from src.mission.outcome_evaluator import EpisodeOutcome

    payload = summary.get("episode_outcome")
    if not isinstance(payload, dict):
        raise ValueError("simulation summary did not contain episode_outcome")
    return EpisodeOutcome(**payload)


def _role_call_summary(engine) -> dict:
    calls = getattr(engine.allocator.llm_client.gateway, "call_log", ())
    by_role: dict[str, int] = {}
    failures: dict[str, int] = {}
    for call in calls:
        role = str(call.get("role", "unknown"))
        by_role[role] = by_role.get(role, 0) + 1
        if not call.get("success", False):
            failures[role] = failures.get(role, 0) + 1
    return {"counts": by_role, "failures": failures, "total": len(calls)}


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
        "prompt_hash": hashlib.sha256(
            json.dumps(
                attempts[0].get("messages", [])
                if attempts and isinstance(attempts[0], dict) else [],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "attempts": [
            {
                "attempt": item.get("attempt"),
                "message_hash": hashlib.sha256(
                    json.dumps(
                        item.get("messages", []),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest(),
                "output_hash": (
                    hashlib.sha256(
                        str(item.get("raw_output")).encode("utf-8")
                    ).hexdigest()
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


def _run_episode(
    config,
    store,
    memory_version: str,
    seed: int,
    steps: int,
    phase: str,
    index: int,
    log_root: Path,
):
    from src.env.simulation import SimulationEngine
    from src.mission.episode_logger import EpisodeLogger

    episode_id = f"{phase}-{index:03d}-{uuid4().hex[:10]}"
    episode_logger = EpisodeLogger(log_root)
    episode_logger.start({
        "episode_id": episode_id,
        "is_fixture": False,
        "phase": phase,
        "seed": seed,
        "memory_version": memory_version,
        "live": True,
    })
    try:
        engine = SimulationEngine(
            config,
            seed=seed,
            episode_id=episode_id,
            strategy_memory_store=store,
            strategy_memory_version=memory_version,
        )
        summary = engine.run(steps)
        outcome = _episode_outcome(summary)
        for call in getattr(engine.allocator.llm_client.gateway, "call_log", ()):
            role = str(call.get("role", "unknown"))
            domain = "red" if role == "red_commander" else "blue"
            episode_logger.append(
                domain,
                "decisions",
                _safe_call_record({
                    **call,
                    "episode_id": call.get("episode_id", episode_id),
                    "memory_version": call.get("memory_version", memory_version),
                }),
            )
        episode_logger.append("evaluation", "outcomes", asdict(outcome))
        episode_logger.finish("completed")
    except Exception:
        episode_logger.finish("failed")
        raise
    return outcome, {
        "phase": phase,
        "pair_index": index,
        "seed": seed,
        "memory_version": memory_version,
        "episode_id": outcome.episode_id,
        "runtime_status": engine.runtime_status,
        "valid": outcome.valid,
        "invalid_reasons": list(outcome.invalid_reasons),
        "outcome": asdict(outcome),
        "role_calls": _role_call_summary(engine),
        "episode_log": str(episode_logger.episode_dir),
    }


def _weighted_optional(first, first_count, second, second_count):
    values = [(first, first_count), (second, second_count)]
    available = [(float(value), count) for value, count in values if value is not None and count]
    if not available:
        return None
    denominator = sum(count for _, count in available)
    return sum(value * count for value, count in available) / denominator


def _merge_phase_reports(validation, holdout):
    validation_count = len(validation.validation_episode_pairs)
    holdout_count = len(holdout.holdout_episode_pairs)
    keys = set(validation.component_deltas) | set(holdout.component_deltas)
    deltas = {
        key: _weighted_optional(
            validation.component_deltas.get(key), validation_count,
            holdout.component_deltas.get(key), holdout_count,
        )
        for key in keys
    }
    reasons = tuple(dict.fromkeys((*validation.reasons, *holdout.reasons)))
    return replace(
        holdout,
        report_id=f"R{uuid4().hex[:10]}",
        validation_episode_pairs=validation.validation_episode_pairs,
        holdout_episode_pairs=holdout.holdout_episode_pairs,
        mean_score_gain=_weighted_optional(
            validation.mean_score_gain, validation_count,
            holdout.mean_score_gain, holdout_count,
        ),
        component_deltas=deltas,
        false_civilian_delta=_weighted_optional(
            validation.false_civilian_delta, validation_count,
            holdout.false_civilian_delta, holdout_count,
        ),
        passed=validation.passed and holdout.passed and not reasons,
        reasons=reasons,
    )


def _run_live(args) -> dict:
    _refuse_overwrite(args.output_dir)
    from src.mission.strategy_memory import StrategyMemoryStore, evaluate_paired_outcomes
    from src.schedule.config_loader import ConfigLoader

    store = StrategyMemoryStore(args.memory_root)
    config = ConfigLoader.load()
    candidate_version, candidate = store.find_memory(args.memory_id)
    if candidate.status not in {"candidate", "validated", "active"}:
        raise SystemExit(f"memory {args.memory_id} is not eligible for live validation")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    config_hash = _config_hash(config)
    planned = _run_budget(args.phase)
    manifest = {
        "schema_version": EVALUATION_SCHEMA,
        "status": "running",
        "phase": args.phase,
        "memory_id": args.memory_id,
        "candidate_version": candidate_version,
        "baseline_version": args.baseline_version,
        "steps": args.steps,
        "planned_episodes": planned,
        "config_hash": config_hash,
        "git_commit": _git_commit(),
        "live": True,
        "pairs": [],
    }
    _write_manifest(args.output_dir, manifest)

    temporary_validation = candidate.status == "candidate"
    if temporary_validation:
        store.set_status(args.memory_id, "validated")
    baseline_outcomes = []
    candidate_outcomes = []
    pair_records = []
    try:
        from src.mission.outcome_evaluator import EpisodeOutcome

        for index, seed in enumerate(_episode_seeds(config, args.phase, planned)):
            baseline, baseline_record = _run_episode(
                config, store, args.baseline_version, seed, args.steps, args.phase, index,
                args.output_dir / "episodes",
            )
            candidate_outcome, candidate_record = _run_episode(
                config, store, candidate_version, seed, args.steps, args.phase, index,
                args.output_dir / "episodes",
            )
            if not isinstance(baseline, EpisodeOutcome) or not isinstance(candidate_outcome, EpisodeOutcome):
                raise ValueError("paired run returned an invalid outcome type")
            baseline_outcomes.append(baseline)
            candidate_outcomes.append(candidate_outcome)
            pair_records.append({"baseline": baseline_record, "candidate": candidate_record})
            manifest["pairs"] = pair_records
            _write_manifest(args.output_dir, manifest)

        report = evaluate_paired_outcomes(
            tuple(baseline_outcomes),
            tuple(candidate_outcomes),
            {
                "phase": args.phase,
                "memory_id": args.memory_id,
                "baseline_version": args.baseline_version,
                "baseline_config_hash": config_hash,
                "candidate_config_hash": config_hash,
            },
        )
        store.save_validation_report(report)
        report_id = report.report_id
        if args.phase == "holdout":
            previous = [
                item for item in store.validation_reports()
                if item.memory_id == args.memory_id
                and item.baseline_version == args.baseline_version
                and item.validation_episode_pairs
                and not item.holdout_episode_pairs
            ]
            if previous:
                report = _merge_phase_reports(previous[-1], report)
                store.save_validation_report(report)
                report_id = report.report_id
        manifest.update({
            "status": "finished",
            "report_id": report_id,
            "report": asdict(report),
            "memory_status_restored": temporary_validation,
        })
        _write_manifest(args.output_dir, manifest)
        return {
            "mode": "live",
            "status": "finished",
            "report_id": report_id,
            "manifest": str(args.output_dir / "manifest.json"),
            "passed": report.passed,
            "planned_episodes": planned,
        }
    except Exception as exc:
        manifest.update({
            "status": "failed",
            "error_type": type(exc).__name__,
            "completed_pairs": len(pair_records),
        })
        _write_manifest(args.output_dir, manifest)
        return {
            "mode": "live",
            "status": "failed",
            "error_type": type(exc).__name__,
            "manifest": str(args.output_dir / "manifest.json"),
            "completed_pairs": len(pair_records),
        }
    finally:
        if temporary_validation:
            store.set_status(args.memory_id, "candidate")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--activate", metavar="MEMORY_ID")
    mode.add_argument("--rollback", metavar="VERSION")
    parser.add_argument("--memory-id")
    parser.add_argument("--report-id")
    parser.add_argument("--baseline-version", default="baseline")
    parser.add_argument("--phase", choices=("validation", "holdout"), default="validation")
    parser.add_argument("--steps", type=int, default=120)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/evaluations/strategy-memory"))
    parser.add_argument("--memory-root", type=Path, default=Path("outputs/strategy_memory"))
    args = parser.parse_args()

    if args.steps < 1:
        parser.error("--steps must be positive")

    if args.activate:
        if not args.report_id:
            parser.error("--activate requires --report-id")
        if args.live:
            parser.error("--activate cannot be combined with --live")
        from src.mission.strategy_memory import StrategyMemoryStore

        version = StrategyMemoryStore(args.memory_root).activate(args.activate, args.report_id)
        print(json.dumps({"mode": "activate", "memory_id": args.activate, "version": version}))
        return
    if args.rollback:
        if args.live:
            parser.error("--rollback cannot be combined with --live")
        from src.mission.strategy_memory import StrategyMemoryStore

        version = StrategyMemoryStore(args.memory_root).rollback(args.rollback)
        print(json.dumps({"mode": "rollback", "version": version}))
        return
    if not args.memory_id:
        parser.error("run mode requires --memory-id")
    payload = _run_live(args) if args.live else _dry_run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

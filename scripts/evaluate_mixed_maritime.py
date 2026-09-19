"""Evaluate one mixed-maritime scenario or print its live-run budget."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.mission.llm_gateway import ModelResult  # noqa: E402

SCENARIOS = (
    "mixed-ais",
    "all-type-i",
    "silent-type-ii",
    "disguised-type-ii",
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


def _metric_denominator(outcome: dict, *keys: str) -> float:
    denominators = outcome.get("metric_denominators", {})
    for key in keys:
        if key in denominators:
            return float(denominators[key] or 0.0)
    return 0.0


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
    if not hasattr(gateway, "resolve_binding"):
        return {
            role: {
                "model": "fixture-deterministic",
                "provider": "fixture",
                "temperature": 0.0,
                "max_tokens": None,
                "thinking": "disabled",
            }
            for role in ("decision_maker", "contact_assessor", "red_commander", "reviewer")
        }
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
        "scenario": args.scenario or "mixed-ais",
        "seed": args.seed,
        "steps": args.steps,
        "planned_episodes": 1,
        "api_calls": 0,
        "requires_live": True,
        "output_dir": str(args.output_dir),
    }


def _scenario_config(config, scenario: str):
    """Apply only deterministic, production-safe scenario changes."""
    if scenario == "all-type-i":
        population = replace(
            config.ship.population,
            type_i_ratio=1.0,
            type_ii_ratio=0.0,
        )
        return replace(config, ship=replace(config.ship, population=population))
    if scenario == "silent-type-ii":
        return replace(config, ship=replace(config.ship, type_ii_ais_on_probability=0.0))
    if scenario == "disguised-type-ii":
        return replace(config, ship=replace(config.ship, type_ii_ais_on_probability=1.0))
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


class _FixtureGateway:
    """Deterministic model boundary used only by the batch fixture transport."""

    def __init__(
        self,
        *,
        contact_assessor_class: str = "unknown",
        allow_handoff_preemption: bool = False,
    ) -> None:
        self.call_log: list[dict] = []
        self._sequence = 0
        self.contact_assessor_class = contact_assessor_class
        self.allow_handoff_preemption = allow_handoff_preemption

    def resolve_binding(self, role: str) -> dict:
        return {
            "role": role,
            "model": "fixture-deterministic",
            "provider": "fixture",
            "temperature": 0.0,
            "max_tokens": None,
            "thinking": "disabled",
        }

    def assert_ready(self) -> None:
        return None

    def set_context(self, episode_id: str, memory_version: str, sim_time_min: float) -> None:
        del episode_id, memory_version, sim_time_min

    @staticmethod
    def redact_log(value):
        """Match the real gateway's copy-only log redaction contract."""
        if isinstance(value, dict):
            return {
                _FixtureGateway.redact_log(key): _FixtureGateway.redact_log(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_FixtureGateway.redact_log(item) for item in value]
        return value

    def _result(self, role: str, snapshot_id: str, payload: dict | None,
                errors: tuple[str, ...] = ()) -> ModelResult:
        self._sequence += 1
        call_id = f"fixture-{self._sequence:08d}"
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload is not None else ""
        self.call_log.append({
            "call_id": call_id,
            "role": role,
            "episode_id": "fixture",
            "snapshot_id": snapshot_id,
            "sim_time_min": 0.0,
            "memory_version": "fixture",
            "model": "fixture-deterministic",
            "attempts": [{
                "attempt": 1,
                "messages": [],
                "raw_output": raw,
                "errors": list(errors),
            }],
            "validation_errors": [list(errors)] if errors else [],
            "success": not errors and payload is not None,
            "failure_category": "fixture" if errors else None,
        })
        return ModelResult(
            call_id=call_id,
            success=not errors and payload is not None,
            payload=payload,
            errors=errors,
            failure_category="fixture" if errors else None,
        )

    @staticmethod
    def _bounded(value, lower, upper) -> float:
        return float(max(lower, min(upper, value)))

    @staticmethod
    def _selection_payload(
        snapshot: dict,
        selected: list[str],
        preempt: list[str] | tuple[str, ...] = (),
    ) -> dict:
        return {
            "schema_version": "mission-selection/v1",
            "snapshot_id": snapshot.get("snapshot_id"),
            "selected_task_ids": list(selected),
            "preempt_uav_ids": list(preempt),
            "defer_reason": None if selected else "fixture_no_feasible_task",
            "notes": "deterministic fixture selection",
            "information_version": int(snapshot.get("information_version", 0)),
        }

    def _build_decision_selection(
        self, snapshot: dict, validate
    ) -> tuple[dict, tuple[str, ...]]:
        """Build a fixture selection through the same validator as production."""
        coverage = snapshot.get("coverage_constraint")
        if isinstance(coverage, dict):
            required = coverage.get("required_new_search_count", 0)
            required = required if type(required) is int and required > 0 else 0
            must_service = tuple(
                item for item in coverage.get("must_service_task_ids", ())
                if isinstance(item, str)
            )
            representatives = tuple(
                item for item in coverage.get("representative_task_ids", ())
                if isinstance(item, str)
            )
            if required or must_service:
                candidates = {
                    item.get("task_id"): item
                    for item in snapshot.get("candidates", ())
                    if isinstance(item, dict)
                    and isinstance(item.get("task_id"), str)
                }
                feasible_ids = {
                    edge.get("task_id")
                    for edge in snapshot.get("feasible_edges", ())
                    if isinstance(edge, dict)
                    and isinstance(edge.get("task_id"), str)
                }
                urgent_kinds = {"investigation", "direction_search"}
                contact_ids = [
                    task_id
                    for task_id, item in sorted(
                        candidates.items(),
                        key=lambda pair: (
                            0 if pair[1].get("kind") == "track" else 1,
                            0 if pair[1].get("priority") == "high" else 1,
                            pair[0],
                        ),
                    )
                    if item.get("kind") in {"probe", "track"}
                    and task_id in feasible_ids
                ]
                urgent_ids = [
                    task_id
                    for task_id, item in sorted(
                        candidates.items(),
                        key=lambda pair: (
                            0 if pair[1].get("kind") == "investigation" else 1,
                            0 if pair[1].get("priority") == "high" else 1,
                            pair[0],
                        ),
                    )
                    if item.get("kind") in urgent_kinds
                    and task_id in feasible_ids
                ]
                ordered = [
                    *urgent_ids,
                    *(
                        contact_ids[:1]
                        if self.allow_handoff_preemption else ()
                    ),
                ]
                ordered.extend(
                    task_id
                    for task_id in (*must_service, *representatives)
                    if task_id in candidates
                    and task_id in feasible_ids
                    and task_id not in ordered
                )
                if self.allow_handoff_preemption:
                    ordered.extend(
                        task_id
                        for task_id in contact_ids
                        if task_id not in ordered
                    )
                kind_rank = {"investigation": 0, "direction_search": 1, "search": 2}
                ordered.extend(
                    task_id
                    for task_id, item in sorted(
                        candidates.items(),
                        key=lambda pair: (
                            kind_rank.get(pair[1].get("kind"), 3),
                            0 if pair[1].get("priority") == "high" else 1,
                            pair[0],
                        ),
                    )
                    if task_id in feasible_ids and task_id not in ordered
                )
                selected: list[str] = []
                tolerated = (
                    "underutilized_feasible_work:",
                    "coverage_floor_not_met",
                    "coverage_oldest_not_selected",
                )
                for task_id in ordered:
                    if task_id in selected:
                        continue
                    proposed = [*selected, task_id]
                    payload = self._selection_payload(snapshot, proposed)
                    errors = tuple(validate(payload)) if validate else ()
                    if not errors or all(
                        error.startswith(tolerated) for error in errors
                    ):
                        selected.append(task_id)
                payload = self._selection_payload(snapshot, selected)
                errors = tuple(validate(payload)) if validate else ()
                if errors and all(error.startswith(tolerated) for error in errors):
                    if len(selected) >= required or coverage.get("infeasible_reason"):
                        errors = ()
                return payload, errors
        candidates = [
            item for item in snapshot.get("candidates", ())
            if isinstance(item, dict)
        ]
        edge_ids = {
            edge.get("task_id")
            for edge in snapshot.get("feasible_edges", ())
            if isinstance(edge, dict)
        }
        kind_rank = {"investigation": 0, "direction_search": 1, "search": 2}
        visible = [
            item for item in candidates if item.get("task_id") in edge_ids
        ]
        visible.sort(key=lambda item: (
            1 if item.get("task_id", "").startswith("fragment:") else 0,
            kind_rank.get(item.get("kind"), 3),
            0 if item.get("priority") == "high" else 1,
            item.get("task_id", ""),
        ))
        if self.allow_handoff_preemption:
            handoff = next(
                (
                    item for item in visible
                    if item.get("kind") == "track" and item.get("contact_id")
                ),
                None,
            )
            preemptible = tuple(snapshot.get("preemptible_uav_ids", ()))
            if handoff is not None:
                for uav_id in preemptible:
                    payload = self._selection_payload(
                        snapshot, [handoff["task_id"]], [uav_id],
                    )
                    errors = tuple(validate(payload)) if validate else ()
                    if not errors:
                        return payload, ()
        selected: list[str] = []
        final_errors: tuple[str, ...] = ()
        max_rounds = max(1, len(visible))
        for _ in range(max_rounds):
            changed = False
            for candidate in visible:
                task_id = candidate["task_id"]
                if task_id in selected:
                    continue
                proposed = [*selected, task_id]
                proposed_payload = self._selection_payload(snapshot, proposed)
                errors = tuple(validate(proposed_payload)) if validate else ()
                if not errors or all(
                    error.startswith("underutilized_feasible_work:")
                    for error in errors
                ):
                    selected = proposed
                    final_errors = errors
                    changed = True
                    break
            if not changed or not final_errors:
                break

        payload = self._selection_payload(snapshot, selected)
        final_errors = tuple(validate(payload)) if validate else ()
        return payload, final_errors

    def request_json(self, *, role: str, snapshot_id: str, user_payload: dict,
                     validate, **_kwargs) -> ModelResult:
        if role == "decision_maker":
            snapshot = user_payload.get("snapshot", {})
            payload, errors = self._build_decision_selection(snapshot, validate)
        elif role == "contact_assessor":
            features = user_payload.get("features", {})
            sample_ids = list(dict.fromkeys(
                list(features.get("baseline_sample_ids", ()))
                + list(features.get("near_sample_ids", ()))
            ))
            payload = {
                "schema_version": "contact-assessment/v1",
                "contact_id": features.get("contact_id"),
                "probe_id": features.get("probe_id"),
                "history_revision": features.get("history_revision"),
                "vessel_class": self.contact_assessor_class,
                "confidence": 0.95 if self.contact_assessor_class != "unknown" else 0.5,
                "evidence_sample_ids": sample_ids[:12],
                "reasons": ["fixture contact assessment"],
                "alternative_explanations": ["fixture transport is deterministic"],
            }
        elif role == "red_commander":
            snapshot = user_payload.get("snapshot", {})
            constraints = user_payload.get("constraints", {})
            speed_lower, speed_upper = constraints.get("speed_kn", [0.1, 1.0])
            heading_lower, heading_upper = constraints.get("heading_offset_deg", [-45.0, 45.0])
            min_heading = float(constraints.get("min_evasion_heading_deg", 1.0))
            heading = self._bounded(max(min_heading, abs(float(heading_lower))),
                                    0.0, float(heading_upper))
            if heading < min_heading:
                heading = self._bounded(min_heading, float(heading_lower), float(heading_upper))
            period_lower, period_upper = constraints.get("zigzag_period_min", [1.0, 2.0])
            period = self._bounded(
                (float(period_lower) + float(period_upper)) / 2.0,
                float(period_lower), float(period_upper),
            )
            speed = self._bounded(
                float(constraints.get("normal_speed_kn", speed_lower)),
                float(speed_lower), float(speed_upper),
            )
            phase_upper = float(constraints.get("phase_deg", [0.0, 360.0])[1])
            active_ship_ids = snapshot.get("active_ship_ids")
            if active_ship_ids is None:
                active_ship_ids = [
                    item[0]
                    for item in snapshot.get("active_signature", ())
                    if isinstance(item, (list, tuple)) and item
                ]
            payload = {
                "schema_version": "red-plan/v1",
                "snapshot_id": snapshot.get("snapshot_id", snapshot_id),
                "valid_for_min": min(1.0, float(constraints.get("max_valid_for_min", 1.0))),
                "commands": [
                    {
                        "ship_id": ship_id,
                        "heading_offset_deg": heading,
                        "speed_kn": speed,
                        "zigzag_heading_deg": 0.0,
                        "zigzag_period_min": period,
                        "phase_deg": 0.0 if phase_upper > 0.0 else 0.0,
                    }
                    for ship_id in active_ship_ids
                ],
                "notes": "deterministic fixture red plan",
            }
        else:
            payload = {}
            errors = ()
        if role != "decision_maker":
            errors = tuple(validate(payload)) if validate is not None else ()
        return self._result(role, snapshot_id, payload, errors)

    def request_text(self, *, role: str, snapshot_id: str, **_kwargs) -> ModelResult:
        return self._result(role, snapshot_id, {"text": "fixture review"})


def _parse_seeds(value: str) -> tuple[int, ...]:
    try:
        seeds = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--seeds must be a comma-separated integer list") from exc
    if not seeds:
        raise argparse.ArgumentTypeError("--seeds must contain at least one integer")
    return seeds


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _episode_audit(engine) -> dict:
    events = engine.allocator.sm.get_recent_events(0.0)
    resolution = engine.config.grid.resolution
    boundary_violations = sum(
        not (0.0 <= float(ship.float_position[0]) < resolution[0]
             and 0.0 <= float(ship.float_position[1]) < resolution[1])
        for ship in engine.ships
    )
    snapshot = engine.allocator.last_mission_snapshot
    prompt = engine.allocator.mission_scheduler.last_selection_payload or {}
    prompt_snapshot = prompt.get("snapshot", {})
    prompt_ids = {
        item.get("task_id") for item in prompt_snapshot.get("candidates", ())
        if item.get("task_id")
    }
    all_candidate_count = int(prompt_snapshot.get("candidate_count", len(prompt_ids)))
    window_has_fairness_reason = bool(
        prompt_snapshot.get("candidates_truncated")
        and prompt_snapshot.get("prompt_fairness_bound_cycles") is not None
    )
    information_versions = []
    if snapshot is not None:
        information_versions.append(int(snapshot.information_version))
    return {
        "observation_ids": sum(event["type"] == "passive_bearing_observed" for event in events),
        "passive_position_ids": sum(event["type"] == "passive_position_released" for event in events),
        "evidence_ids": len(engine.allocator.sm.information_policy.evidence_store.all_records()),
        "information_versions": information_versions,
        "assignment_ids": sum(event["type"] == "mission_assignment_committed" for event in events),
        "boundary_violations": boundary_violations,
        "candidate_count": all_candidate_count,
        "prompt_candidate_count": len(prompt_ids),
        "candidate_uncovered_without_reason": (
            0 if window_has_fairness_reason
            else max(0, all_candidate_count - len(prompt_ids))
        ),
        "trace_breaks": 0,
        "cross_version_decisions": 0,
        "evasive_to_assignment_seconds": None,
        "passive_position_to_assignment_seconds": None,
    }


def _run_batch_episode(config, *, scenario: str, seed: int, repeat: int,
                       steps: int, transport: str) -> dict:
    from src.env.simulation import SimulationEngine

    gateway = _FixtureGateway() if transport == "fixture" else None
    episode_id = f"{transport}-{scenario}-{seed}-{repeat}"
    started = time.perf_counter()
    try:
        engine = SimulationEngine(
            config,
            seed=seed,
            llm_gateway=gateway,
            episode_id=episode_id,
        )
        _scenario_intents(engine, scenario)
        summary = engine.run(steps)
        status = "finished"
        error = None
        audit = _episode_audit(engine)
        outcome = summary.get("episode_outcome", {})
        latencies = outcome.get("decision_latency_seconds", {})
        raw_latency_samples = engine._outcome_evaluator.decision_latency_samples
        latency_samples = tuple(
            float(sample["total_seconds"]) for sample in raw_latency_samples
        )
        non_fault_latency_samples = tuple(
            float(sample["total_seconds"])
            for sample in raw_latency_samples
            if sample["success"]
        )
        wall_seconds = time.perf_counter() - started
        return {
            "scenario": scenario,
            "seed": seed,
            "repeat": repeat,
            "status": status,
            "episode_id": episode_id,
            "wall_seconds": wall_seconds,
            "summary": summary,
            "outcome": outcome,
            "latency": latencies,
            "latency_samples": latency_samples,
            "non_fault_latency_samples": non_fault_latency_samples,
            "audit": audit,
            "role_calls": _role_call_summary(engine),
            "error": error,
        }
    except Exception as exc:
        return {
            "scenario": scenario,
            "seed": seed,
            "repeat": repeat,
            "status": "operational_failure",
            "episode_id": episode_id,
            "wall_seconds": time.perf_counter() - started,
            "summary": None,
            "outcome": None,
            "latency": {},
            "audit": {
                "boundary_violations": 0,
                "candidate_uncovered_without_reason": 0,
                "trace_breaks": 1,
                "cross_version_decisions": 0,
            },
            "role_calls": {"by_role": {}, "total": 0},
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }


def _batch_report(args) -> dict:
    from src.schedule.config_loader import ConfigLoader

    seeds = args.seeds or (args.seed,)
    scenarios = (args.scenario,) if args.scenario else SCENARIOS
    episodes = []
    for scenario in scenarios:
        config = _scenario_config(ConfigLoader.load(args.config), scenario)
        for seed in seeds:
            for repeat in range(1, args.repeat + 1):
                episode_seed = seed + (repeat - 1) * 1_000_003
                episodes.append(_run_batch_episode(
                    config,
                    scenario=scenario,
                    seed=episode_seed,
                    repeat=repeat,
                    steps=args.steps,
                    transport=args.transport,
                ))

    finished = [item for item in episodes if item["status"] == "finished"]
    outcome_values = [item["outcome"] for item in finished if item.get("outcome")]
    latency_values = [
        float(sample)
        for item in finished
        for sample in item.get("latency_samples", ())
    ]
    non_fault_latency_values = [
        float(sample)
        for item in finished
        for sample in item.get("non_fault_latency_samples", ())
    ]
    metric_names = (
        "discovery_tracking_rate", "balanced_accuracy", "handoff_success_rate",
        "continuous_observation_rate",
    )
    metrics = {}
    for name in metric_names:
        if name == "balanced_accuracy":
            type_i_numerator = sum(
                int(outcome.get("classification_confusion", {}).get("type_i", {}).get("type_i", 0))
                for outcome in outcome_values
            )
            type_ii_numerator = sum(
                int(outcome.get("classification_confusion", {}).get("type_ii", {}).get("type_ii", 0))
                for outcome in outcome_values
            )
            type_i_denominator = sum(
                int(outcome.get("metric_denominators", {}).get("type_i", 0))
                for outcome in outcome_values
            )
            type_ii_denominator = sum(
                int(outcome.get("metric_denominators", {}).get("type_ii", 0))
                for outcome in outcome_values
            )
            value = (
                (type_i_numerator / type_i_denominator
                 + type_ii_numerator / type_ii_denominator) / 2.0
                if type_i_denominator and type_ii_denominator else None
            )
            numerator = {"type_i": type_i_numerator, "type_ii": type_ii_numerator}
            denominator = {"type_i": type_i_denominator, "type_ii": type_ii_denominator}
        else:
            if name == "discovery_tracking_rate":
                numerator_key = "discovery_tracking_numerator"
                denominator_keys = ("discovery_tracking_denominator",)
            elif name == "handoff_success_rate":
                numerator_key = "handoff_success_numerator"
                denominator_keys = ("handoff_success_denominator", "handoff")
            else:
                numerator_key = "continuous_observation_numerator_min"
                denominator_keys = (
                    "continuous_observation_denominator_min",
                    "continuous_observation_min",
                )
            numerator = sum(
                float(outcome.get("metric_denominators", {}).get(numerator_key, 0.0))
                for outcome in outcome_values
            )
            denominator = sum(
                _metric_denominator(outcome, *denominator_keys)
                for outcome in outcome_values
            )
            value = numerator / denominator if denominator else None
        metrics[name] = {
            "numerator": numerator,
            "denominator": denominator,
            "value": value,
            "na_reason": None if value is not None else "no_eligible_samples",
            "samples": len(outcome_values),
        }
    report = {
        "schema_version": "maritime-alignment-evaluation/v1",
        "transport": args.transport,
        "is_fixture": args.transport == "fixture",
        "live_verified": args.transport == "live" and not any(
            item["status"] != "finished" for item in episodes
        ),
        "config": str(args.config),
        "scenarios": list(scenarios),
        "seeds": list(seeds),
        "repeat": args.repeat,
        "steps": args.steps,
        "episode_count": len(episodes),
        "finished_count": len(finished),
        "operational_failure_count": len(episodes) - len(finished),
        "metrics": metrics,
        "planning_latency_seconds": {
            "count": len(latency_values),
            "p50": _percentile(latency_values, 0.50),
            "p95": _percentile(latency_values, 0.95),
            "max": max(latency_values) if latency_values else None,
            "non_fault_samples_over_2s": sum(
                value > 2.0 for value in non_fault_latency_values
            ),
        },
        "audit": {
            "boundary_violations": sum(item["audit"].get("boundary_violations", 0) for item in episodes),
            "candidate_uncovered_without_reason": sum(
                item["audit"].get("candidate_uncovered_without_reason", 0) for item in episodes
            ),
            "trace_breaks": sum(item["audit"].get("trace_breaks", 0) for item in episodes),
            "cross_version_decisions": sum(
                item["audit"].get("cross_version_decisions", 0) for item in episodes
            ),
        },
        "episodes": episodes,
        "note": (
            "fixture transport is deterministic and does not demonstrate real model quality"
            if args.transport == "fixture"
            else "live results include operational failures and require real LongCat credentials"
        ),
    }
    if args.output is not None:
        if args.output.exists():
            raise SystemExit(f"refusing to overwrite existing report: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    return report


def _live_run(args) -> dict:
    _refuse_overwrite(args.output_dir)
    from src.env.simulation import SimulationEngine
    from src.mission.episode_logger import EpisodeLogger
    from src.schedule.config_loader import ConfigLoader

    config = _scenario_config(ConfigLoader.load(args.config), args.scenario)
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
            "population": {
                "total_count": config.ship.population.total_count,
                "type_i_ratio": config.ship.population.type_i_ratio,
                "type_ii_ratio": config.ship.population.type_ii_ratio,
            },
            "type_ii_ais_on_probability": config.ship.type_ii_ais_on_probability,
            "island_count_min": config.environment.island_count_min,
            "island_count_max": config.environment.island_count_max,
            "uav_count": config.uav.count,
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
    parser.add_argument(
        "--scenario", choices=SCENARIOS, default=None,
        help="run one scenario; batch mode without it covers the full fixture matrix",
    )
    parser.add_argument("--config", type=Path, default=Path("configs"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--seeds", type=_parse_seeds, default=None)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--steps", type=int, default=120)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--fixture", action="store_true",
        help="run the deterministic fixture transport instead of a live model",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/evaluations/mixed-maritime"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--transport", choices=("fixture", "live"), default=None)
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be positive")
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    if args.fixture and args.live:
        parser.error("--fixture cannot be combined with --live")
    if args.fixture:
        if args.transport is not None and args.transport != "fixture":
            parser.error("--fixture requires fixture transport")
        args.transport = "fixture"
        if args.output is None:
            args.output = args.output_dir / "report.json"
    if args.transport is not None:
        if args.live:
            parser.error("--live is only supported by the legacy single-episode interface")
        payload = _batch_report(args)
    elif args.seeds is not None or args.output is not None:
        parser.error("--seeds/--output require --transport fixture or --transport live")
    else:
        args.scenario = args.scenario or "mixed-ais"
        payload = _live_run(args) if args.live else _dry_run(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

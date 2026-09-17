"""Real-engine scenarios and provenance-preserving replay artifacts."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_mixed_maritime import (  # noqa: E402
    _FixtureGateway,
    _scenario_config,
    _scenario_intents,
)
from src.schedule.config_loader import ConfigLoader  # noqa: E402
from src.vis.backend.frame_builder import build_frame  # noqa: E402
from src.vis.backend.frame_logger import FrameLogger  # noqa: E402


SCENARIOS = (
    "V01", "V02", "V03", "V04", "V05", "V06", "V07", "V08", "V09",
    "ais-toggle", "passive-gates", "information-loop", "contact-assessment",
    "intent-lifecycle", "vessel-red-lifecycle", "weather-replan", "base-recovery",
    "reviewer-episode", "memory-lifecycle", "provider-contract",
)
FEATURE_INTEGRATION_SCENARIOS = (
    "ais-toggle", "passive-gates", "information-loop", "contact-assessment",
    "intent-lifecycle", "vessel-red-lifecycle", "weather-replan", "base-recovery",
    "reviewer-episode", "memory-lifecycle", "provider-contract",
)
_BASE_SCENARIO = {
    "V01": "mixed-ais",
    "V02": "mixed-ais",
    "V03": "mixed-ais",
    "V04": "disguised-type-ii",
    "V05": "mixed-ais",
    "V06": "mixed-ais",
    "V07": "disguised-type-ii",
    "V08": "mixed-ais",
    "V09": "model-failure",
    "ais-toggle": "mixed-ais",
    "passive-gates": "mixed-ais",
    "information-loop": "intent-overlap",
    "contact-assessment": "disguised-type-ii",
    "intent-lifecycle": "intent-overlap",
    "vessel-red-lifecycle": "mixed-ais",
    "weather-replan": "mixed-ais",
    "base-recovery": "mixed-ais",
    "reviewer-episode": "mixed-ais",
    "memory-lifecycle": "mixed-ais",
    "provider-contract": "mixed-ais",
}


def _stable_hash(value) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    return commit or None


def _wall_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scenario_config_for(config, name: str):
    base_name = _BASE_SCENARIO[name]
    configured = _scenario_config(config, base_name)
    if name in {"V03", "weather-replan"}:
        configured = replace(
            configured,
            environment=replace(
                configured.environment,
                island_count_min=0,
                island_count_max=0,
            ),
        )
    return configured


def _scenario_overrides(base, configured) -> dict:
    left = asdict(base)
    right = asdict(configured)

    def changed(a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            return {
                key: changed(a[key], b[key])
                for key in sorted(set(a) | set(b))
                if a.get(key) != b.get(key)
            }
        return b

    return changed(left, right)


def build_scenario(name: str, *, seed: int, transport: str):
    """Construct a real SimulationEngine with an explicit model transport."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown replay restoration scenario: {name}")
    if transport not in {"fixture", "live"}:
        raise ValueError("transport must be fixture or live")
    from src.env.simulation import SimulationEngine

    config = _scenario_config_for(ConfigLoader.load(str(PROJECT_ROOT / "configs")), name)
    gateway = (
        _FixtureGateway(
            contact_assessor_class="type_ii",
            allow_handoff_preemption=True,
        )
        if transport == "fixture" and name == "V07"
        else _FixtureGateway()
        if transport == "fixture"
        else None
    )
    engine = SimulationEngine(
        config,
        seed=int(seed),
        llm_gateway=gateway,
        episode_id=f"replay-{name.lower()}-{int(seed)}",
    )
    if name in {"information-loop", "intent-lifecycle"}:
        _scenario_intents(engine, "intent-overlap")
    return engine


def capture_frame(engine, *, total_steps: int) -> dict:
    """Capture through the production frame builder without advancing state."""
    result = getattr(engine, "last_result", {}) or {}
    return build_frame(
        engine.allocator.sm,
        cycle=int(getattr(engine.allocator.sm, "cycle", 0)),
        config=engine.config,
        total_steps=int(total_steps),
        llm_cycle=result.get("llm_cycle"),
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )


def _advance_v07_fixture_controls(engine, step_index: int, state: dict) -> None:
    """Drive the V07 fixture through observed control milestones.

    The hook only changes a real engine entity after the corresponding public
    event exists.  Frame capture and all subsequent sensor/control actions
    still run through the production engine path.
    """
    sm = engine.allocator.sm
    if step_index == 0 and "source_uav_id" not in state:
        probe = next(
            (
                item for item in sm.get_probe_sessions()
                if engine.control_coordinator.active_task(item.uav_id) is not None
            ),
            None,
        )
        if probe is None:
            return
        task = engine.control_coordinator.active_task(probe.uav_id)
        target = sm.contact_position(probe.contact_id, engine.clock.time)
        if task is None or target is None:
            return
        uav = next(item for item in engine.uavs if item.id == probe.uav_id)
        uav._col, uav._row = target[0] - 1.0, target[1]
        uav.heading_rad = 0.0
        sm.update_uav_status(
            uav.id,
            uav.status,
            uav.position,
            assigned_region_id=task.task_id,
        )
        state.update({
            "source_uav_id": uav.id,
            "contact_id": probe.contact_id,
            "probe_id": probe.probe_id,
            "source_position": [uav.float_position[0], uav.float_position[1]],
        })
        sm.add_event("validation_fixture_prepared", {
            "scenario": "V07",
            "uav_id": uav.id,
            "contact_id": probe.contact_id,
            "probe_id": probe.probe_id,
            "reason": "place_assigned_probe_at_observed_contact_standoff",
        })

    source_uav_id = state.get("source_uav_id")
    contact_id = state.get("contact_id")
    if not source_uav_id or not contact_id:
        return
    events = sm.get_recent_events(0.0)
    source_task = engine.control_coordinator.active_task(source_uav_id)
    if (
        "fuel_set" not in state
        and source_task is not None
        and source_task.task_type.value == "track"
        and any(
            event["type"] == "assessment_applied"
            and event["data"].get("contact_id") == contact_id
            for event in events
        )
    ):
        source = next(item for item in engine.uavs if item.id == source_uav_id)
        # 20% is below the proactive warning gate and still leaves a legal
        # fixed-wing route to Base-2 from the scripted V07 observation point.
        source.fuel_remaining_pct = 0.2
        source.fuel_warning_sent = False
        state["fuel_set"] = True
        sm.add_event("validation_fixture_fuel_set", {
            "scenario": "V07",
            "uav_id": source_uav_id,
            "fuel_pct": source.fuel_remaining_pct,
            "reason": "trigger_controlled_return_after_type_ii_track_started",
        })

    if "successor_uav_id" in state:
        return
    assignment = next(
        (
            event for event in events
            if event["type"] == "handoff_assignment_committed"
            and event["data"].get("contact_id") == contact_id
        ),
        None,
    )
    if assignment is None:
        return
    successor_id = assignment["data"]["successor_uav_id"]
    target = sm.contact_position(contact_id, engine.clock.time)
    if target is None:
        return
    successor = next(item for item in engine.uavs if item.id == successor_id)
    successor._col, successor._row = target[0] - 1.0, target[1]
    successor.heading_rad = 0.0
    successor_state = sm.get_uav(successor.id)
    sm.update_uav_status(
        successor.id,
        successor.status,
        successor.position,
        assigned_region_id=(
            successor_state.assigned_region_id if successor_state is not None else None
        ),
        fuel_remaining_pct=successor.fuel_remaining_pct,
        target_group_id=contact_id,
        heading_deg=successor.heading_deg,
        sensor_mode=successor.sensor_mode,
    )
    state["successor_uav_id"] = successor_id
    state["successor_position"] = [
        successor.float_position[0], successor.float_position[1],
    ]
    sm.add_event("validation_fixture_successor_prepared", {
        "scenario": "V07",
        "uav_id": successor_id,
        "contact_id": contact_id,
        "handoff_id": assignment["data"].get("handoff_id"),
        "reason": "place_assigned_successor_at_observed_contact_standoff",
    })


def _finite_pair(value) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in value[:2])
    )


def _audit_frame(frame: dict, previous: dict | None) -> list[dict]:
    issues = []

    def issue(code, **details):
        issues.append({"code": code, **details})

    for collection, position_key, identity_key in (
        ("uavs", "position", "id"),
        ("ships", "position", "id"),
        ("scenario_vessels", "position", "scenario_entity_id"),
        ("contacts", "estimated_position", "contact_id"),
    ):
        for item in frame.get(collection, ()):
            if not _finite_pair(item.get(position_key)):
                issue(
                    "non_finite_position",
                    frame_id=frame.get("frame_id"),
                    entity=f"{collection}:{item.get(identity_key)}",
                    expected="finite x/y position",
                    actual=item.get(position_key),
                )

    uav_ids = [item.get("id") for item in frame.get("uavs", ())]
    if len(uav_ids) != len(set(uav_ids)):
        issue("duplicate_uav_id", frame_id=frame.get("frame_id"), actual=uav_ids)

    probe_ids = [
        item.get("active_probe_id")
        for item in frame.get("contacts", ())
        if item.get("active_probe_id")
    ]
    if len(probe_ids) != len(set(probe_ids)):
        issue("duplicate_active_probe", frame_id=frame.get("frame_id"), actual=probe_ids)

    for uav in frame.get("uavs", ()):
        visual = uav.get("task_visual") or {}
        if visual.get("route_source") == "controller":
            if visual.get("generation") != uav.get("controller_generation"):
                issue(
                    "stale_route_generation",
                    frame_id=frame.get("frame_id"),
                    uav_id=uav.get("id"),
                    expected=uav.get("controller_generation"),
                    actual=visual.get("generation"),
                )
            route = uav.get("planned_path") or []
            if visual.get("route_status") == "ready" and route:
                current = uav.get("position")
                if not _finite_pair(route[0]) or math.dist(current[:2], route[0][:2]) > 1e-5:
                    issue(
                        "route_first_point_disconnected",
                        frame_id=frame.get("frame_id"),
                        uav_id=uav.get("id"),
                        expected=current,
                        actual=route[0],
                    )

        for key in ("sar_footprint", "sar_beam", "eo_fov"):
            value = uav.get(key)
            points = value if key == "sar_footprint" else (value or {}).get("polygon", [])
            for point in points:
                if not _finite_pair(point):
                    issue(
                        "non_finite_sensor_geometry",
                        frame_id=frame.get("frame_id"),
                        uav_id=uav.get("id"),
                        expected="finite sensor geometry",
                        actual=point,
                    )

    for key in ("coverage_pct", "searchable_cells", "scanned_searchable_cells", "information_version"):
        value = frame.get(key)
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            issue("non_finite_metric", frame_id=frame.get("frame_id"), key=key, actual=value)

    if previous is not None and frame.get("sim_time_min", 0) <= previous.get("sim_time_min", -1):
        issue(
            "simulation_time_not_monotonic",
            frame_id=frame.get("frame_id"),
            expected=f"> {previous.get('sim_time_min')}",
            actual=frame.get("sim_time_min"),
        )
    return issues


_EVENT_ALIASES = {
    "mission_assignment_approved": "mission_assignment_committed",
    "handoff_assignment_committed": "mission_assignment_committed",
    "mission_assignment_rejected": "task_failed",
    "mission_selection_failed": "task_failed",
    "decision_failed": "task_failed",
    "route_plan_failed": "task_failed",
    "conflict_replan_failed": "task_failed",
    "mission_task_released": "task_completed",
    "lifecycle_completed": "task_completed",
}


def _event_key(event: dict) -> str:
    event_type = _EVENT_ALIASES.get(event.get("type"), event.get("type"))
    time_value = event.get("time")
    if isinstance(time_value, (int, float)) and math.isfinite(float(time_value)):
        time_value = float(time_value)
    payload = json.dumps(
        event.get("data") or {}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False, default=str,
    )
    return f"{event_type}|{time_value!r}|{payload}"


def _phase_metrics(frames: Iterable[dict]) -> dict:
    phases = {}
    for frame in frames:
        for uav in frame.get("uavs", ()):
            visual = uav.get("task_visual") or {}
            phase = visual.get("phase")
            if not phase or phase in phases:
                continue
            phases[phase] = {
                "first_time": frame.get("sim_time_min"),
                "uav_id": uav.get("id"),
                "task_id": visual.get("task_id"),
                "source": "task_visual",
            }
    return phases


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _write_manifest(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    _write_json(temporary, payload)
    temporary.replace(path)


def _base_manifest(name: str, seed: int, steps: int, transport: str, engine) -> dict:
    base_config = ConfigLoader.load(str(PROJECT_ROOT / "configs"))
    return {
        "schema_version": "replay-restoration-run/v1",
        "status": "running",
        "git_commit": _git_commit(),
        "config_hash": _stable_hash(asdict(engine.config)),
        "seed": int(seed),
        "scenario": name,
        "requested_steps": int(steps),
        "completed_steps": 0,
        "sim_time": 0.0,
        "transport": transport,
        "fixture": transport == "fixture",
        "role_bindings": _role_bindings(engine),
        "config_overrides": _scenario_overrides(base_config, engine.config),
        "input_hashes": {
            "config": _stable_hash(asdict(engine.config)),
            "scenario": _stable_hash({"name": name, "seed": int(seed)}),
        },
        "memory_version": getattr(engine.allocator, "memory_version", "baseline"),
        "test_version": "replay-restoration-runner/v1",
        "episode_id": engine.episode_id,
        "started_at_wall_utc": _wall_time(),
    }


def _role_bindings(engine) -> dict:
    gateway = engine.allocator.llm_client.gateway
    result = {}
    for role in ("decision_maker", "contact_assessor", "red_commander", "reviewer"):
        binding = gateway.resolve_binding(role) if hasattr(gateway, "resolve_binding") else {}
        result[role] = {
            "model": binding.get("model", "unknown"),
            "provider": binding.get("provider", "unknown"),
            "temperature": binding.get("temperature"),
            "max_tokens": binding.get("max_tokens"),
            "thinking": binding.get("thinking"),
        }
    return result


def run_scenario(name: str, *, seed: int, steps: int, output_dir, transport: str) -> dict:
    """Run one engine scenario and write auditable frame/event artifacts."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown replay restoration scenario: {name}")
    if isinstance(steps, bool) or int(steps) < 1:
        raise ValueError("steps must be positive")
    if transport not in {"fixture", "live"}:
        raise ValueError("transport must be fixture or live")
    output_dir = Path(output_dir)
    if (output_dir / "manifest.json").exists():
        raise SystemExit(f"refusing to overwrite existing run: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)

    engine = build_scenario(name, seed=int(seed), transport=transport)
    manifest = _base_manifest(name, int(seed), int(steps), transport, engine)
    manifest_path = output_dir / "manifest.json"
    _write_manifest(manifest_path, manifest)
    frame_logger = FrameLogger(output_dir=str(output_dir), filename="frames.jsonl")
    events_path = output_dir / "events.jsonl"
    audit_path = output_dir / "audit.jsonl"
    event_keys: set[str] = set()
    all_frames: list[dict] = []
    all_issues: list[dict] = []
    status = "finished"
    blocked_reason = None
    previous = None
    fixture_state = {}
    try:
        for _ in range(int(steps)):
            step_index = len(all_frames)
            before = float(engine.clock.time)
            engine.step()
            after = float(engine.clock.time)
            if engine.runtime_status != "running" or after <= before:
                status = "blocked"
                blocked_reason = (
                    f"runtime_status={engine.runtime_status}, sim_time did not advance "
                    f"from {before}"
                )
                break
            if name == "V07" and transport == "fixture":
                _advance_v07_fixture_controls(engine, step_index, fixture_state)
            frame = capture_frame(engine, total_steps=int(steps))
            issues = _audit_frame(frame, previous)
            all_issues.extend(issues)
            for issue in issues:
                audit_path.open("a", encoding="utf-8").write(
                    json.dumps(issue, ensure_ascii=False, allow_nan=False) + "\n"
                )
            frame_logger.write(frame)
            all_frames.append(frame)
            previous = frame
            for event in frame.get("events", ()):
                key = _event_key(event)
                if key in event_keys:
                    continue
                event_keys.add(key)
                with events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"episode_id": engine.episode_id, "key": key, **event}, ensure_ascii=False, allow_nan=False) + "\n")
            if issues:
                status = "failed"
                blocked_reason = "frame audit failed"
                break
    except Exception as exc:
        status = "failed"
        blocked_reason = f"{type(exc).__name__}: {exc}"

    if not audit_path.exists():
        audit_path.write_text("", encoding="utf-8")
    if not events_path.exists():
        events_path.write_text("", encoding="utf-8")
    metrics = {
        "schema_version": "replay-restoration-metrics/v1",
        "scenario": name,
        "seed": int(seed),
        "phases": _phase_metrics(all_frames),
        "event_count": len(event_keys),
        "evidence_ids": sorted({
            sample.get("sample_id")
            for frame in all_frames
            for contact in frame.get("contacts", ())
            for sample in contact.get("samples", ())
            if sample.get("sample_id")
        }),
        "role_bindings": manifest["role_bindings"],
        "audit_issue_count": len(all_issues),
        "not_observed": {},
    }
    for expected in ("coverage_transit", "coverage_scan", "probe_approach", "probe_baseline", "probe_near", "track_active", "return", "holding"):
        if expected not in metrics["phases"]:
            metrics["not_observed"][expected] = "not observed within requested steps"
    _write_json(output_dir / "metrics.json", metrics)

    manifest.update({
        "status": status,
        "completed_steps": len(all_frames),
        "sim_time": float(engine.clock.time),
        "finished_at_wall_utc": _wall_time(),
        "frame_count": frame_logger.count,
        "event_count": len(event_keys),
        "audit_issue_count": len(all_issues),
        "blocked_reason": blocked_reason,
        "scenario_setup": fixture_state,
        "artifacts": {
            "frames": "frames.jsonl",
            "events": "events.jsonl",
            "metrics": "metrics.json",
            "audit": "audit.jsonl",
        },
    })
    _write_manifest(manifest_path, manifest)
    return {
        "status": status,
        "scenario": name,
        "seed": int(seed),
        "requested_steps": int(steps),
        "completed_steps": len(all_frames),
        "sim_time": float(engine.clock.time),
        "manifest": str(manifest_path),
        "output_dir": str(output_dir),
        "blocked_reason": blocked_reason,
    }


def check_log(path) -> dict:
    """Read-only audit for an existing frames JSONL file."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    frames = []
    issues = []
    previous = None
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            frame = json.loads(line)
            frame_issues = _audit_frame(frame, previous)
            issues.extend({"line": line_number, **issue} for issue in frame_issues)
            previous = frame
            frames.append(frame)
    return {
        "status": "passed" if not issues else "failed",
        "frames": len(frames),
        "issues": issues,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


__all__ = [
    "SCENARIOS", "FEATURE_INTEGRATION_SCENARIOS", "build_scenario",
    "capture_frame", "run_scenario", "check_log",
]

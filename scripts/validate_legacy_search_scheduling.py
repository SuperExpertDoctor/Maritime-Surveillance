"""Validate legacy search scheduling with replayable, independently audited frames."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.persistent_coverage_scenarios import (  # noqa: E402
    build_coverage_scenario,
    coverage_fixture_source_hash,
)
from scripts.replay_restoration_scenarios import capture_frame  # noqa: E402
from src.vis.backend.frame_logger import FrameLogger  # noqa: E402


SCHEMA_VERSION = "legacy-search-scheduling/v1"
ARTIFACTS = ("manifest.json", "frames.jsonl", "events.jsonl", "metrics.json", "audit.json")
NAVIGATION_EVENT_TYPES = frozenset({
    "route_blocked",
    "route_plan_failed",
    "conflict_replan_failed",
    "no_safe_recovery_path",
    "emergency_failure",
})
SCHEDULING_EVENT_TYPES = frozenset({
    "mission_selection_failed",
    "decision_failed",
    "mission_model_failure",
    "mission_assignment_rejected",
    "mission_state_invariant_failed",
    "pending_search_reassigned",
    "overlapping_active_search",
})
PENDING_RELEASE_REASONS = frozenset({
    "preempted",
    "uav_return",
    "fuel_return",
    "range_reserve",
    "holding",
    "coverage_incomplete",
    "emergency_failure",
    "route_plan_failed",
})


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
    return result.stdout.strip()


def _dirty_diff_hash() -> str:
    patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", "."],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=False,
        check=False,
    ).stdout
    return hashlib.sha256(patch).hexdigest()


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


def _read_jsonl(path: Path) -> list[dict]:
    frames = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {line_number} is not an object")
        frames.append(value)
    return frames


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


def _rect_payload(bbox) -> list[int] | None:
    if bbox is None:
        return None
    try:
        return [int(bbox.col_start), int(bbox.row_start), int(bbox.col_end), int(bbox.row_end)]
    except AttributeError:
        try:
            return [int(value) for value in bbox]
        except (TypeError, ValueError):
            return None


def _task_record_payload(record) -> dict:
    return {
        "task_id": record.task_id,
        "kind": record.kind,
        "status": record.status,
        "bbox": _rect_payload(record.bbox),
        "contact_id": record.contact_id,
        "intent_ids": list(record.intent_ids),
        "assigned_uav_id": record.assigned_uav_id,
        "approved_call_id": record.approved_call_id,
        "created_at_min": record.created_at_min,
        "started_at_min": record.started_at_min,
        "finished_at_min": record.finished_at_min,
        "release_reason": record.release_reason,
    }


def _frame_with_task_records(engine, steps: int) -> dict:
    frame = capture_frame(engine, total_steps=steps)
    frame["task_records"] = [
        _task_record_payload(record)
        for record in sorted(
            engine._mission_task_records.values(),
            key=lambda item: item.task_id,
        )
    ]
    return frame


def _event_key(event: dict) -> tuple:
    if event.get("event_id") is not None:
        return ("event_id", event.get("event_id"))
    return (
        "payload",
        event.get("type"),
        event.get("time"),
        _stable_json(event.get("data") or {}),
    )


def _event_classes(events: Iterable[dict]) -> dict[str, list[dict]]:
    classes = {"navigation": [], "scheduling": []}
    seen = {"navigation": set(), "scheduling": set()}
    for event in events:
        event_type = str(event.get("type") or "")
        key = _event_key(event)
        if event_type in NAVIGATION_EVENT_TYPES:
            category = "navigation"
        elif (
            event_type in SCHEDULING_EVENT_TYPES
            and event_type != "pending_search_reassigned"
        ) or any(
            marker in event_type
            for marker in ("must_service", "overlapping_active_search")
        ):
            category = "scheduling"
        else:
            continue
        if key in seen[category]:
            continue
        seen[category].add(key)
        classes[category].append({
            "type": event_type,
            "time": event.get("time"),
            "data": event.get("data") or {},
        })
    return classes


def _bbox(value) -> tuple[int, int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return None
    if result[0] >= result[2] or result[1] >= result[3]:
        return None
    return result


def _overlap(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bool:
    return not (
        left[2] <= right[0]
        or right[2] <= left[0]
        or left[3] <= right[1]
        or right[3] <= left[1]
    )


def _ordinary_regions(frame: dict) -> tuple[list[dict], list[str]]:
    regions = []
    issues = []
    seen_ids: set[str] = set()
    for region in frame.get("search_regions", ()):
        if not isinstance(region, dict) or region.get("type") != "search":
            continue
        region_id = region.get("id")
        if not isinstance(region_id, str) or not region_id:
            issues.append("search_region_missing_id")
            continue
        if region_id in seen_ids:
            issues.append(f"duplicate_search_region:{region_id}")
        seen_ids.add(region_id)
        if region.get("status") == "active":
            regions.append(region)
    return regions, issues


def _record_map(frame: dict) -> dict[str, dict]:
    values = frame.get("task_records")
    if not isinstance(values, list):
        values = frame.get("mission_task_records")
    if not isinstance(values, list):
        return {}
    return {
        item.get("task_id"): item
        for item in values
        if isinstance(item, dict) and isinstance(item.get("task_id"), str)
    }


def check_log(path: str | Path) -> dict:
    """Audit replay frames without consulting the live engine state."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    output_dir = path.parent
    manifest_path = output_dir / "manifest.json"
    manifest = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frames = _read_jsonl(path)
    issues: list[str] = []
    all_events: list[dict] = []
    event_keys: set[tuple] = set()
    pending: dict[str, float] = {}
    previous_time = None
    previous_frame_id = None

    for frame in frames:
        now = frame.get("sim_time_min")
        try:
            now = float(now)
        except (TypeError, ValueError):
            issues.append("invalid_frame_time")
            continue
        if previous_time is not None and now <= previous_time:
            issues.append(f"non_monotonic_frame_time:{now:g}")
        previous_time = now
        frame_id = frame.get("frame_id")
        if isinstance(frame_id, int) and previous_frame_id is not None and frame_id <= previous_frame_id:
            issues.append(f"non_monotonic_frame_id:{frame_id}")
        if isinstance(frame_id, int):
            previous_frame_id = frame_id

        runtime_status = frame.get("runtime_status", "running")
        if runtime_status == "paused_model":
            issues.append(f"runtime_paused_model:{now:g}")
        elif runtime_status == "paused_safety":
            issues.append(f"runtime_paused_safety:{now:g}")

        regions, region_issues = _ordinary_regions(frame)
        issues.extend(f"{issue}:{now:g}" for issue in region_issues)
        active_by_id = {region["id"]: region for region in regions}
        for index, left in enumerate(regions):
            left_box = _bbox(left.get("bbox"))
            if left_box is None:
                issues.append(f"invalid_search_bbox:{left.get('id')}:{now:g}")
                continue
            for right in regions[index + 1:]:
                right_box = _bbox(right.get("bbox"))
                if right_box is not None and _overlap(left_box, right_box):
                    issues.append(
                        f"overlapping_active_search:{left['id']}/{right['id']}:{now:g}"
                    )

        uavs = {
            item.get("id"): item
            for item in frame.get("uavs", ())
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        region_assignees: defaultdict[str, list[str]] = defaultdict(list)
        for region in regions:
            assignee = region.get("assigned_uav_id")
            if assignee is None:
                continue
            region_assignees[assignee].append(region["id"])
            uav = uavs.get(assignee)
            if uav is None:
                issues.append(f"region_uav_missing:{region['id']}:{assignee}:{now:g}")
            else:
                if uav.get("operational_status") == "failed" or uav.get("status") == "failed":
                    issues.append(f"region_uav_nonoperational:{region['id']}:{assignee}:{now:g}")
                if uav.get("assigned_region_id") != region["id"]:
                    issues.append(f"region_uav_mismatch:{region['id']}:{assignee}:{now:g}")
        for uav_id, region_ids in region_assignees.items():
            if len(region_ids) > 1:
                issues.append(f"uav_multiple_active_searches:{uav_id}:{now:g}")
        for uav_id, uav in uavs.items():
            assigned = uav.get("assigned_region_id")
            if assigned is not None:
                region = active_by_id.get(assigned)
                if region is None or region.get("assigned_uav_id") != uav_id:
                    issues.append(f"uav_region_mismatch:{uav_id}:{assigned}:{now:g}")

        records = _record_map(frame)
        for task_id, record in records.items():
            if record.get("kind") != "search":
                continue
            status = record.get("status")
            assignee = record.get("assigned_uav_id")
            region = active_by_id.get(task_id)
            if status == "executing":
                if assignee is None:
                    issues.append(f"executing_search_without_uav:{task_id}:{now:g}")
                elif region is None or region.get("assigned_uav_id") != assignee:
                    issues.append(f"record_region_mismatch:{task_id}:{now:g}")
                elif assignee not in uavs or uavs[assignee].get("operational_status") == "failed":
                    issues.append(f"executing_search_nonoperational_uav:{task_id}:{now:g}")
            elif status == "approved" and assignee is None:
                if region is None or region.get("assigned_uav_id") is not None or region.get("status") != "active":
                    issues.append(f"pending_projection_mismatch:{task_id}:{now:g}")
            elif status in {"completed", "cancelled", "blocked"} and assignee is not None:
                issues.append(f"terminal_search_has_uav:{task_id}:{now:g}")

        for task_id in pending:
            if task_id not in active_by_id:
                issues.append(f"pending_region_disappeared:{task_id}:{now:g}")

        for event in frame.get("events", ()):
            if not isinstance(event, dict):
                continue
            event_key = _event_key(event)
            if event_key not in event_keys:
                event_keys.add(event_key)
                all_events.append(event)
            event_type = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event_type == "mission_task_released" and (
                data.get("status") == "approved"
                or data.get("reason") in PENDING_RELEASE_REASONS
            ):
                task_id = data.get("task_id")
                if isinstance(task_id, str):
                    pending.setdefault(task_id, float(event.get("time", now)))
            elif event_type == "pending_search_reassigned":
                for assignment in data.get("assignments", ()):
                    if isinstance(assignment, dict) and isinstance(assignment.get("task_id"), str):
                        pending.pop(assignment["task_id"], None)

    if not frames:
        issues.append("no_frames")
    requested = manifest.get("requested_steps")
    completed = manifest.get("completed_steps")
    if isinstance(requested, int) and isinstance(completed, int) and requested != completed:
        issues.append(f"incomplete_requested_steps:{completed}/{requested}")
    if isinstance(requested, int) and len(frames) != requested:
        issues.append(f"frame_count_mismatch:{len(frames)}/{requested}")

    unique_issues = list(dict.fromkeys(issues))
    classes = _event_classes(all_events)
    scheduling_issue_prefixes = (
        "runtime_paused_",
        "overlapping_active_search:",
        "duplicate_search_region:",
        "invalid_search_bbox:",
        "region_uav_",
        "uav_region_",
        "uav_multiple_active_searches:",
        "executing_search_",
        "record_region_mismatch:",
        "pending_projection_mismatch:",
        "pending_region_disappeared:",
        "terminal_search_has_uav:",
        "incomplete_requested_steps:",
        "frame_count_mismatch:",
    )
    for issue in unique_issues:
        if issue.startswith(scheduling_issue_prefixes):
            classes["scheduling"].append({
                "type": "audit_violation",
                "time": None,
                "data": {"issue": issue},
            })
    return {
        "schema_version": f"{SCHEMA_VERSION}/audit",
        "status": "PASS" if not unique_issues else "FAIL",
        "frames_checked": len(frames),
        "issues": unique_issues,
        "failure_classes": classes,
        "navigation_failure_count": len(classes["navigation"]),
        "scheduling_failure_count": len(classes["scheduling"]),
    }


def _prepare_fixture_preemption(engine) -> dict:
    records = sorted(
        (
            record for record in engine._mission_task_records.values()
            if record.kind == "search"
            and record.status == "executing"
            and record.assigned_uav_id is not None
        ),
        key=lambda record: record.task_id,
    )
    if not records:
        raise RuntimeError("fixture could not find an executing ordinary search")
    record = records[0]
    uav = next(item for item in engine.uavs if item.id == record.assigned_uav_id)
    engine._begin_return(uav, engine.clock.time)
    engine.allocator.sm.add_event("legacy_fixture_preemption", {
        "task_id": record.task_id,
        "uav_id": uav.id,
        "reason": "deterministic_return_window",
    })
    pending = next(
        item for item in engine.allocator.sm.get_pending_search_regions()
        if item.id == record.task_id
    )
    return {
        "task_id": record.task_id,
        "source_uav_id": uav.id,
        "bbox": _rect_payload(pending.bbox),
        "trigger_time_min": float(engine.clock.time),
    }


def _fixture_metrics(frames: list[dict], events: list[dict], fixture: dict | None) -> dict:
    task_id = fixture.get("task_id") if fixture else None
    history = []
    if task_id:
        for frame in frames:
            region = next(
                (
                    item for item in frame.get("search_regions", ())
                    if item.get("id") == task_id and item.get("type") == "search"
                ),
                None,
            )
            if region is not None:
                history.append({
                    "time": frame.get("sim_time_min"),
                    "bbox": region.get("bbox"),
                    "status": region.get("status"),
                    "assigned_uav_id": region.get("assigned_uav_id"),
                })
    pending_frames = [
        item for item in history
        if item.get("status") == "active" and item.get("assigned_uav_id") is None
    ]
    reassignments = []
    for event in events:
        if event.get("type") != "pending_search_reassigned":
            continue
        data = event.get("data") or {}
        for assignment in data.get("assignments", ()):
            if isinstance(assignment, dict):
                reassignments.append({
                    "time": event.get("time"),
                    "task_id": assignment.get("task_id"),
                    "uav_id": assignment.get("uav_id"),
                })
    bbox_values = {tuple(item["bbox"]) for item in history if item.get("bbox") is not None}
    coverage = {}
    for frame in frames:
        if float(frame.get("sim_time_min", -1)) in {480.0, 720.0}:
            metrics = frame.get("coverage_metrics") or {}
            coverage[str(int(float(frame["sim_time_min"])))] = metrics.get("cumulative_pct")
    return {
        "fixture_task_id": task_id,
        "fixture_source_uav_id": fixture.get("source_uav_id") if fixture else None,
        "fixture_trigger_time_min": fixture.get("trigger_time_min") if fixture else None,
        "fixture_bbox": fixture.get("bbox") if fixture else None,
        "fixture_region_bbox_count": len(bbox_values),
        "fixture_region_frame_count": len(history),
        "pending_interval_count": 1 if pending_frames else 0,
        "pending_frame_times": [item.get("time") for item in pending_frames],
        "reassignment_count": len(reassignments),
        "reassignments": reassignments,
        "coverage_cumulative_pct": coverage,
    }


def _validate_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        existing = [name for name in ARTIFACTS if (output_dir / name).exists()]
        if existing:
            raise FileExistsError(
                f"refusing to overwrite existing validation artifacts: {', '.join(existing)}"
            )
    output_dir.mkdir(parents=True, exist_ok=True)


def run_validation(*, seed: int, steps: int, transport: str, output_dir: str | Path) -> dict:
    """Run the real engine and write one frame per completed simulation step."""
    if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
        raise ValueError("steps must be a positive integer")
    if transport not in {"fixture", "live"}:
        raise ValueError("transport must be fixture or live")
    output_dir = Path(output_dir).expanduser().resolve()
    _validate_output_dir(output_dir)
    frames_path = output_dir / "frames.jsonl"
    events_path = output_dir / "events.jsonl"
    manifest_path = output_dir / "manifest.json"
    frames_path.write_text("", encoding="utf-8")
    events_path.write_text("", encoding="utf-8")

    started_at = _wall_time()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "seed": int(seed),
        "transport": transport,
        "fixture": transport == "fixture",
        "fixture_quality_scope": (
            "deterministic model boundary; not real-LLM quality"
            if transport == "fixture" else "real project-configured LongCat/live provider"
        ),
        "fixture_source_sha256": coverage_fixture_source_hash() if transport == "fixture" else None,
        "git_commit": _git_output("rev-parse", "HEAD") or None,
        "dirty_diff_sha256": _dirty_diff_hash(),
        "requested_steps": int(steps),
        "completed_steps": 0,
        "actual_end_time_min": 0.0,
        "clock_dt_min": None,
        "episode_id": None,
        "config_sha256": None,
        "role_bindings": None,
        "platform": {"python": platform.python_version(), "system": platform.platform()},
        "started_at_wall_utc": started_at,
        "artifacts": {name: name for name in ARTIFACTS},
    }

    engine = None
    fixture = None
    completed_steps = 0
    runtime_exception = None
    all_events: dict[tuple, dict] = {}
    frame_logger = FrameLogger(output_dir=str(output_dir), filename="frames.jsonl")
    try:
        engine = build_coverage_scenario(
            "coverage-open-water", seed=int(seed), transport=transport,
        )
        manifest.update({
            "episode_id": engine.episode_id,
            "clock_dt_min": float(engine.clock.dt_min),
            "config_sha256": _stable_hash(engine.config),
            "role_bindings": _role_bindings(engine),
        })
        fixture = None
        for step_index in range(steps):
            before = float(engine.clock.time)
            engine.step()
            after = float(engine.clock.time)
            frame = _frame_with_task_records(engine, steps)
            frame_logger.write(frame)
            if after > before:
                completed_steps += 1
            for event in frame.get("events", ()):
                if isinstance(event, dict):
                    all_events.setdefault(_event_key(event), event)
            try:
                engine._validate_mission_state_invariants(strict=True)
            except ValueError as exc:
                runtime_exception = f"mission invariant: {exc}"
            if transport == "fixture" and step_index == 0:
                fixture = _prepare_fixture_preemption(engine)
            if after <= before or engine.runtime_status != "running":
                break
    except Exception as exc:  # The manifest makes blocked setup visible to CI.
        runtime_exception = f"{type(exc).__name__}: {exc}"

    frames = _read_jsonl(frames_path) if frames_path.is_file() else []
    events = sorted(
        all_events.values(),
        key=lambda event: (float(event.get("time", 0.0)), str(event.get("type", "")), _stable_json(event.get("data") or {})),
    )
    for event in events:
        _append_jsonl(events_path, event)
    classes = _event_classes(events)
    metrics = _fixture_metrics(frames, events, fixture)
    metrics.update({
        "schema_version": f"{SCHEMA_VERSION}/metrics",
        "requested_steps": steps,
        "completed_steps": completed_steps,
        "runtime_status": engine.runtime_status if engine is not None else None,
        "final_time_min": float(engine.clock.time) if engine is not None else None,
        "navigation_failure_count": len(classes["navigation"]),
        "scheduling_failure_count": len(classes["scheduling"]),
        "navigation_failures": classes["navigation"],
        "scheduling_failures": classes["scheduling"],
        "model_failure_count": sum(
            item["type"] in {"mission_model_failure", "mission_selection_failed", "decision_failed"}
            for item in classes["scheduling"]
        ),
        "runtime_exception": runtime_exception,
    })
    _write_json(output_dir / "metrics.json", metrics)

    manifest.update({
        "status": "finished" if runtime_exception is None and completed_steps == steps else "blocked",
        "runtime_status": engine.runtime_status if engine is not None else None,
        "stopped_reason": runtime_exception,
        "completed_steps": completed_steps,
        "actual_end_time_min": float(engine.clock.time) if engine is not None else None,
        "frame_count": len(frames),
        "event_count": len(events),
        "fixture_trigger": fixture,
        "finished_at_wall_utc": _wall_time(),
    })
    _write_json(manifest_path, manifest)
    audit = check_log(frames_path)
    overall_status = "PASS" if (
        manifest["status"] == "finished"
        and audit["status"] == "PASS"
        and not classes["scheduling"]
        and (transport != "fixture" or metrics["reassignment_count"] >= 1)
    ) else (
        "BLOCKED" if manifest["status"] != "finished" or runtime_exception else "FAIL"
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": overall_status,
        "seed": int(seed),
        "transport": transport,
        "requested_steps": steps,
        "completed_steps": completed_steps,
        "output_dir": str(output_dir),
        "manifest": str(manifest_path),
        "metrics": metrics,
        "audit": audit,
    }
    _write_json(output_dir / "audit.json", {**audit, "overall_status": overall_status})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--steps", type=int, default=480)
    parser.add_argument("--transport", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--output-dir")
    parser.add_argument("--check-log")
    args = parser.parse_args(argv)
    if args.check_log:
        audit = check_log(args.check_log)
        print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if audit["status"] == "PASS" else 1
    if not args.output_dir:
        parser.error("--output-dir is required when --check-log is not used")
    result = run_validation(
        seed=args.seed,
        steps=args.steps,
        transport=args.transport,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

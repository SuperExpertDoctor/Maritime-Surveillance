"""Run and independently audit persistent SAR coverage evaluations."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
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
    coverage_domain_cells,
    coverage_execution_manifest,
    coverage_fixture_source_hash,
)
from scripts.replay_restoration_scenarios import capture_frame  # noqa: E402


WINDOWS = (30, 60, 120)
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2


def _wall_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_text(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _hash(value) -> str:
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    return result.stdout.strip()


def _dirty_hash() -> str:
    patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD", "--", "."],
        cwd=PROJECT_ROOT, capture_output=True, text=False, check=False,
    ).stdout
    return hashlib.sha256(patch).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True,
                   default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_json_text(payload) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    result = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"frame record at {path}:{line_number} is not an object")
            result.append(value)
    return result


def _config_value(config: dict, *keys, default=None):
    value = config
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _role_bindings(engine) -> dict:
    gateway = engine.allocator.llm_client.gateway
    result = {}
    for role in ("decision_maker", "contact_assessor", "red_commander", "reviewer"):
        binding = gateway.resolve_binding(role) if hasattr(gateway, "resolve_binding") else {}
        result[role] = {
            key: binding.get(key)
            for key in ("model", "provider", "temperature", "max_tokens", "thinking")
        }
    return result


def _fixed_cells(engine) -> list[list[int]]:
    return [list(cell) for cell in coverage_domain_cells(engine)]


def _execution_config(engine) -> dict:
    return coverage_execution_manifest(engine)


def _seed_manifest(engine, *, scenario: str, seed: int, steps: int, transport: str) -> dict:
    config = asdict(engine.config)
    return {
        "schema_version": "persistent-coverage-evaluation-seed/v1",
        "status": "running",
        "scenario": scenario,
        "seed": int(seed),
        "transport": transport,
        "fixture": transport == "fixture",
        "fixture_quality_scope": (
            "deterministic model boundary; not real-LLM quality"
            if transport == "fixture" else "real model transport"
        ),
        "fixture_source_sha256": coverage_fixture_source_hash() if transport == "fixture" else None,
        "git_commit": _git("rev-parse", "HEAD") or None,
        "dirty_diff_sha256": _dirty_hash(),
        "requested_steps": int(steps),
        "completed_steps": 0,
        "actual_end_time_min": float(engine.clock.time),
        "clock_dt_min": float(engine.clock.dt_min),
        "episode_id": engine.episode_id,
        "config": config,
        "config_sha256": _hash(config),
        "coverage_execution": _execution_config(engine),
        "fixed_searchable_cells": _fixed_cells(engine),
        "role_bindings": _role_bindings(engine),
        "platform": {"python": platform.python_version(), "system": platform.platform()},
        "started_at_wall_utc": _wall_time(),
        "artifacts": {
            "frames": "frames.jsonl",
            "events": "events.jsonl",
            "metrics": "metrics.csv",
            "outcome": "outcome.json",
        },
    }


def _event_key(episode_id: str, event: dict) -> tuple:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return (episode_id, event.get("type"), event.get("time"), data.get("uav_id"))


def _capture_seed(scenario: str, seed: int, steps: int, output_dir: Path, transport: str) -> dict:
    seed_dir = output_dir / f"seed-{seed}"
    if seed_dir.exists():
        raise FileExistsError(f"refusing to overwrite seed output: {seed_dir}")
    seed_dir.mkdir(parents=True)
    frames_path = seed_dir / "frames.jsonl"
    events_path = seed_dir / "events.jsonl"
    manifest_path = seed_dir / "manifest.json"
    try:
        engine = build_coverage_scenario(scenario, seed=seed, transport=transport)
    except Exception as exc:
        manifest = {
            "schema_version": "persistent-coverage-evaluation-seed/v1",
            "status": "blocked",
            "scenario": scenario,
            "seed": int(seed),
            "transport": transport,
            "requested_steps": int(steps),
            "completed_steps": 0,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "started_at_wall_utc": _wall_time(),
            "finished_at_wall_utc": _wall_time(),
        }
        _write_json(manifest_path, manifest)
        return {"seed": seed, "status": "BLOCKED", "completed_steps": 0, "error": str(exc)}

    manifest = _seed_manifest(engine, scenario=scenario, seed=seed, steps=steps, transport=transport)
    _write_json(manifest_path, manifest)
    event_payloads: dict[tuple, str] = {}
    frame_count = 0
    completed_steps = 0
    status = "completed"
    stopped_reason = None

    def record(frame: dict) -> None:
        nonlocal frame_count
        _append_jsonl(frames_path, frame)
        frame_count += 1
        for event in frame.get("events", ()):
            if event.get("type") != "sar_scan":
                continue
            key = _event_key(engine.episode_id, event)
            payload = _json_text(event)
            previous = event_payloads.get(key)
            if previous is not None and previous != payload:
                raise ValueError(f"conflicting SAR event payload for identity {key!r}")
            if previous is None:
                event_payloads[key] = payload
                _append_jsonl(events_path, {"episode_id": engine.episode_id, **event})

    try:
        record(capture_frame(engine, total_steps=steps))
        for _ in range(steps):
            before = float(engine.clock.time)
            engine.step()
            after = float(engine.clock.time)
            if after > before:
                completed_steps += 1
            record(capture_frame(engine, total_steps=steps))
            if engine.runtime_status != "running" or after <= before:
                status = "blocked"
                stopped_reason = (
                    f"runtime_status={engine.runtime_status}; clock {before} -> {after}"
                )
                break
    except KeyboardInterrupt:
        status = "blocked"
        stopped_reason = "capture interrupted"
    except Exception as exc:
        status = "blocked"
        stopped_reason = f"{type(exc).__name__}: {exc}"

    if completed_steps != steps and status == "completed":
        status = "blocked"
        stopped_reason = "requested step count was not reached"
    if not events_path.exists():
        events_path.write_text("", encoding="utf-8")
    final_frame = _read_jsonl(frames_path)[-1] if frame_count else {}
    manifest.update({
        "status": status,
        "runtime_status": engine.runtime_status,
        "stopped_reason": stopped_reason,
        "completed_steps": completed_steps,
        "actual_end_time_min": float(engine.clock.time),
        "frame_count": frame_count,
        "event_count": len(event_payloads),
        "finished_at_wall_utc": _wall_time(),
    })
    _write_json(manifest_path, manifest)

    audit = check_log(frames_path, output_dir=seed_dir, write_artifacts=True)
    outcome = {
        "schema_version": "persistent-coverage-outcome/v1",
        "scenario": scenario,
        "seed": int(seed),
        "transport": transport,
        "status": audit["status"],
        "completed_steps": completed_steps,
        "actual_end_time_min": float(engine.clock.time),
        "independent_check": audit,
        "engine_summary": engine.summary(),
        "final_frame_id": final_frame.get("frame_id"),
    }
    _write_json(seed_dir / "outcome.json", outcome)
    manifest["coverage_gate_status"] = audit["status"]
    _write_json(manifest_path, manifest)
    return {
        "seed": int(seed),
        "status": audit["status"],
        "completed_steps": completed_steps,
        "actual_end_time_min": float(engine.clock.time),
        "coverage_gate_status": audit["status"],
        "stopped_reason": stopped_reason,
        "manifest": str(manifest_path),
        "outcome": str(seed_dir / "outcome.json"),
    }


def _deduplicate_frames(frames: list[dict]) -> tuple[list[dict], list[str]]:
    by_time: dict[float, dict] = {}
    issues: list[str] = []
    for frame in frames:
        time_value = frame.get("sim_time_min")
        if isinstance(time_value, bool) or not isinstance(time_value, (int, float)):
            issues.append("invalid_frame_time")
            continue
        time_value = float(time_value)
        previous = by_time.get(time_value)
        if previous is None:
            by_time[time_value] = frame
            continue
        left = dict(previous)
        right = dict(frame)
        left.pop("runtime_status", None)
        right.pop("runtime_status", None)
        if _json_text(left) != _json_text(right):
            issues.append(f"conflicting_duplicate_frame:{time_value:g}")
    return [by_time[key] for key in sorted(by_time)], issues


def _collect_events(frames: list[dict], episode_id: str) -> tuple[list[dict], list[str]]:
    events: dict[tuple, tuple[str, dict]] = {}
    issues: list[str] = []
    for frame in frames:
        for event in frame.get("events", ()):
            if not isinstance(event, dict) or event.get("type") != "sar_scan":
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            key = _event_key(episode_id, event)
            payload = _json_text(event)
            previous = events.get(key)
            if previous is not None:
                if previous[0] != payload:
                    issues.append(f"conflicting_sar_event:{key!r}")
                continue
            events[key] = (payload, event)
    result = [event for _, event in events.values()]
    result.sort(key=lambda event: (float(event.get("time", 0.0)), str(event.get("data", {}).get("uav_id", ""))))
    return result, issues


def _metric_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _close(left, right, tolerance=1e-9) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)


def _geometry_audit(frames: list[dict], events: list[dict], manifest: dict) -> tuple[int, list[str]]:
    execution = manifest.get("coverage_execution") or {}
    config = manifest.get("config") or {}
    resolution = _config_value(config, "grid", "resolution", default=(30, 30))
    try:
        grid_shape = (int(resolution[0]), int(resolution[1]))
    except (TypeError, ValueError, IndexError):
        return 0, ["missing_grid_resolution"]
    frame_by_time = {float(frame.get("sim_time_min")): frame for frame in frames}
    if not events:
        return 0, ["no_sar_scan_events"]
    checked = 0
    issues: list[str] = []
    required = ("swath_width_cells", "near_range_cells", "along_track_cells")
    if any(_metric_number(execution.get(key)) is None for key in required):
        return 0, ["missing_coverage_execution_geometry"]
    for event in events:
        data = event.get("data") or {}
        event_time = _metric_number(event.get("time"))
        frame = frame_by_time.get(event_time)
        if frame is None:
            issues.append(f"missing_frame_for_sar_event:{event_time}")
            continue
        uav = next((item for item in frame.get("uavs", ()) if item.get("id") == data.get("uav_id")), None)
        if uav is None:
            issues.append(f"missing_uav_for_sar_event:{data.get('uav_id')}")
            continue
        position = uav.get("sar_scan_position")
        heading = uav.get("sar_actual_heading_rad")
        look = uav.get("sar_look_direction")
        if position is None or heading is None or look not in {"left", "right"}:
            return checked, [*issues, "native_sar_geometry_missing_from_frame"]
        if data.get("position") is not None and not all(
            _close(data["position"][index], position[index], 1e-9) for index in (0, 1)
        ):
            issues.append(f"sar_event_pose_mismatch:{event_time}")
            continue
        if data.get("heading_rad") is not None and not _close(data["heading_rad"], heading, 1e-9):
            issues.append(f"sar_event_heading_mismatch:{event_time}")
            continue
        if data.get("look_direction") is not None and data.get("look_direction") != look:
            issues.append(f"sar_event_look_mismatch:{event_time}")
            continue
        if not all(_close(data.get(key), execution.get(key), 1e-9) for key in required):
            issues.append(f"sar_event_geometry_config_mismatch:{event_time}")
            continue
        x, y = float(position[0]), float(position[1])
        heading = float(heading)
        side_x, side_y = (
            (-math.sin(heading), math.cos(heading))
            if look == "right"
            else (math.sin(heading), -math.cos(heading))
        )
        forward_x, forward_y = math.cos(heading), math.sin(heading)
        near = float(execution["near_range_cells"])
        far = near + float(execution["swath_width_cells"])
        along_limit = float(execution["along_track_cells"]) / 2.0
        padding = int(math.ceil(far + float(execution["along_track_cells"])))
        expected = []
        for col in range(max(0, int(math.floor(x)) - padding), min(grid_shape[0], int(math.floor(x)) + padding + 1)):
            for row in range(max(0, int(math.floor(y)) - padding), min(grid_shape[1], int(math.floor(y)) + padding + 1)):
                dx, dy = col + 0.5 - x, row + 0.5 - y
                cross = dx * side_x + dy * side_y
                along = dx * forward_x + dy * forward_y
                if near <= cross < far and abs(along) <= along_limit:
                    expected.append([col, row])
        expected.sort()
        actual = sorted(data.get("cells") or [])
        if expected != actual:
            issues.append(f"sar_event_footprint_mismatch:{event_time}")
            continue
        checked += 1
    return checked, issues


def _gate_status(manifest: dict, frames: list[dict], rows: list[dict], issues: list[str], geometry_checked: int) -> tuple[str, dict]:
    end_time = float(manifest.get("actual_end_time_min") or (frames[-1].get("sim_time_min", 0.0) if frames else 0.0))
    scenario = manifest.get("scenario")
    target = (
        {"cumulative_480": 80.0, "cumulative_720": 95.0, "time_mean": 10.0, "p5": 5.0}
        if scenario == "coverage-open-water"
        else {"cumulative_480": 65.0, "cumulative_720": 85.0, "time_mean": 7.5, "p5": 3.0}
    )
    frame_by_time = {float(row["sim_time_min"]): row for row in rows}
    cumulative_480 = frame_by_time.get(480.0, {}).get("cumulative_pct")
    cumulative_720 = frame_by_time.get(720.0, {}).get("cumulative_pct")
    window = {}
    for row in rows:
        if row.get("window_minutes") == 60 and row.get("time_mean_pct") is not None:
            window = row
    gate_values = {
        "cumulative_480_pct": cumulative_480,
        "cumulative_720_pct": cumulative_720,
        "c60_time_mean_pct": window.get("time_mean_pct"),
        "c60_p5_pct": window.get("p5_pct"),
        "c60_longest_zero_min": window.get("longest_zero_coverage_min"),
        "geometry_events_checked": geometry_checked,
        "actual_end_time_min": end_time,
    }
    if issues or geometry_checked == 0:
        return "FAIL" if issues and not any("missing" in issue or "no_sar" in issue for issue in issues) else "BLOCKED", gate_values
    if end_time < 720.0:
        return "BLOCKED", gate_values
    checks = (
        _metric_number(cumulative_480) is not None and cumulative_480 >= target["cumulative_480"],
        _metric_number(cumulative_720) is not None and cumulative_720 >= target["cumulative_720"],
        _metric_number(window.get("time_mean_pct")) is not None and window["time_mean_pct"] >= target["time_mean"],
        _metric_number(window.get("p5_pct")) is not None and window["p5_pct"] >= target["p5"],
        _metric_number(window.get("longest_zero_coverage_min")) is not None and window["longest_zero_coverage_min"] <= 0.0,
    )
    return ("PASS" if all(checks) else "FAIL"), gate_values


def check_log(path: Path, *, output_dir: Path | None = None, write_artifacts: bool = False) -> dict:
    """Independently recompute coverage from raw SAR events and frames."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    seed_dir = path.parent
    manifest_path = seed_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"missing manifest beside {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frames, frame_issues = _deduplicate_frames(_read_jsonl(path))
    if not frames:
        raise ValueError("log has no frames")
    episode_id = str(manifest.get("episode_id") or frames[0].get("episode_id") or "")
    events, event_issues = _collect_events(frames, episode_id)
    fixed_cells = {
        (int(item[0]), int(item[1]))
        for item in manifest.get("fixed_searchable_cells", ())
        if isinstance(item, (list, tuple)) and len(item) == 2
    }
    first_metrics = frames[0].get("coverage_metrics") or {}
    domain_count = len(fixed_cells) or int(first_metrics.get("fixed_searchable_cells") or 0)
    cell_size = _metric_number(_config_value(manifest.get("config") or {}, "grid", "cell_size_km"))
    if cell_size is None:
        cell_size = 0.0
    scan_times: dict[tuple[int, int], list[float]] = {}
    events_by_time: dict[float, list[dict]] = {}
    for event in events:
        event_time = _metric_number(event.get("time"))
        data = event.get("data") or {}
        if event_time is None:
            event_issues.append("invalid_sar_event_time")
            continue
        for raw_cell in data.get("cells", ()):
            if not isinstance(raw_cell, (list, tuple)) or len(raw_cell) != 2:
                event_issues.append(f"invalid_sar_cell:{event_time}")
                continue
            try:
                cell = (int(raw_cell[0]), int(raw_cell[1]))
            except (TypeError, ValueError):
                event_issues.append(f"invalid_sar_cell:{event_time}")
                continue
            if fixed_cells and cell not in fixed_cells:
                continue
            scan_times.setdefault(cell, []).append(event_time)
        events_by_time.setdefault(event_time, []).append(event)

    rows: list[dict] = []
    audit_issues = [*frame_issues, *event_issues]
    expected_dt = _metric_number(manifest.get("clock_dt_min"))
    if expected_dt is not None:
        for previous, current in zip(frames, frames[1:]):
            delta = float(current["sim_time_min"]) - float(previous["sim_time_min"])
            if delta > expected_dt + 1e-9:
                audit_issues.append(
                    f"missing_tick:{previous['sim_time_min']:g}->{current['sim_time_min']:g}"
                )
    for frame in frames:
        now = float(frame["sim_time_min"])
        for event in frame.get("events", ()):
            if event.get("type") == "sar_scan" and _metric_number(event.get("time")) is not None:
                if float(event["time"]) > now + 1e-9:
                    audit_issues.append(f"future_sar_event_in_frame:{now:g}")
        metrics = frame.get("coverage_metrics")
        if not isinstance(metrics, dict):
            audit_issues.append(f"missing_coverage_metrics:{now:g}")
            continue
        if not _close(metrics.get("as_of_min"), now):
            audit_issues.append(f"metric_time_mismatch:{now:g}")
        if int(metrics.get("fixed_searchable_cells", -1)) != domain_count:
            audit_issues.append(f"fixed_domain_mismatch:{now:g}")
        ever = {
            cell for cell, times in scan_times.items()
            if any(scanned_at <= now for scanned_at in times)
        }
        cumulative = None if domain_count == 0 else 100.0 * len(ever) / domain_count
        if not _close(metrics.get("cumulative_pct"), cumulative):
            audit_issues.append(f"cumulative_mismatch:{now:g}")
        expected_windows = {}
        for window_min in WINDOWS:
            covered = {
                cell for cell, times in scan_times.items()
                if any(
                    now - window_min < scanned_at <= now
                    for scanned_at in times
                )
            }
            expected = None if domain_count == 0 else 100.0 * len(covered) / domain_count
            expected_windows[window_min] = (len(covered), expected)
        for window in metrics.get("windows", ()):
            if not isinstance(window, dict) or window.get("minutes") not in expected_windows:
                continue
            minutes = int(window["minutes"])
            count, expected = expected_windows[minutes]
            if int(window.get("covered_cells", -1)) != count:
                audit_issues.append(f"window_count_mismatch:{now:g}:{minutes}")
            if not _close(window.get("coverage_pct"), expected):
                audit_issues.append(f"window_pct_mismatch:{now:g}:{minutes}")
            if not _close(window.get("covered_area_km2"), count * cell_size * cell_size):
                audit_issues.append(f"window_area_mismatch:{now:g}:{minutes}")
        primary = expected_windows.get(int(metrics.get("primary_window_min", 60)), (0, None))[1]
        overdue = None if cumulative is None or primary is None else cumulative - primary
        if not _close(metrics.get("unseen_pct"), None if cumulative is None else 100.0 - cumulative):
            audit_issues.append(f"unseen_pct_mismatch:{now:g}")
        if not _close(metrics.get("overdue_seen_pct"), overdue):
            audit_issues.append(f"overdue_pct_mismatch:{now:g}")
        window = next((item for item in metrics.get("windows", ()) if item.get("minutes") == 60), {})
        rows.append({
            "sim_time_min": now,
            "cumulative_pct": cumulative,
            "window_minutes": 60,
            "coverage_pct": window.get("coverage_pct"),
            "covered_cells": window.get("covered_cells"),
            "covered_area_km2": window.get("covered_area_km2"),
            "window_complete": window.get("window_complete"),
        })

    geometry_checked, geometry_issues = _geometry_audit(frames, events, manifest)
    audit_issues.extend(geometry_issues)
    for index, row in enumerate(rows):
        next_time = rows[index + 1]["sim_time_min"] if index + 1 < len(rows) else None
        row["time_mean_pct"] = None
        row["p5_pct"] = None
        row["longest_zero_coverage_min"] = 0.0
        if (
            next_time is not None
            and row["sim_time_min"] >= 240.0
            and next_time <= 720.0
            and (expected_dt is None or next_time - row["sim_time_min"] <= expected_dt + 1e-9)
        ):
            row["time_mean_pct"] = row["coverage_pct"]
    intervals = [
        (row["coverage_pct"], rows[index + 1]["sim_time_min"] - row["sim_time_min"])
        for index, row in enumerate(rows[:-1])
        if row["sim_time_min"] >= 240.0 and rows[index + 1]["sim_time_min"] <= 720.0
        and (expected_dt is None or rows[index + 1]["sim_time_min"] - row["sim_time_min"] <= expected_dt + 1e-9)
        and _metric_number(row["coverage_pct"]) is not None
    ]
    duration = sum(item[1] for item in intervals)
    time_mean = sum(value * span for value, span in intervals) / duration if duration else None
    zero_duration = sum(span for value, span in intervals if value == 0.0)
    ordered = sorted(intervals, key=lambda item: item[0])
    p5 = None
    accumulated = 0.0
    for value, span in ordered:
        accumulated += span
        if duration and accumulated >= duration * 0.05:
            p5 = value
            break
    for row in rows:
        row["time_mean_pct"] = time_mean
        row["p5_pct"] = p5
        row["longest_zero_coverage_min"] = zero_duration
    status, gates = _gate_status(manifest, frames, rows, audit_issues, geometry_checked)
    result = {
        "schema_version": "persistent-coverage-independent-check/v1",
        "status": status,
        "measurement_mode": "native" if geometry_checked else "native_counts_geometry_blocked",
        "frame_count": len(frames),
        "unique_sar_event_count": len(events),
        "audit_issue_count": len(audit_issues),
        "audit_issues": audit_issues,
        "geometry_events_checked": geometry_checked,
        "fixed_searchable_cells": domain_count,
        "cell_size_km": cell_size,
        "gates": gates,
    }
    if write_artifacts:
        destination = Path(output_dir or seed_dir)
        fieldnames = sorted({key for row in rows for key in row})
        with (destination / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        _write_json(destination / "check.json", result)
    return result


def _run_batch(args) -> dict:
    output_dir = Path(args.output_dir)
    _refuse_empty_output(output_dir)
    seeds = _validate_seeds(args.seeds)
    if args.steps < 1:
        raise ValueError("steps must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    root_manifest = {
        "schema_version": "persistent-coverage-evaluation/v1",
        "status": "running",
        "scenario": args.scenario,
        "transport": args.transport,
        "fixture": args.transport == "fixture",
        "seeds": list(seeds),
        "requested_steps_per_seed": args.steps,
        "git_commit": _git("rev-parse", "HEAD") or None,
        "dirty_diff_sha256": _dirty_hash(),
        "started_at_wall_utc": _wall_time(),
        "runs": [],
    }
    _write_json(output_dir / "manifest.json", root_manifest)
    runs = []
    for seed in seeds:
        run = _capture_seed(args.scenario, seed, args.steps, output_dir, args.transport)
        runs.append(run)
        root_manifest["runs"] = runs
        _write_json(output_dir / "manifest.json", root_manifest)
    statuses = {run.get("status") for run in runs}
    status = "PASS" if statuses == {"PASS"} else "FAIL" if "FAIL" in statuses else "BLOCKED"
    summary = {
        "schema_version": "persistent-coverage-evaluation-summary/v1",
        "status": status,
        "scenario": args.scenario,
        "transport": args.transport,
        "seeds": list(seeds),
        "requested_steps_per_seed": args.steps,
        "runs": runs,
        "note": "fixture transport does not validate real LLM selection quality" if args.transport == "fixture" else "live transport requires real model credentials",
    }
    root_manifest.update({"status": status, "runs": runs, "finished_at_wall_utc": _wall_time()})
    _write_json(output_dir / "summary.json", summary)
    _write_json(output_dir / "manifest.json", root_manifest)
    return summary


def _refuse_empty_output(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite existing output: {output_dir}")


def _validate_seeds(values) -> tuple[int, ...]:
    seeds = tuple(int(value) for value in values)
    if not seeds or len(seeds) != len(set(seeds)):
        raise ValueError("seeds must be non-empty and unique")
    return seeds


def _compare_dirs(baseline_dir: Path, candidate_dir: Path, output_dir: Path) -> dict:
    baseline = json.loads((baseline_dir / "manifest.json").read_text(encoding="utf-8"))
    candidate = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
    if baseline.get("scenario") != candidate.get("scenario"):
        return {"status": "BLOCKED", "reason": "scenario_mismatch"}
    if baseline.get("transport") != candidate.get("transport"):
        return {"status": "BLOCKED", "reason": "transport_mismatch"}
    if baseline.get("config_sha256") and candidate.get("config_sha256") and baseline["config_sha256"] != candidate["config_sha256"]:
        return {"status": "BLOCKED", "reason": "config_mismatch"}
    baseline_runs = {int(run["seed"]): run for run in baseline.get("runs", ())}
    candidate_runs = {int(run["seed"]): run for run in candidate.get("runs", ())}
    if set(baseline_runs) != set(candidate_runs):
        return {"status": "BLOCKED", "reason": "seed_mismatch"}
    pairs = []
    for seed in sorted(baseline_runs):
        left = json.loads((baseline_dir / f"seed-{seed}/check.json").read_text(encoding="utf-8")) if (baseline_dir / f"seed-{seed}/check.json").is_file() else None
        right = json.loads((candidate_dir / f"seed-{seed}/check.json").read_text(encoding="utf-8")) if (candidate_dir / f"seed-{seed}/check.json").is_file() else None
        if left is None or right is None:
            return {"status": "BLOCKED", "reason": f"missing_check:{seed}"}
        left_mean = left.get("gates", {}).get("c60_time_mean_pct")
        right_mean = right.get("gates", {}).get("c60_time_mean_pct")
        pairs.append({
            "seed": seed,
            "baseline_status": left.get("status"),
            "candidate_status": right.get("status"),
            "baseline_c60_time_mean_pct": left_mean,
            "candidate_c60_time_mean_pct": right_mean,
            "delta_pct_points": None if left_mean is None or right_mean is None else right_mean - left_mean,
        })
    status = "BLOCKED" if any(item["baseline_status"] == "BLOCKED" for item in pairs) else "PASS"
    result = {
        "schema_version": "persistent-coverage-comparison/v1",
        "status": status,
        "scenario": candidate.get("scenario"),
        "transport": candidate.get("transport"),
        "pairs": pairs,
        "note": "A blocked baseline cannot establish a controlled improvement comparison.",
    }
    _refuse_empty_output(output_dir)
    output_dir.mkdir(parents=True)
    _write_json(output_dir / "summary.json", result)
    _write_json(output_dir / "manifest.json", {"schema_version": "persistent-coverage-comparison/v1", **result})
    return result


def _live_budget(args) -> dict:
    seeds = _validate_seeds(args.seeds)
    if args.steps < 1:
        raise ValueError("steps must be positive")
    from src.schedule.config_loader import ConfigLoader

    config = ConfigLoader.load()
    coverage = config.mission.coverage
    timing = config.mission.information_update
    return {
        "schema_version": "persistent-coverage-live-budget/v1",
        "status": "READY",
        "scenario": args.scenario,
        "seeds": list(seeds),
        "steps": args.steps,
        "transport_calls": 0,
        "model_calls": 0,
        "decision_max_tokens": 1536,
        "planning_timeout_seconds": timing.planning_deadline_seconds,
        "postprocess_reserve_seconds": timing.postprocess_reserve_seconds,
        "note": "Budget calculation only; no engine or model transport was invoked.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--scenario", required=True, choices=COVERAGE_SCENARIOS)
    run.add_argument("--transport", required=True, choices=("fixture", "live"))
    run.add_argument("--seeds", required=True, nargs="+", type=int)
    run.add_argument("--steps", required=True, type=int)
    run.add_argument("--output-dir", required=True, type=Path)

    check = subparsers.add_parser("check-log")
    check.add_argument("--log", required=True, type=Path)
    check.add_argument("--output-dir", required=True, type=Path)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline-dir", required=True, type=Path)
    compare.add_argument("--candidate-dir", required=True, type=Path)
    compare.add_argument("--output-dir", required=True, type=Path)

    budget = subparsers.add_parser("live-budget")
    budget.add_argument("--scenario", required=True, choices=COVERAGE_SCENARIOS)
    budget.add_argument("--steps", required=True, type=int)
    budget.add_argument("--seeds", required=True, nargs="+", type=int)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = _run_batch(args)
        elif args.command == "check-log":
            _refuse_empty_output(args.output_dir)
            args.output_dir.mkdir(parents=True)
            result = check_log(args.log, output_dir=args.output_dir, write_artifacts=True)
            _write_json(args.output_dir / "manifest.json", {
                "schema_version": "persistent-coverage-check-manifest/v1",
                "source_log": str(args.log),
                **result,
            })
            _write_json(args.output_dir / "summary.json", result)
        elif args.command == "compare":
            result = _compare_dirs(args.baseline_dir, args.candidate_dir, args.output_dir)
        else:
            result = _live_budget(args)
    except (FileExistsError, FileNotFoundError, ValueError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False))
        return EXIT_BLOCKED
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return {
        "PASS": EXIT_PASS,
        "FAIL": EXIT_FAIL,
        "BLOCKED": EXIT_BLOCKED,
        "READY": EXIT_PASS,
    }.get(result.get("status"), EXIT_BLOCKED)


if __name__ == "__main__":
    raise SystemExit(main())

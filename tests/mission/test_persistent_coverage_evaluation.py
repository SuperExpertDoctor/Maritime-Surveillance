import json

import pytest

from scripts.evaluate_persistent_coverage import check_log
from src.mission.outcome_evaluator import EvaluationTick, OutcomeEvaluator


def _metrics(time_min, covered=0):
    pct = covered / 2 * 100
    return {
        "schema_version": "persistent-coverage/v1",
        "episode_id": "evaluation-episode",
        "as_of_min": time_min,
        "status": "ok",
        "source": "sar",
        "denominator": "fixed_searchable_sea",
        "primary_window_min": 60,
        "fixed_searchable_cells": 2,
        "fixed_searchable_area_km2": 200.0,
        "currently_searchable_cells": 2,
        "weather_blocked_cells": 0,
        "ever_scanned_cells": covered,
        "cumulative_pct": pct,
        "unseen_pct": 100.0 - pct,
        "overdue_seen_pct": 0.0,
        "windows": [{
            "minutes": minutes,
            "covered_cells": covered,
            "covered_area_km2": covered * 100.0,
            "coverage_pct": pct,
            "currently_searchable_coverage_pct": pct,
            "window_complete": time_min >= minutes,
        } for minutes in (30, 60, 120)],
    }


def _sar_event(*, time=1.0, cells=((0, 0),)):
    return {
        "type": "sar_scan",
        "time": time,
        "data": {
            "episode_id": "evaluation-episode",
            "uav_id": "UAV-1",
            "task_id": "search-1",
            "generation": 1,
            "cells": [list(cell) for cell in cells],
            "position": [0.0, 0.0],
            "heading_rad": 0.0,
            "look_direction": "right",
            "swath_width_cells": 0.5,
            "near_range_cells": 0.1,
            "along_track_cells": 1.0,
        },
    }


def _frame(time_min, *, event=None, covered=0, footprint=False):
    return {
        "schema_version": "mission-frame/v2",
        "frame_id": int(time_min),
        "episode_id": "evaluation-episode",
        "sim_time_min": time_min,
        "runtime_status": "running",
        "coverage_metrics": _metrics(time_min, covered),
        "uavs": [{
            "id": "UAV-1",
            "sar_scan_position": [0.0, 0.0] if footprint else None,
            "sar_actual_heading_rad": 0.0 if footprint else None,
            "sar_look_direction": "right" if footprint else None,
        }],
        "events": [event] if event else [],
    }


def _write_log(tmp_path, *, tamper=False, missing_tick=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    event = _sar_event()
    frames = [_frame(0.0), _frame(1.0, event=event, covered=1, footprint=True)]
    if not missing_tick:
        frames.append(_frame(2.0, event=event, covered=1, footprint=True))
    else:
        frames.append(_frame(3.0, event=event, covered=1, footprint=True))
    if tamper:
        frames[-1]["coverage_metrics"]["windows"][1]["covered_cells"] = 2
    log = tmp_path / "frames.jsonl"
    log.write_text("".join(json.dumps(frame) + "\n" for frame in frames), encoding="utf-8")
    manifest = {
        "schema_version": "persistent-coverage-evaluation-seed/v1",
        "episode_id": "evaluation-episode",
        "scenario": "coverage-open-water",
        "seed": 42,
        "transport": "fixture",
        "clock_dt_min": 1.0,
        "actual_end_time_min": frames[-1]["sim_time_min"],
        "fixed_searchable_cells": [[0, 0], [1, 0]],
        "coverage_execution": {
            "swath_width_cells": 0.5,
            "near_range_cells": 0.1,
            "along_track_cells": 1.0,
        },
        "config": {"grid": {"resolution": [2, 2], "cell_size_km": 10}},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return log


def test_check_log_recomputes_counts_and_geometry_without_production_metric(tmp_path):
    result = check_log(_write_log(tmp_path))

    assert result["status"] == "BLOCKED"  # short evidence cannot pass a 720 min gate
    assert result["audit_issue_count"] == 0
    assert result["unique_sar_event_count"] == 1
    assert result["geometry_events_checked"] == 1
    assert result["fixed_searchable_cells"] == 2


def test_check_log_catches_tampered_summary_and_missing_tick(tmp_path):
    tampered = check_log(_write_log(tmp_path / "tampered", tamper=True))
    assert tampered["audit_issue_count"] > 0
    assert any("window_count_mismatch" in issue for issue in tampered["audit_issues"])

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    missing = check_log(_write_log(missing_dir, missing_tick=True))
    assert missing["status"] == "BLOCKED"
    assert any("missing_tick" in issue for issue in missing["audit_issues"])


def test_check_log_keeps_ever_scanned_cells_after_a_revisit(tmp_path):
    event_one = _sar_event(time=1.0)
    event_two = _sar_event(time=2.0)
    frames = [
        _frame(0.0),
        _frame(1.0, event=event_one, covered=1, footprint=True),
        _frame(2.0, event=event_two, covered=1, footprint=True),
    ]
    log = tmp_path / "frames.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(json.dumps(frame) + "\n" for frame in frames),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "persistent-coverage-evaluation-seed/v1",
        "episode_id": "evaluation-episode",
        "scenario": "coverage-open-water",
        "seed": 42,
        "transport": "fixture",
        "clock_dt_min": 1.0,
        "actual_end_time_min": 2.0,
        "fixed_searchable_cells": [[0, 0], [1, 0]],
        "coverage_execution": {
            "swath_width_cells": 0.5,
            "near_range_cells": 0.1,
            "along_track_cells": 1.0,
        },
        "config": {"grid": {"resolution": [2, 2], "cell_size_km": 10}},
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )

    result = check_log(log)

    assert result["audit_issue_count"] == 0


def test_check_log_does_not_use_future_scan_for_an_earlier_frame(tmp_path):
    event = _sar_event(time=2.0)
    frames = [
        _frame(0.0),
        _frame(1.0),
        _frame(2.0, event=event, covered=1, footprint=True),
    ]
    log = tmp_path / "frames.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(json.dumps(frame) + "\n" for frame in frames),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "persistent-coverage-evaluation-seed/v1",
        "episode_id": "evaluation-episode",
        "scenario": "coverage-open-water",
        "seed": 42,
        "transport": "fixture",
        "clock_dt_min": 1.0,
        "actual_end_time_min": 2.0,
        "fixed_searchable_cells": [[0, 0], [1, 0]],
        "coverage_execution": {
            "swath_width_cells": 0.5,
            "near_range_cells": 0.1,
            "along_track_cells": 1.0,
        },
        "config": {"grid": {"resolution": [2, 2], "cell_size_km": 10}},
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )

    result = check_log(log)

    assert result["audit_issue_count"] == 0


def test_outcome_evaluator_integrates_native_coverage_once_per_sim_time():
    evaluator = OutcomeEvaluator("evaluation-episode")
    empty = ()
    metrics_one = _metrics(1.0, covered=1)
    metrics_two = _metrics(2.0, covered=1)
    for metrics in (metrics_one, metrics_two):
        for window in metrics["windows"]:
            window["window_complete"] = True
    tick_one = EvaluationTick(1.0, 1.0, empty, empty, empty, empty, empty, empty, 0.5,
                              persistent_coverage=metrics_one)
    tick_duplicate = EvaluationTick(1.0, 1.0, empty, empty, empty, empty, empty, empty, 0.5,
                                    persistent_coverage=metrics_one)
    tick_two = EvaluationTick(2.0, 1.0, empty, empty, empty, empty, empty, empty, 0.5,
                              persistent_coverage=metrics_two)

    evaluator.observe(tick_one)
    evaluator.observe(tick_duplicate)
    evaluator.observe(tick_two)
    outcome = evaluator.finalize()

    coverage = outcome.persistent_coverage
    assert coverage["availability"] is True
    assert coverage["sample_count"] == 2
    assert coverage["windows"]["60"]["sample_count"] == 2
    assert coverage["windows"]["60"]["time_mean_pct"] == pytest.approx(50.0)

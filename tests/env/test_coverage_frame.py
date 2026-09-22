import json

import numpy as np
import pytest

from scripts.evaluate_mixed_maritime import _FixtureGateway
from src.env.simulation import SimulationEngine
from src.control.common.contracts import (
    ControlRouteSnapshot,
    UavRouteSnapshot,
)
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_builder import build_frame


def _frame(engine, **overrides):
    return build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
        **overrides,
    )


def test_baseline_git_output_uses_utf8_for_dirty_diff(monkeypatch):
    from scripts import capture_coverage_baseline as capture

    observed = {}

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return type("Completed", (), {"stdout": "\u8986\u76d6\u5dee\u5f02"})()

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    assert capture._git_output("diff", "--binary") == "\u8986\u76d6\u5dee\u5f02"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "surrogateescape"


def test_legacy_state_frame_has_explicit_null_coverage_without_matrices():
    config = ConfigLoader.load()
    state = StateManager(config)

    frame = build_frame(state, 0, config, include_matrices=False)

    assert frame["coverage_metrics"] is None
    assert "info_matrix" not in frame
    assert "value_matrix" not in frame


def test_frame_coverage_is_pure_json_safe_and_timestamp_aligned():
    engine = SimulationEngine(
        ConfigLoader.load(),
        seed=42,
        llm_gateway=_FixtureGateway(),
        episode_id="coverage-frame-test",
    )
    sm = engine.allocator.sm
    sm.coverage_metrics.record_sar(((10, 10),), at_min=5.0)
    sm.current_time = 5.0
    sm.runtime_status = "paused_model"
    scans_before = sm.coverage_metrics.last_scan_matrix()
    events_before = json.dumps(sm.get_recent_events(0.0), sort_keys=True)
    version_before = sm.information_policy.version

    first = _frame(engine, realtime=True, include_matrices=False)
    repeated = _frame(engine, realtime=True, include_matrices=False)

    assert first["coverage_metrics"] == repeated["coverage_metrics"]
    assert first["coverage_metrics"]["as_of_min"] == first["sim_time_min"] == 5.0
    assert first["coverage_metrics"]["episode_id"] == engine.episode_id
    assert first["coverage_metrics"]["ever_scanned_cells"] == 1
    assert "info_matrix" not in first
    assert "value_matrix" not in first
    json.dumps(first, allow_nan=False)

    first["coverage_metrics"]["windows"][0]["covered_cells"] = 999
    pure = _frame(engine, realtime=True, include_matrices=False)
    assert pure["coverage_metrics"]["windows"][0]["covered_cells"] == 1
    np.testing.assert_array_equal(sm.coverage_metrics.last_scan_matrix(), scans_before)
    assert json.dumps(sm.get_recent_events(0.0), sort_keys=True) == events_before
    assert sm.information_policy.version == version_before
    assert pure["coverage_metrics"] == sm.get_persistent_coverage_stats()


def test_frame_publisher_can_deepcopy_state_without_mappingproxy_error():
    engine = SimulationEngine(
        ConfigLoader.load(),
        seed=42,
        llm_gateway=_FixtureGateway(),
        episode_id="coverage-snapshot-test",
    )
    route = ControlRouteSnapshot(
        task_id="coverage-1",
        task_type="coverage",
        phase="coverage",
        target_contact_id=None,
        route=((1.0, 1.0, 0.0),),
        next_index=0,
        route_revision=1,
        planning_map_version=1,
        status="ready",
        coverage_progress={"nested": {"cells": np.array([1, 2])}},
    )
    engine.allocator.sm.set_control_route(
        "UAV-1", UavRouteSnapshot("coverage-snapshot-test", 0, route),
    )

    frame = _frame(engine, realtime=True, include_matrices=False)

    json.dumps(frame, allow_nan=False)
    assert frame["uavs"][0]["task_visual"]["coverage_progress"] == {
        "nested": {"cells": [1, 2]},
    }


def test_zero_heading_is_preserved_in_sar_beam_geometry():
    engine = SimulationEngine(
        ConfigLoader.load(),
        seed=42,
        llm_gateway=_FixtureGateway(),
        episode_id="heading-zero-test",
    )
    uav = engine.uavs[0]
    uav.sar_look_direction = "left"
    uav.sar_imaging = True
    uav.sar_scan_heading_rad = 0.0

    frame = _frame(engine, realtime=True, include_matrices=False)

    assert frame["uavs"][0]["sar_beam"]["heading"] == 0.0


def test_event_window_does_not_duplicate_boundary_event():
    config = ConfigLoader.load()
    state = StateManager(config)
    state.episode_id = "event-window-test"
    state.current_time = 1.0
    state.add_event("boundary", {"value": 1})
    first = build_frame(state, 0, config, include_matrices=False)

    state.current_time = 2.0
    second = build_frame(state, 0, config, include_matrices=False)

    assert [event["event_id"] for event in first["events"]] == [
        "event-window-test:1",
    ]
    assert second["events"] == []


def test_reset_configures_a_fresh_metric_for_the_new_episode():
    engine = SimulationEngine(
        ConfigLoader.load(),
        seed=42,
        llm_gateway=_FixtureGateway(),
        episode_id="coverage-before-reset",
    )
    engine.allocator.sm.coverage_metrics.record_sar(((10, 10),), at_min=0.0)
    old_episode = engine.episode_id

    engine.reset(seed=43)
    frame = _frame(engine, include_matrices=False)

    assert engine.episode_id != old_episode
    assert frame["coverage_metrics"]["episode_id"] == engine.episode_id
    assert frame["coverage_metrics"]["as_of_min"] == 0.0
    assert frame["coverage_metrics"]["ever_scanned_cells"] == 0


def test_baseline_interrupt_stops_all_seeds_and_captures_actual_terminal_time(tmp_path, monkeypatch):
    from scripts import capture_coverage_baseline as capture

    original_builder = capture.build_coverage_scenario
    started_seeds = []

    def interrupted_builder(name, *, seed, transport):
        engine = original_builder(name, seed=seed, transport=transport)
        started_seeds.append(seed)

        def interrupt_step():
            engine.allocator.sm.current_time = engine.clock.tick()
            raise KeyboardInterrupt

        monkeypatch.setattr(engine, "step", interrupt_step)
        return engine

    monkeypatch.setattr(capture, "build_coverage_scenario", interrupted_builder)
    output = tmp_path / "interrupted"
    summary = capture.capture_baseline(
        scenario="coverage-open-water", seeds=(42, 43), steps=3, output_dir=output,
    )

    assert started_seeds == [42]
    assert summary["status"] == "incomplete"
    assert summary["coverage_gate_status"] == "not_evaluated"
    run = summary["runs"][0]
    assert run["actual_end_time_min"] == 1.0
    assert run["completed_steps"] == 0
    frames = [json.loads(line) for line in (output / "seed-42/frames.jsonl").read_text().splitlines()]
    assert [frame["sim_time_min"] for frame in frames] == [0.0, 1.0]
    assert frames[-1]["coverage_metrics"]["as_of_min"] == 1.0
    assert run["frame_count"] == len(frames)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "incomplete"
    assert manifest["seeds"] == [42, 43]
    assert not (output / "seed-43").exists()
    with pytest.raises(FileExistsError):
        capture.capture_baseline(
            scenario="coverage-open-water", seeds=(42,), steps=1, output_dir=output,
        )


def test_baseline_initial_capture_failure_writes_failed_manifests(tmp_path, monkeypatch):
    from scripts import capture_coverage_baseline as capture

    def broken_frame(*_args, **_kwargs):
        raise ValueError("cannot serialize frame")

    monkeypatch.setattr(capture, "capture_frame", broken_frame)
    output = tmp_path / "failed"
    summary = capture.capture_baseline(
        scenario="coverage-open-water", seeds=(42,), steps=1, output_dir=output,
    )

    assert summary["status"] == "incomplete"
    manifest = json.loads((output / "seed-42/manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert "cannot serialize frame" in manifest["stopped_reason"]
    assert manifest["frame_count"] == manifest["completed_steps"] == 0
    assert manifest["actual_end_time_min"] == 0.0


def test_baseline_startup_failure_is_reported_without_inventing_an_end_time(tmp_path, monkeypatch):
    from scripts import capture_coverage_baseline as capture

    def broken_builder(*_args, **_kwargs):
        raise RuntimeError("engine initialization failed")

    monkeypatch.setattr(capture, "build_coverage_scenario", broken_builder)
    summary = capture.capture_baseline(
        scenario="coverage-open-water", seeds=(42,), steps=1, output_dir=tmp_path,
    )

    assert summary["status"] == "incomplete"
    manifest = json.loads((tmp_path / "seed-42/manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["actual_end_time_min"] is None
    assert manifest["completed_steps"] == manifest["frame_count"] == 0
    assert "engine initialization failed" in manifest["stopped_reason"]


def test_baseline_event_identity_is_episode_type_time_and_uav_only():
    from scripts.capture_coverage_baseline import _event_identity

    event = {"type": "sar_scan", "time": 1.0, "data": {"uav_id": "U1", "cells": [[1, 1]]}}
    identity = _event_identity("episode-1", event)
    assert identity == ("episode-1", "sar_scan", 1.0, "U1")
    changed = {**event, "data": {**event["data"], "cells": [[2, 2]]}}
    assert _event_identity("episode-1", changed) == identity
    assert _event_identity("episode-2", event) != identity
    assert _event_identity("episode-1", {**event, "type": "other"}) != identity
    assert _event_identity("episode-1", {**event, "time": 2.0}) != identity
    assert _event_identity("episode-1", {**event, "data": {"uav_id": "U2"}}) != identity


@pytest.mark.parametrize("conflicting", [False, True])
def test_baseline_deduplicates_equal_events_but_fails_conflicting_payloads(
    tmp_path, monkeypatch, conflicting,
):
    from scripts import capture_coverage_baseline as capture

    real_capture = capture.capture_frame

    def controlled_events(engine, **kwargs):
        frame = real_capture(engine, **kwargs)
        cells = [[2, 2]] if conflicting and engine.clock.time > 0 else [[1, 1]]
        frame["events"] = [
            {"type": "sar_scan", "time": 0.0, "data": {"uav_id": "U1", "cells": cells}},
            {"type": "sar_scan", "time": 0.0, "data": {"uav_id": "U2", "cells": [[1, 1]]}},
        ]
        return frame

    monkeypatch.setattr(capture, "capture_frame", controlled_events)
    summary = capture.capture_baseline(
        scenario="coverage-open-water", seeds=(42,), steps=1, output_dir=tmp_path,
    )
    run = summary["runs"][0]
    assert run["status"] == ("failed" if conflicting else "completed")
    assert summary["status"] == ("incomplete" if conflicting else "completed")
    events = [json.loads(line) for line in (tmp_path / "seed-42/events.jsonl").read_text().splitlines()]
    assert len(events) == run["event_count"] == 2
    assert {event["data"]["uav_id"] for event in events} == {"U1", "U2"}
    if conflicting:
        assert "conflicting event payload" in run["stopped_reason"]
        frames = [json.loads(line) for line in (tmp_path / "seed-42/frames.jsonl").read_text().splitlines()]
        assert frames[-1]["events"][0]["data"]["cells"] == [[2, 2]]


@pytest.mark.parametrize("event_type,first_data,second_data", [
    ("contact_created", {"contact_id": "C0001"}, {"contact_id": "C0002"}),
    (
        "return_triggered",
        {"uav_id": "U1", "reason": "range_reserve"},
        {"uav_id": "U1", "reason": "lifecycle_or_task_return"},
    ),
])
@pytest.mark.parametrize("distinct", [False, True])
def test_baseline_retains_distinct_non_sar_history_and_deduplicates_exact_repeats(
    tmp_path, monkeypatch, event_type, first_data, second_data, distinct,
):
    from scripts import capture_coverage_baseline as capture

    real_capture = capture.capture_frame
    first = {"type": event_type, "time": 0.0, "data": first_data}
    second = {"type": event_type, "time": 0.0, "data": second_data if distinct else first_data}
    assert capture._event_identity("episode", first) == capture._event_identity("episode", second)

    def repeated_history(engine, **kwargs):
        frame = real_capture(engine, **kwargs)
        # Both the initial frame and the next frame contain repeated history.
        frame["events"] = json.loads(json.dumps([first, second, first]))
        return frame

    monkeypatch.setattr(capture, "capture_frame", repeated_history)
    summary = capture.capture_baseline(
        scenario="coverage-open-water", seeds=(42,), steps=1, output_dir=tmp_path,
    )

    assert summary["status"] == "completed"
    run = summary["runs"][0]
    assert run["status"] == "completed"
    assert run["completed_steps"] == run["actual_end_time_min"] == 1
    assert run["stopped_reason"] is None
    events = [json.loads(line) for line in (tmp_path / "seed-42/events.jsonl").read_text().splitlines()]
    assert events == ([first, second] if distinct else [first])
    assert run["event_count"] == len(events)

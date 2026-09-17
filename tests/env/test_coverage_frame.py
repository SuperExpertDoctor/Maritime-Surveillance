import json

import numpy as np
import pytest

from scripts.evaluate_mixed_maritime import _FixtureGateway
from src.env.simulation import SimulationEngine
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

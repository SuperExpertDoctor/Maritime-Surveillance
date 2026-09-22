import json

import pytest


def test_build_scenario_fixture_is_an_engine_and_capture_uses_production_frame():
    from scripts.replay_restoration_scenarios import build_scenario, capture_frame

    engine = build_scenario("V01", seed=42, transport="fixture")
    before = capture_frame(engine, total_steps=2)

    engine.step()
    after = capture_frame(engine, total_steps=2)

    assert after["schema_version"] == "mission-frame/v2"
    assert after["frame_id"] == 1
    assert after["sim_time_min"] == pytest.approx(1.0)
    assert after["uavs"] != before["uavs"]


def test_run_scenario_writes_provenance_and_canonical_artifacts(tmp_path):
    from scripts.replay_restoration_scenarios import run_scenario

    output_dir = tmp_path / "v01"
    result = run_scenario(
        "V01", seed=42, steps=2, output_dir=output_dir, transport="fixture",
    )

    assert result["status"] == "finished"
    assert result["completed_steps"] == 2
    assert result["sim_time"] == pytest.approx(2.0)
    for name in ("manifest.json", "frames.jsonl", "events.jsonl", "metrics.json", "audit.jsonl"):
        assert (output_dir / name).is_file(), name

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scenario"] == "V01"
    assert manifest["seed"] == 42
    assert manifest["requested_steps"] == 2
    assert manifest["completed_steps"] == 2
    assert manifest["transport"] == "fixture"
    assert manifest["fixture"] is True
    assert manifest["memory_version"] == "baseline"
    assert manifest["git_commit"]
    assert manifest["config_hash"]
    assert manifest["input_hashes"]
    assert manifest["role_bindings"]["decision_maker"]["provider"] == "fixture"

    frames = [
        json.loads(line)
        for line in (output_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [frame["frame_id"] for frame in frames] == [1, 2]
    assert frames[0]["uavs"] != frames[1]["uavs"]


def test_runner_refuses_existing_manifest(tmp_path):
    from scripts.replay_restoration_scenarios import run_scenario

    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    (output_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing to overwrite"):
        run_scenario("V01", seed=42, steps=1, output_dir=output_dir, transport="fixture")


def test_runner_normalizes_controller_phases_for_observation_gates():
    from scripts.replay_restoration_scenarios import _observed_phase_names

    observed = _observed_phase_names([{
        "uavs": [
            {"task_visual": {"task_type": "coverage", "phase": "transit_astar"}},
            {"task_visual": {"task_type": "coverage", "phase": "align_scan"}},
            {"task_visual": {"task_type": "probe", "phase": "baseline", "observation_started": False}},
            {"task_visual": {"task_type": "probe", "phase": "baseline", "observation_started": True}},
            {"task_visual": {"task_type": "probe", "phase": "near", "observation_started": True}},
            {"task_visual": {"task_type": "track", "phase": "tracking"}},
            {"task_visual": {"task_type": "return", "phase": "return"}},
            {"task_visual": {"task_type": "holding", "phase": "holding"}},
        ],
    }])

    assert observed == {
        "coverage_transit",
        "coverage_scan",
        "probe_approach",
        "probe_baseline",
        "probe_near",
        "track_active",
        "return",
        "holding",
    }


def test_v07_fixture_runner_drives_assessment_return_and_handoff(tmp_path):
    from scripts.replay_restoration_scenarios import run_scenario

    output_dir = tmp_path / "v07"
    result = run_scenario(
        "V07", seed=42, steps=30, output_dir=output_dir, transport="fixture",
    )

    assert result["status"] == "finished"
    events = [
        json.loads(line)
        for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    event_types = {event["type"] for event in events}
    assert {
        "assessment_applied",
        "uav_fuel_low_warning",
        "handoff_required",
        "handoff_assignment_committed",
        "handoff_eo_lock_acquired",
        "return_reserved",
    } <= event_types

    handoff = next(
        event for event in events if event["type"] == "handoff_required"
    )
    assignment = next(
        event for event in events
        if event["type"] == "handoff_assignment_committed"
        and event["data"]["handoff_id"] == handoff["data"]["handoff_id"]
    )
    lock = next(
        event for event in events
        if event["type"] == "handoff_eo_lock_acquired"
        and event["data"]["handoff_id"] == handoff["data"]["handoff_id"]
    )
    # Requirement and assignment are one atomic scheduler boundary; the EO
    # lock must still be acquired by a later observed control tick.
    assert handoff["time"] <= assignment["time"] < lock["time"]


def test_legacy_scheduling_runner_writes_auditable_replay_artifacts(tmp_path):
    from scripts.validate_legacy_search_scheduling import run_validation

    output_dir = tmp_path / "legacy"
    result = run_validation(
        seed=42,
        steps=6,
        transport="fixture",
        output_dir=output_dir,
    )

    assert result["status"] == "PASS"
    assert result["completed_steps"] == 6
    assert result["metrics"]["reassignment_count"] >= 1
    assert result["metrics"]["pending_interval_count"] >= 1
    assert result["audit"]["status"] == "PASS"
    for name in (
        "manifest.json",
        "frames.jsonl",
        "events.jsonl",
        "metrics.json",
        "audit.json",
    ):
        assert (output_dir / name).is_file(), name

    frames = [
        json.loads(line)
        for line in (output_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(frames) == 6
    region_history = [
        next(
            region for region in frame["search_regions"]
            if region["id"] == result["metrics"]["fixture_task_id"]
        )
        for frame in frames
    ]
    assert region_history[0]["assigned_uav_id"]
    assert any(region["assigned_uav_id"] is None for region in region_history)
    assert region_history[-1]["assigned_uav_id"]
    assert [
        record["status"]
        for record in frames[1]["task_records"]
        if record["task_id"] == result["metrics"]["fixture_task_id"]
    ] == ["approved"]


def test_legacy_scheduling_check_log_detects_projection_and_overlap_failures(tmp_path):
    from scripts.validate_legacy_search_scheduling import check_log

    output_dir = tmp_path / "audit"
    output_dir.mkdir()
    manifest = {
        "schema_version": "legacy-search-scheduling/v1",
        "requested_steps": 2,
        "completed_steps": 2,
        "runtime_status": "running",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )
    frames = [
        {
            "frame_id": 1,
            "sim_time_min": 1.0,
            "runtime_status": "running",
            "search_regions": [
                {"id": "search-a", "bbox": [1, 1, 5, 5], "type": "search", "status": "active", "assigned_uav_id": "UAV-1"},
                {"id": "search-b", "bbox": [4, 4, 8, 8], "type": "search", "status": "active", "assigned_uav_id": "UAV-2"},
            ],
            "uavs": [
                {"id": "UAV-1", "operational_status": "available", "assigned_region_id": "wrong"},
                {"id": "UAV-2", "operational_status": "available", "assigned_region_id": "search-b"},
            ],
            "task_records": [
                {"task_id": "search-a", "kind": "search", "status": "executing", "assigned_uav_id": "UAV-1"},
            ],
            "events": [],
        },
        {
            "frame_id": 2,
            "sim_time_min": 2.0,
            "runtime_status": "paused_model",
            "search_regions": [],
            "uavs": [],
            "task_records": [],
            "events": [],
        },
    ]
    (output_dir / "frames.jsonl").write_text(
        "".join(json.dumps(frame) + "\n" for frame in frames),
        encoding="utf-8",
    )

    audit = check_log(output_dir / "frames.jsonl")

    assert audit["status"] == "FAIL"
    assert any(issue.startswith("runtime_paused_model:") for issue in audit["issues"])
    assert any(issue.startswith("overlapping_active_search:") for issue in audit["issues"])
    assert any(issue.startswith("region_uav_mismatch:") for issue in audit["issues"])
    assert audit["failure_classes"]["navigation"] == []
    assert audit["failure_classes"]["scheduling"]


def test_legacy_scheduling_check_log_detects_missing_pending_region(tmp_path):
    from scripts.validate_legacy_search_scheduling import check_log

    output_dir = tmp_path / "pending-gap"
    output_dir.mkdir()
    (output_dir / "manifest.json").write_text(
        json.dumps({
            "schema_version": "legacy-search-scheduling/v1",
            "requested_steps": 2,
            "completed_steps": 2,
        }),
        encoding="utf-8",
    )
    frames = [
        {
            "frame_id": 1,
            "sim_time_min": 1.0,
            "runtime_status": "running",
            "search_regions": [{"id": "search-a", "bbox": [1, 1, 5, 5], "type": "search", "status": "active", "assigned_uav_id": "UAV-1"}],
            "uavs": [{"id": "UAV-1", "operational_status": "available", "assigned_region_id": "search-a"}],
            "task_records": [{"task_id": "search-a", "kind": "search", "status": "executing", "assigned_uav_id": "UAV-1"}],
            "events": [{"type": "mission_task_released", "time": 1.0, "data": {"task_id": "search-a", "status": "approved", "reason": "uav_return"}}],
        },
        {
            "frame_id": 2,
            "sim_time_min": 2.0,
            "runtime_status": "running",
            "search_regions": [],
            "uavs": [],
            "task_records": [],
            "events": [{"type": "pending_search_reassigned", "time": 2.0, "data": {"assignments": [{"task_id": "search-a", "uav_id": "UAV-2"}]}}],
        },
    ]
    (output_dir / "frames.jsonl").write_text(
        "".join(json.dumps(frame) + "\n" for frame in frames),
        encoding="utf-8",
    )

    audit = check_log(output_dir / "frames.jsonl")

    assert audit["status"] == "FAIL"
    assert "pending_region_disappeared:search-a:2" in audit["issues"]


def test_legacy_scheduling_runner_refuses_existing_manifest(tmp_path):
    from scripts.validate_legacy_search_scheduling import run_validation

    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    (output_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        run_validation(
            seed=42,
            steps=1,
            transport="fixture",
            output_dir=output_dir,
        )

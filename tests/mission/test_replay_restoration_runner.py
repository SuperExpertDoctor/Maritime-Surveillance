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

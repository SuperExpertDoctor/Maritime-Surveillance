import json
from pathlib import Path

from scripts import run_visual_fixture_server


def test_visual_fixture_prepares_legacy_replay_artifacts(tmp_path, monkeypatch):
    calls = []

    def fake_run_scenario(name, *, seed, steps, output_dir, transport):
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        (output_path / "frames.jsonl").write_text(
            json.dumps({
                "frame_id": 1,
                "sim_time_min": 1.0,
                "timestamp": "00:01:00",
                "total_steps": steps,
                "events": [],
            }) + "\n",
            encoding="utf-8",
        )
        calls.append((name, seed, steps, transport))
        return {"status": "finished"}

    monkeypatch.setattr(run_visual_fixture_server, "run_scenario", fake_run_scenario)

    run_visual_fixture_server.prepare_replay_compatibility_artifacts(tmp_path)

    assert calls == [
        ("V06", 42, 40, "fixture"),
        ("V07", 42, 45, "fixture"),
    ]
    for filename in ("v06-seed42-frames.jsonl", "v07-seed42-frames.jsonl"):
        frames = [
            json.loads(line)
            for line in (tmp_path / filename).read_text(encoding="utf-8").splitlines()
        ]
        assert len(frames) == 480
        assert frames[0]["frame_id"] == 1
        assert frames[-1]["frame_id"] == 480
        assert frames[-1]["total_steps"] == 480

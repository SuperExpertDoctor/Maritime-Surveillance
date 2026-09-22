"""Offline checks for evidence preservation in the live validation script."""
import json
from types import SimpleNamespace

import pytest

from scripts import validate_live_adversarial as validation


@pytest.mark.parametrize("failure", ["step", "frame", "serialization", None])
def test_remaining_events_are_collected_on_exit_without_duplicates(tmp_path, monkeypatch, failure):
    recorded = []
    gateway = SimpleNamespace(
        transport=object(), call_log=[], assert_ready=lambda: None,
        set_context=lambda *args: None, _redact=lambda value: value,
        redact_log=lambda value: value,
    )
    state = SimpleNamespace(cycle=0, get_recent_events=lambda _: list(recorded))
    engine = SimpleNamespace(
        clock=SimpleNamespace(time=0), episode_id="stub", runtime_status="running",
        allocator=SimpleNamespace(sm=state, llm_client=SimpleNamespace(gateway=gateway)),
        config=None, ships=[], uavs=[], obstacles=[], bases=[], summary=lambda: {},
    )

    def step():
        engine.clock.time += 1
        assert engine.clock.time <= 2, "stub loop must stop after two steps"
        kind = "vessel_created" if engine.clock.time == 1 else "opponent_maneuver_installed"
        recorded.append(dict(event_id=f"stub:{engine.clock.time}", type=kind,
                             time=engine.clock.time - 1, data={"vessel_class": "type_i"}))
        if engine.clock.time == 2:
            if failure == "step":
                raise RuntimeError("step failed after event")
            if failure is None:
                engine.runtime_status = "paused_model"
        return {}

    def frame(*args, **kwargs):
        if engine.clock.time == 2:
            if failure == "frame":
                raise ValueError("frame failed after event")
            if failure == "serialization":
                return {"invalid": float("nan")}
        return {}

    engine.step = step
    monkeypatch.setattr(validation, "SimulationEngine", lambda *a, **kw: engine)
    monkeypatch.setattr(validation.ConfigLoader, "load", lambda: None)
    monkeypatch.setattr(validation, "build_frame", frame)
    monkeypatch.setattr(validation.subprocess, "check_output", lambda *a, **kw: "stub-commit")
    output = tmp_path / "evidence"
    report = validation.run(output, seconds=60, seed=42)

    saved = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    assert saved == recorded
    assert report["event_counts"] == {"vessel_created": 1, "opponent_maneuver_installed": 1}
    assert report["released_classes"] == {"type_i": 1}
    assert report["frames"] == (2 if failure is None else 1)
    assert (report["error"] is None) == (failure is None)
    assert not report["passed"]
    assert json.loads((output / "report.json").read_text())["event_counts"] == report["event_counts"]

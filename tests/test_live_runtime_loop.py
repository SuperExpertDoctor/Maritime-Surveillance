"""Entry-point regressions: no network, server threads, or real model calls."""
from types import SimpleNamespace
import threading

import pytest

import main as cli
from src.env.simulation import SimulationEngine


@pytest.mark.parametrize("start_server", [False, True])
def test_command_boundary_published_before_blocking_model(monkeypatch, tmp_path, start_server):
    from scripts.evaluate_mixed_maritime import _FixtureGateway
    from src.mission.contracts import IntentCommand, VesselCommand
    from src.schedule.config_loader import ConfigLoader
    from src.vis.backend.frame_logger import FrameLogger
    from src.vis.backend.frame_publisher import FramePublisher

    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=_FixtureGateway())
    engine.last_result = {"trigger_type": "heavy", "llm_cycle": {"stale": True}}
    engine.vessel_commands.enqueue(VesselCommand(
        "boundary-vessel", engine.episode_id, "create",
        vessel_class="type_ii", position_cells=(12.5, 8.5),
    ))
    engine.intent_commands.enqueue(IntentCommand(
        "boundary-focus", engine.episode_id, "create", None, None,
        {"label": "focus", "bbox": [10, 10, 15, 15], "mode": "search_priority",
         "priority": "high", "weight": 1, "valid_duration_min": 120},
    ))
    entered, release = threading.Event(), threading.Event()
    errors, publications = [], []
    publisher = FramePublisher(FrameLogger(str(tmp_path)))

    def block_model(_time):
        entered.set()
        assert release.wait(5)
        raise RuntimeError("stop offline model gate")

    monkeypatch.setattr(engine, "_prepare_red_decision", block_model)

    def publish(current, result):
        publications.append((current.clock.time, dict(result)))
        publisher.push_snapshot(current, result, 1)

    def run():
        try:
            cli._run_runtime_loop(engine, 1, publish, start_server=start_server)
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        assert engine.vessel_commands.get("boundary-vessel").status == "applied"
        assert engine.intent_commands.get("boundary-focus").status == "applied"
        assert publications, "applied commands must publish before the model returns"
        assert publications == [(0, {"trigger_type": "none", "action": None, "command_boundary": True})]
        assert publisher.flush(5)
        import json
        frames = [json.loads(line) for line in open(publisher.logger.path, encoding="utf-8")]
        assert len(frames) == 1
        frame = frames[0]
        assert frame["sim_time_min"] == 0 and engine.clock.time == 0
        assert frame["llm_cycle"] is None
        assert frame["actual_vessel_count"] == len(engine.ships)
        assert any(v["scenario_entity_id"] == engine.vessel_commands.get("boundary-vessel").vessel_id
                   for v in frame["scenario_vessels"])
        assert frame["intents"][0]["bbox"] == [10, 10, 15, 15]
        assert any(event["data"].get("command_id") == "boundary-focus" for event in frame["events"])
    finally:
        release.set()
        thread.join(5)
        publisher.close()
    assert not thread.is_alive()
    assert errors == ["stop offline model gate"]


class StubEngine:
    # Preserve the engine's existing early-return contract in the stub.
    run = SimulationEngine.run

    def __init__(self, *, tick_before_pause=False, tick_size=1):
        self.clock = SimpleNamespace(time=0)
        self.allocator = SimpleNamespace(sm=SimpleNamespace(current_time=0, cycle=0))
        self.runtime_status = "running"
        self.last_result = {"trigger_type": "none", "action": None}
        self.ships, self.uavs, self.obstacles, self.bases = [], [], [], []
        self.tick_before_pause = tick_before_pause
        self.tick_size = tick_size
        self.step_calls = 0
        self.pending = {kind: [] for kind in ("runtime", "intent", "vessel")}
        self.applied = []
        self.rejected = []
        self.episode_id = "stub-episode"

    @property
    def vessel_mutation_allowed(self):
        return self.runtime_status != "finished"

    def _set_runtime_state(self, status):
        self.runtime_status = status

    def _publish_runtime_state(self):
        pass

    def step(self, *, on_command_boundary=None):
        self.step_calls += 1
        self.apply_pending_runtime_commands()
        self.apply_pending_intent_commands()
        self.apply_pending_vessel_commands()
        if self.runtime_status != "running":
            return self.last_result
        if self.step_calls > 1 or self.tick_before_pause:
            self.clock.time += self.tick_size
            self.allocator.sm.current_time = self.clock.time
        if self.step_calls == 1:
            self.runtime_status = "paused_model"
            self.last_result = {"trigger_type": "model_blocked", "action": None}
        else:
            self.last_result = {"trigger_type": "none", "action": None}
        return self.last_result

    def _drain(self, kind):
        commands, self.pending[kind] = self.pending[kind], []
        for command in commands:
            if self.runtime_status == "finished":
                self.rejected.append((kind, command))
                continue
            self.applied.append((kind, command, self.clock.time))
            if kind == "runtime" and command == "retry":
                self.runtime_status = "running"
                self.last_result = {"trigger_type": "heavy", "action": "retried"}
            elif kind == "runtime" and command == "abort":
                self.runtime_status = "finished"
        return tuple(commands)

    def apply_pending_runtime_commands(self):
        return self._drain("runtime")

    def apply_pending_intent_commands(self):
        return self._drain("intent")

    def apply_pending_vessel_commands(self):
        return self._drain("vessel")

    def summary(self):
        return dict(steps=self.clock.time, coverage_pct=0, detected_ships=0,
                    ship_count=0, heavy_triggers=0)


@pytest.fixture
def harness(monkeypatch):
    engine = StubEngine()
    publisher = SimpleNamespace(frames=[], closed=False, flushes=0)

    def push(current, result, total):
        assert not publisher.closed
        publisher.frames.append((current.clock.time, current.runtime_status, dict(result)))

    def flush(timeout):
        publisher.flushes += 1
        return True

    publisher.push_snapshot = push
    publisher.flush = flush
    publisher.close = lambda: setattr(publisher, "closed", True)
    logger = SimpleNamespace(path="stub.jsonl")
    app = SimpleNamespace(state=SimpleNamespace(event_loop=object(), frame_logger=logger))
    monkeypatch.setattr(cli.ConfigLoader, "load", lambda _: SimpleNamespace(
        common=SimpleNamespace(clear_outputs_before_run=False)))
    monkeypatch.setattr(cli, "StrategyMemoryStore", lambda _: SimpleNamespace(resolve_version=lambda v: v))
    monkeypatch.setattr(cli, "SimulationEngine", lambda *a, **kw: engine)
    monkeypatch.setattr(cli, "FramePublisher", lambda *a, **kw: publisher)
    monkeypatch.setattr(cli, "FrameLogger", lambda: logger)
    monkeypatch.setattr(cli, "create_app", lambda *a, **kw: app)
    monkeypatch.setattr(cli.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None))
    # Port availability is covered separately; never touch a real listener here.
    monkeypatch.setattr(cli, "_check_port_available", lambda _: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _: pytest.fail("unexpected wait"))
    return engine, publisher


@pytest.mark.parametrize("tick_before_pause", [False, True])
@pytest.mark.parametrize("tick_size", [1, 0.25])
def test_live_retry_consumes_commands_without_idle_frames_or_clock_ticks(
    harness, monkeypatch, tick_before_pause, tick_size,
):
    engine, publisher = harness
    engine.tick_before_pause, engine.tick_size = tick_before_pause, tick_size
    paused_time = tick_size if tick_before_pause else 0
    waits = []

    def wait(delay):
        assert 0 < delay <= 1
        assert not publisher.closed
        assert engine.clock.time == paused_time
        assert engine.step_calls == 1
        waits.append(len(publisher.frames))
        assert len(waits) <= 4, "runtime commands were not consumed"
        if len(waits) == 2:
            engine.pending["intent"].append("create")
            engine.pending["vessel"].append("set_ais")
            engine.pending["runtime"].append("failed_retry")
        if len(waits) == 4:
            engine.pending["runtime"].append("retry")

    monkeypatch.setattr(cli.time, "sleep", wait)
    result = cli.main(steps=3, step_delay=0, probe_llm=False)

    assert result["steps"] == 3 * tick_size
    assert engine.step_calls == 3 + (not tick_before_pause)
    assert waits == [1, 1, 2, 2]
    assert {kind for kind, _, _ in engine.applied} == {"runtime", "intent", "vessel"}
    assert all(t == paused_time for _, _, t in engine.applied)
    assert any(status == "running" and frame["action"] == "retried"
               for _, status, frame in publisher.frames)
    assert publisher.closed and publisher.flushes == 1


@pytest.mark.parametrize("hold_server", [False, True])
def test_abort_exits_even_with_hold_server(harness, monkeypatch, hold_server):
    engine, publisher = harness
    waits = []

    def wait(_):
        waits.append(True)
        assert len(waits) == 1, "abort entered the hold-server loop"
        engine.pending["runtime"].append("abort")

    monkeypatch.setattr(cli.time, "sleep", wait)
    cli.main(steps=3, hold_server=hold_server, step_delay=0, probe_llm=False)
    assert engine.runtime_status == "finished"
    assert engine.step_calls == 1 and engine.clock.time == 0
    assert publisher.frames[-1][1] == "finished"
    assert publisher.closed


def test_no_server_returns_on_model_failure(harness):
    engine, publisher = harness
    result = cli.main(steps=3, start_server=False, hold_server=True,
                      step_delay=0, probe_llm=False)
    assert result["steps"] == 0
    assert engine.step_calls == 1 and publisher.closed


def test_pause_on_last_tick_still_accepts_retry(harness, monkeypatch):
    engine, publisher = harness
    engine.tick_before_pause = True
    monkeypatch.setattr(cli.time, "sleep", lambda _: engine.pending["runtime"].append("retry"))
    result = cli.main(steps=1, step_delay=0, probe_llm=False)
    assert result["steps"] == 1
    assert engine.runtime_status == "finished" and engine.step_calls == 1
    assert publisher.frames[-1][1] == "finished" and publisher.closed


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_publisher_closed_on_interruption_or_error(harness, monkeypatch, error):
    _, publisher = harness

    def wait(_):
        raise error("interrupted")

    monkeypatch.setattr(cli.time, "sleep", wait)
    with pytest.raises(error):
        cli.main(steps=3, step_delay=0, probe_llm=False)
    assert publisher.closed and publisher.flushes == 1


@pytest.mark.parametrize("start_server", [False, True])
@pytest.mark.parametrize("steps", [0, 3])
def test_normal_run_preserves_step_budget_and_delay(harness, monkeypatch, start_server, steps):
    engine, publisher = harness
    engine.step_calls = 1  # Skip the stub's first-call model failure.
    delays = []
    monkeypatch.setattr(cli.time, "sleep", delays.append)
    result = cli.main(steps=steps, start_server=start_server, step_delay=0.05, probe_llm=False)
    assert result["steps"] == steps
    assert len(publisher.frames) == steps + start_server
    assert delays == [0.05] * (steps + start_server)
    if start_server:
        assert engine.runtime_status == "finished"
        assert publisher.frames[-1][1] == "finished"
    assert publisher.closed


def test_hold_server_still_holds_after_normal_completion(harness, monkeypatch):
    engine, publisher = harness
    engine.step_calls = 1

    def wait(delay):
        assert delay == 1 and publisher.closed
        assert engine.clock.time == 2
        assert engine.runtime_status == "finished"
        assert publisher.frames[-1][1] == "finished"
        assert not engine.vessel_mutation_allowed
        # Exercise the actual API write gates against the completed stub run.
        from src.vis.backend.server import _vessel_write_error, _writes_blocked

        app = SimpleNamespace(state=SimpleNamespace(replay_mode=False, engine=engine))
        response = _vessel_write_error(app, engine.episode_id)
        assert response.status_code == 409
        assert b"mutation_closed" in response.body
        assert _writes_blocked(app, SimpleNamespace(replay_mode=False, runtime_status=engine.runtime_status))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", wait)
    assert cli.main(steps=2, hold_server=True, step_delay=0, probe_llm=False)["steps"] == 2


def test_commands_queued_during_last_frame_are_rejected_on_completion(harness):
    engine, publisher = harness
    engine.step_calls = 1
    original_push = publisher.push_snapshot

    def push(current, result, total):
        original_push(current, result, total)
        if current.runtime_status == "running":
            for kind in engine.pending:
                engine.pending[kind].append("late_command")

    publisher.push_snapshot = push
    cli.main(steps=1, step_delay=0, probe_llm=False)
    assert engine.runtime_status == "finished"
    assert len(engine.rejected) == 3
    assert not any(engine.pending.values())
    assert engine.clock.time == 1 and publisher.frames[-1][1] == "finished"


@pytest.mark.parametrize("raises", [False, True])
def test_publisher_closed_even_when_flush_fails(harness, raises):
    _, publisher = harness

    def flush(timeout):
        if raises:
            raise RuntimeError("writer failed")
        return False

    publisher.flush = flush
    with pytest.raises(RuntimeError, match="writer failed|flushing replay frames"):
        cli.main(steps=0, step_delay=0, probe_llm=False)
    assert publisher.closed


@pytest.mark.parametrize("occupied", [False, True])
def test_port_check_never_kills_processes(monkeypatch, occupied):
    import os
    import subprocess

    calls = []

    class SocketStub:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def bind(self, address):
            calls.append(address)
            if occupied:
                raise OSError("address already in use")

    def forbidden(*args, **kwargs):
        pytest.fail("port check must not kill processes or invoke subprocesses")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli.socket, "socket", lambda *args: SocketStub())
    if occupied:
        with pytest.raises(RuntimeError, match="port 8765 is unavailable"):
            cli._check_port_available(8765)
    else:
        cli._check_port_available(8765)
    assert calls == [("0.0.0.0", 8765), "closed"]


def test_compatibility_wrapper_keeps_cli_options():
    from scripts import run_simulation

    assert run_simulation.main is cli.main
    args = run_simulation._parser().parse_args([
        "--config", "configs", "--steps", "7", "--port", "8888",
        "--no-server", "--hold-server", "--step-delay", "0",
        "--skip-llm-probe", "--llm-probe-timeout", "1",
        "--memory-version", "baseline", "--memory-root", "memory",
    ])
    assert (args.steps, args.port, args.step_delay) == (7, 8888, 0)
    assert args.no_server and args.hold_server and args.skip_llm_probe


def test_wall_budget_finishes_step_without_starting_another(monkeypatch):
    engine = StubEngine(tick_before_pause=True)
    clock = [0.0]
    monkeypatch.setattr(cli.time, 'monotonic', lambda: clock[0])
    published = []
    def publish(current, result):
        published.append(current.clock.time)
        current.runtime_status = 'running'
        clock[0] += 4.0
    result = cli._run_runtime_loop(engine, 100, publish, start_server=True, wall_seconds=3.)
    assert engine.step_calls == 1
    assert published == [1]
    assert result['steps'] == 1


def test_wall_budget_exits_model_pause_without_fake_steps(monkeypatch):
    engine = StubEngine()
    clock = [0.0]
    monkeypatch.setattr(cli.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    published = []
    cli._run_runtime_loop(engine, 100, lambda *args: published.append(True), start_server=True, wall_seconds=.3)
    assert engine.runtime_status == 'paused_model'
    assert engine.clock.time == 0
    assert engine.step_calls == 1
    assert len(published) == 1


@pytest.mark.parametrize('seconds', [0, -1, float('inf'), float('nan')])
def test_wall_budget_rejects_invalid_duration(seconds):
    with pytest.raises(ValueError, match='wall_seconds'):
        cli._run_runtime_loop(StubEngine(), 1, lambda *args: None, start_server=True, wall_seconds=seconds)


def test_wall_budget_closes_paused_live_session_as_readonly(harness, monkeypatch):
    engine, publisher = harness
    clock = [0.0]
    monkeypatch.setattr(cli.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    result = cli.main(steps=100, step_delay=0, probe_llm=False, wall_seconds=.3)
    assert engine.runtime_status == 'finished'
    assert result['runtime_before_finalize'] == 'paused_model'
    assert result['steps'] == 0
    assert publisher.frames[-1][1] == 'finished'
    assert publisher.closed


def test_run_report_preserves_pause_and_filters_request_payloads(harness, monkeypatch, tmp_path):
    import json
    engine, publisher = harness
    clock = [0.0]
    monkeypatch.setattr(cli.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    engine.allocator.llm_client = SimpleNamespace(gateway=SimpleNamespace(
        call_log=[{'role': 'decision_maker', 'success': False, 'failure_category': 'timeout',
                   'messages': [{'content': 'not report data'}]}],
        redact_log=lambda calls: calls,
    ))
    engine.allocator.sm.get_recent_events = lambda _: [{'type': 'mission_model_failure'}]
    report_dir = tmp_path / 'run'
    cli.main(steps=100, step_delay=0, probe_llm=False, wall_seconds=.3,
             run_report_dir=str(report_dir))
    report = json.loads((report_dir / 'report.json').read_text())
    assert report['entrypoint'] == 'main.py'
    assert report['summary']['runtime_before_finalize'] == 'paused_model'
    assert report['summary']['wall_seconds'] >= .3
    assert report['model_calls'][0]['failure_category'] == 'timeout'
    assert 'messages' not in report['model_calls'][0]
    with pytest.raises(FileExistsError):
        cli.main(probe_llm=False, run_report_dir=str(report_dir))


@pytest.mark.parametrize("budget", [None, 0.3])
@pytest.mark.parametrize("pause_during_step", [False, True])
def test_safety_pause_keeps_live_commands_responsive(monkeypatch, budget, pause_during_step):
    engine = StubEngine()
    if pause_during_step:
        original_step = engine.step

        def safety_step(**kwargs):
            result = original_step(**kwargs)
            engine.runtime_status = "paused_safety"
            return result

        engine.step = safety_step
    else:
        engine.runtime_status = "paused_safety"
    clock = [0.0]
    waits = []
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        assert engine.clock.time == 0
        assert engine.step_calls == int(pause_during_step)
        if budget is None:
            engine.pending["runtime"].append("abort")

    monkeypatch.setattr(cli.time, "sleep", wait)
    cli._run_runtime_loop(
        engine, 100, lambda *args: None, start_server=True, wall_seconds=budget,
    )
    assert waits
    assert engine.runtime_status == ("finished" if budget is None else "paused_safety")


def test_command_boundary_does_not_sleep_through_step_delay(harness, monkeypatch):
    engine, publisher = harness
    delays = []
    monkeypatch.setattr(cli.time, "sleep", delays.append)

    def step(*, on_command_boundary=None):
        assert on_command_boundary is not None
        on_command_boundary(engine, {"trigger_type": "none", "action": None, "command_boundary": True})
        assert len(publisher.frames) == 1
        assert delays == [], "command snapshots must not incur the simulation pacing delay"
        engine.clock.time = 1
        engine.allocator.sm.current_time = 1
        return {"trigger_type": "none", "action": None}

    engine.step = step
    cli.main(steps=1, step_delay=0.5, probe_llm=False)
    assert delays == [0.5, 0.5]


def test_paused_command_boundary_refreshes_inventory_before_publish(monkeypatch):
    from scripts.evaluate_mixed_maritime import _FixtureGateway
    from src.mission.contracts import VesselCommand
    from src.schedule.config_loader import ConfigLoader
    from src.vis.backend.frame_builder import build_frame

    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=_FixtureGateway())
    engine._set_runtime_state("paused_model")
    engine.vessel_commands.enqueue(VesselCommand(
        "paused-vessel", engine.episode_id, "create",
        vessel_class="type_ii", position_cells=(12.5, 8.5),
    ))
    frames = []

    def publish(current, result):
        frames.append(build_frame(current.allocator.sm, 0, current.config))
        current._set_runtime_state("finished")

    monkeypatch.setattr(cli.time, "sleep", lambda _: pytest.fail("unnecessary delay"))
    cli._run_runtime_loop(engine, 1, publish, start_server=True)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["sim_time_min"] == 0
    assert frame["actual_vessel_count"] == len(engine.ships)
    assert any(v["scenario_entity_id"] == engine.vessel_commands.get("paused-vessel").vessel_id
               for v in frame["scenario_vessels"])


def test_running_abort_does_not_republish_previous_heavy_decision():
    from scripts.evaluate_mixed_maritime import _FixtureGateway
    from src.mission.contracts import RuntimeCommand
    from src.schedule.config_loader import ConfigLoader

    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=_FixtureGateway())
    engine.last_result = {"trigger_type": "heavy", "llm_cycle": {"stale": True}}
    engine.runtime_commands.enqueue(RuntimeCommand("abort-boundary", engine.episode_id, "abort"))
    results = []
    cli._run_runtime_loop(engine, 1, lambda _, result: results.append(result), start_server=True)
    assert results
    assert all(result["trigger_type"] == "none" and "llm_cycle" not in result for result in results)
    assert engine.clock.time == 0
    assert engine.runtime_status == "finished"

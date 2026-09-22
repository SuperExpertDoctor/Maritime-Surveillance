import json
import threading
from concurrent.futures import Future
from types import SimpleNamespace

from src.control.common.contracts import ControlRouteSnapshot, UavRouteSnapshot
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_builder import build_frame
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend import frame_publisher as frame_publisher_module
from src.vis.backend.frame_publisher import FramePublisher


def _engine_with_time(config, state):
    return SimpleNamespace(
        allocator=SimpleNamespace(sm=state),
        config=config,
        ships=[],
        uavs=[],
        obstacles=[],
        bases=[],
    )


def test_frame_publisher_persists_every_immutable_step_snapshot(tmp_path):
    config = ConfigLoader.load()
    state = StateManager(config)
    publisher = FramePublisher(FrameLogger(str(tmp_path)))
    engine = _engine_with_time(config, state)

    for step in (1, 2, 3):
        state.current_time = float(step)
        publisher.push_snapshot(engine, {}, total_steps=3)

    assert publisher.flush(timeout=5)
    publisher.close()
    lines = publisher.logger.path
    frames = [json.loads(line) for line in open(lines, encoding="utf-8")]

    assert publisher.record_count == 3
    assert [frame["frame_id"] for frame in frames] == [1, 2, 3]


def test_compact_live_frame_omits_matrices_without_changing_replay_shape():
    config = ConfigLoader.load()
    state = StateManager(config)

    compact = build_frame(state, 0, config, realtime=True, include_matrices=False)
    replay = build_frame(state, 0, config)

    assert "info_matrix" not in compact
    assert "value_matrix" not in compact
    assert len(replay["info_matrix"]) == config.grid.resolution[0]
    assert len(replay["value_matrix"]) == config.grid.resolution[0]


def test_publishing_a_frame_while_a_coverage_route_exists(tmp_path):
    """A recorded coverage route must not break frame publication.

    Regression: the snapshot deep-copy raised
    ``TypeError: cannot pickle 'mappingproxy' object`` on the first frame after
    a coverage route was recorded, killing the whole simulation process.
    """
    config = ConfigLoader.load()
    state = StateManager(config)
    state.episode_id = "episode-1"
    state.update_uav_control(
        "UAV-2", "heuristic", "heuristic", "coverage", 1, False,
    )
    route = ControlRouteSnapshot(
        task_id="task-1",
        task_type="coverage",
        phase="coverage",
        target_contact_id=None,
        route=((1.0, 2.0, 0.0), (3.0, 4.0, 0.0)),
        next_index=0,
        route_revision=1,
        planning_map_version=1,
        status="ready",
        coverage_progress={"phase": "coverage", "progress_cells": 12.0},
    )
    state.set_control_route("UAV-2", UavRouteSnapshot("episode-1", 1, route))
    publisher = FramePublisher(FrameLogger(str(tmp_path)))
    engine = _engine_with_time(config, state)

    publisher.push_snapshot(engine, {}, total_steps=1)

    assert publisher.flush(timeout=5)
    publisher.close()
    assert publisher.record_count == 1


class _FailingFrameLogger:
    def write(self, _frame):
        raise OSError("disk full")


class _BlockingFrameLogger:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.frames = []

    def write(self, frame):
        self.started.set()
        assert self.release.wait(timeout=5)
        self.frames.append(frame)


def test_recording_exception_sets_failure_and_does_not_report_flush_success():
    config = ConfigLoader.load()
    state = StateManager(config)
    publisher = FramePublisher(_FailingFrameLogger())
    engine = _engine_with_time(config, state)

    publisher.push_snapshot(engine, {}, total_steps=1)

    assert publisher.flush(timeout=2) is False
    assert isinstance(publisher.record_error, OSError)
    publisher.close()


def test_flush_waits_for_queue_join_even_when_queue_empty_race_occurs():
    config = ConfigLoader.load()
    state = StateManager(config)
    logger = _BlockingFrameLogger()
    publisher = FramePublisher(logger)
    engine = _engine_with_time(config, state)
    publisher.push_snapshot(engine, {}, total_steps=1)
    assert logger.started.wait(timeout=2)

    result = {}
    waiter = threading.Thread(
        target=lambda: result.setdefault("flushed", publisher.flush(timeout=2)),
    )
    waiter.start()
    assert waiter.is_alive()
    logger.release.set()
    waiter.join(timeout=3)

    assert result == {"flushed": True}
    assert publisher.record_count == 1
    publisher.close()


def test_final_live_frame_is_broadcast_after_previous_future_finishes(monkeypatch):
    config = ConfigLoader.load()
    state = StateManager(config)
    engine = _engine_with_time(config, state)
    app = object()
    first_future = Future()
    broadcasted = []

    def fake_build(snapshot, *, realtime, include_matrices):
        if realtime:
            return {"sim_time_min": snapshot.state.current_time}
        return {"realtime": False}

    def fake_broadcast(_app, frame):
        broadcasted.append(frame)
        if len(broadcasted) == 1:
            return first_future
        return None

    monkeypatch.setattr(frame_publisher_module, "_build", fake_build)
    monkeypatch.setattr(frame_publisher_module, "broadcast_payload_sync", fake_broadcast)
    publisher = FramePublisher(_FailingFrameLogger(), app=app)

    state.current_time = 1.0
    publisher.push_snapshot(engine, {}, total_steps=2)
    assert _wait_until(lambda: len(broadcasted) == 1)
    state.current_time = 2.0
    publisher.push_snapshot(engine, {}, total_steps=2)
    assert len(broadcasted) == 1

    first_future.set_result(None)
    assert publisher.wait_live_idle(timeout=2)
    assert [frame["sim_time_min"] for frame in broadcasted] == [1.0, 2.0]
    publisher.close()


def test_broadcast_future_exception_is_observed_and_logged(monkeypatch):
    config = ConfigLoader.load()
    state = StateManager(config)
    engine = _engine_with_time(config, state)
    failed_future = Future()
    logged = []

    monkeypatch.setattr(
        frame_publisher_module,
        "_build",
        lambda snapshot, *, realtime, include_matrices: {"sim_time_min": snapshot.state.current_time},
    )
    monkeypatch.setattr(
        frame_publisher_module,
        "broadcast_payload_sync",
        lambda _app, _frame: failed_future,
    )
    monkeypatch.setattr(
        frame_publisher_module._LOGGER,
        "exception",
        lambda *args, **_kwargs: logged.append(args),
    )
    publisher = FramePublisher(_FailingFrameLogger(), app=object())

    publisher.push_snapshot(engine, {}, total_steps=1)
    assert _wait_until(lambda: publisher._broadcast_future is failed_future)
    failed_future.set_exception(RuntimeError("socket closed"))
    assert publisher.wait_live_idle(timeout=2) is False

    assert isinstance(publisher.live_error, RuntimeError)
    assert any("broadcast future failed" in args[0] for args in logged)
    publisher.close()


def test_matrices_use_sim_time_threshold_crossing_not_float_modulo(monkeypatch):
    config = ConfigLoader.load()
    state = StateManager(config)
    engine = _engine_with_time(config, state)
    built = []

    monkeypatch.setattr(
        frame_publisher_module,
        "_build",
        lambda snapshot, *, realtime, include_matrices: (
            built.append((realtime, include_matrices, snapshot.state.current_time))
            or {"sim_time_min": snapshot.state.current_time}
        ),
    )
    monkeypatch.setattr(
        frame_publisher_module,
        "broadcast_payload_sync",
        lambda _app, _frame: None,
    )
    publisher = FramePublisher(_FailingFrameLogger(), app=object())

    for current_time in (0.1, 4.9, 5.1, 10.1):
        state.current_time = current_time
        publisher.push_snapshot(engine, {}, total_steps=4)
        assert publisher.wait_live_idle(timeout=2)

    assert [item[1] for item in built if item[0]] == [True, False, True, True]
    publisher.close()


def test_llm_cycle_is_present_only_on_decision_frames(monkeypatch):
    config = ConfigLoader.load()
    state = StateManager(config)
    engine = _engine_with_time(config, state)
    frames = []

    monkeypatch.setattr(
        frame_publisher_module,
        "_build",
        lambda snapshot, *, realtime, include_matrices: {"llm_cycle": snapshot.llm_cycle},
    )

    class _Sink:
        def write(self, frame):
            frames.append(frame)

    publisher = FramePublisher(_Sink())
    publisher.push_snapshot(
        engine,
        {"trigger_type": "light", "llm_cycle": {"stale": True}},
        total_steps=2,
    )
    publisher.push_snapshot(
        engine,
        {"trigger_type": "heavy", "llm_cycle": {"success": True}},
        total_steps=2,
    )
    assert publisher.flush(timeout=2)
    publisher.close()

    assert [frame["llm_cycle"] for frame in frames] == [None, {"success": True}]


def _wait_until(predicate, timeout=2):
    deadline = __import__("time").monotonic() + timeout
    while __import__("time").monotonic() < deadline:
        if predicate():
            return True
        __import__("time").sleep(0.01)
    return predicate()


def test_publication_keeps_commands_applied_after_previous_frame_boundary(tmp_path):
    config = ConfigLoader.load()
    state = StateManager(config)
    state.episode_id = 'boundary'
    state.current_time = 1.
    publisher = FramePublisher(FrameLogger(str(tmp_path)))
    engine = _engine_with_time(config, state)
    publisher.push_snapshot(engine, {}, total_steps=10)
    # The next step drains UI commands before advancing its clock.
    state.add_event('vessel_created', {'vessel_id': 'operator-ship'})
    state.current_time = 2.
    publisher.push_snapshot(engine, {}, total_steps=10)
    # A slow client can miss the second frame; a later frame must retain it.
    state.current_time = 10.
    publisher.push_snapshot(engine, {}, total_steps=10)
    assert publisher.flush(timeout=5)
    publisher.close()
    frames = [json.loads(line) for line in open(publisher.logger.path)]
    assert not frames[0]['events']
    assert [e['type'] for e in frames[1]['events']] == ['vessel_created']
    assert frames[2]['events'] == frames[1]['events']

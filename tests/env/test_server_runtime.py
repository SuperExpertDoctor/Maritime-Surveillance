import builtins

from fastapi.testclient import TestClient

from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend.server import broadcast_frame_sync, create_app


class _FrameSink:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)


def test_sync_broadcast_uses_the_running_server_event_loop():
    config = ConfigLoader.load()
    state = StateManager(config)
    app = create_app(config, state)
    sink = _FrameSink()
    app.state.frame_logger = sink

    with TestClient(app):
        assert app.state.event_loop is not None
        assert app.state.event_loop.is_running()
        future = broadcast_frame_sync(app)
        assert future is not None
        future.result(timeout=5)

    assert len(sink.frames) == 1
    assert sink.frames[0]["frame_id"] == 0


def test_frame_logger_retries_a_transient_windows_sharing_violation(tmp_path, monkeypatch):
    logger = FrameLogger(str(tmp_path))
    real_open = builtins.open
    attempts = 0

    def flaky_open(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("file is temporarily locked")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)
    monkeypatch.setattr("src.vis.backend.frame_logger.time.sleep", lambda _seconds: None)

    logger.write({"frame_id": 1})

    assert attempts == 3
    assert logger.count == 1

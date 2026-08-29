import builtins
import asyncio
import json

from fastapi.testclient import TestClient

from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend import server
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


def test_api_config_exposes_control_strategy_contract():
    config = ConfigLoader.load()
    app = create_app(config, StateManager(config))
    route = next(
        item for item in app.routes
        if getattr(item, "path", None) == "/api/config"
    )
    response = asyncio.run(route.endpoint())
    payload = json.loads(response.body)

    assert payload["control"]["default_mode"] == "heuristic"
    assert payload["control"]["per_uav"] == {}
    assert payload["control"]["observation"]["schema_version"] == "control-observation/v1"
    assert payload["control"]["safety"]["max_invalid_commands"] == 3
    assert "heuristic" in payload["control"]


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


def test_replay_total_refreshes_while_a_live_jsonl_file_is_growing(tmp_path, monkeypatch):
    replay = tmp_path / "simulation_live.jsonl"
    replay.write_text('{"frame_id": 1}\n', encoding="utf-8")
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    app = create_app(ConfigLoader.load(), StateManager(ConfigLoader.load()))

    with TestClient(app) as client:
        first = client.get("/api/replay", params={"file": replay.name}).json()
        replay.write_text(
            '{"frame_id": 1}\n{"frame_id": 2}\n', encoding="utf-8",
        )
        second = client.get("/api/replay", params={"file": replay.name}).json()

    assert first["total"] == 1
    assert second["total"] == 2


def test_mp4_export_reports_encoder_availability(monkeypatch):
    monkeypatch.setattr(server, "_find_ffmpeg", lambda: None)
    app = create_app(ConfigLoader.load(), StateManager(ConfigLoader.load()))

    with TestClient(app) as client:
        response = client.get("/api/export/capabilities")

    assert response.json() == {"mp4": False}


def test_mp4_export_transcodes_browser_recording_and_returns_download(tmp_path, monkeypatch):
    encoded = tmp_path / "encoded.mp4"
    encoded.write_bytes(b"mp4")
    monkeypatch.setattr(server, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(server, "_transcode_webm_to_mp4", lambda _payload: (tmp_path, encoded))
    monkeypatch.setattr(server.shutil, "rmtree", lambda *_args, **_kwargs: None)
    app = create_app(ConfigLoader.load(), StateManager(ConfigLoader.load()))

    with TestClient(app) as client:
        response = client.post("/api/export/mp4", content=b"webm", headers={"content-type": "video/webm"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"].endswith('filename="uav-mission-replay.mp4"')
    assert response.content == b"mp4"

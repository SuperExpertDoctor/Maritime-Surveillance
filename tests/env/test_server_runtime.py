import builtins
import asyncio
import json

from fastapi.testclient import TestClient

from src.env.simulation import SimulationEngine
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend import server
from src.vis.backend.server import broadcast_frame_sync, create_app
from src.vis.backend.frame_builder import build_frame


class _FrameSink:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)


class _OfflineGateway:
    def request_json(self, **_kwargs):
        return ModelResult("offline", False, None, ("offline",), "transport")

    def request_text(self, **_kwargs):
        return ModelResult("offline-text", False, None, ("offline",), "transport")


def _runtime_app(*, replay_mode=False):
    config = ConfigLoader.load()
    engine = SimulationEngine(
        config,
        seed=42,
        llm_gateway=_OfflineGateway(),
        episode_id="vessel-api-episode",
    )
    return create_app(config, engine.allocator.sm, engine=engine, replay_mode=replay_mode), engine


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


def test_initial_websocket_frame_includes_live_engine_entities():
    config = ConfigLoader.load()
    engine = SimulationEngine(config, seed=42, llm_gateway=_OfflineGateway())
    app = create_app(config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as websocket:
            frame = websocket.receive_json()

    assert len(frame["uavs"]) == len(engine.uavs)
    assert len(frame["obstacles"]) == len(engine.obstacles)
    assert len(frame["bases"]) == len(engine.bases)
    assert len(frame["scenario_vessels"]) == len(engine.ships)


def test_live_frame_always_contains_runtime_vessel_inventory():
    app, engine = _runtime_app()
    engine.step()
    frame = build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )

    assert frame["vessel_mutation_allowed"] is True
    assert {item["vessel_class"] for item in frame["scenario_vessels"]} <= {
        "type_i", "type_ii",
    }
    assert all(type(item["ais_enabled"]) is bool for item in frame["scenario_vessels"])
    assert all(
        {"revision", "ais_controllable", "surveillance_stage"} <= set(item)
        for item in frame["scenario_vessels"]
    )


def test_runtime_vessel_ais_patch_accepts_only_strict_boolean_payload():
    app, engine = _runtime_app()

    with TestClient(app) as client:
        invalid = client.patch(
            "/api/vessels/Ship-1/ais",
            json={
                "episode_id": engine.episode_id,
                "command_id": "ais-invalid",
                "expected_revision": 1,
                "ais_enabled": 1,
            },
        )
        valid = client.patch(
            "/api/vessels/Ship-1/ais",
            json={
                "episode_id": engine.episode_id,
                "command_id": "ais-valid",
                "expected_revision": 1,
                "ais_enabled": False,
            },
        )

    assert invalid.status_code == 422
    assert valid.status_code == 202
    assert valid.json()["status"] == "queued"


def test_vessel_mutation_stays_open_after_first_step():
    app, engine = _runtime_app()
    engine.step()
    assert engine.vessel_mutation_allowed is True


def test_vessel_mutation_reports_replay_and_finished_states():
    replay_app, replay_engine = _runtime_app(replay_mode=True)
    finished_app, finished_engine = _runtime_app()
    finished_engine._set_runtime_state("finished")
    body = {
        "episode_id": replay_engine.episode_id,
        "command_id": "ais-replay",
        "expected_revision": 1,
        "ais_enabled": False,
    }

    with TestClient(replay_app) as client:
        replay = client.patch("/api/vessels/Ship-1/ais", json=body)
    body["episode_id"] = finished_engine.episode_id
    body["command_id"] = "ais-finished"
    with TestClient(finished_app) as client:
        finished = client.patch("/api/vessels/Ship-1/ais", json=body)

    assert replay.status_code == 409
    assert replay.json()["error_code"] == "replay_read_only"
    assert finished.status_code == 409
    assert finished.json()["error_code"] == "mutation_closed"


def test_applied_intent_is_present_in_the_next_websocket_frame():
    config = ConfigLoader.load()
    engine = SimulationEngine(
        config,
        seed=42,
        llm_gateway=_OfflineGateway(),
        episode_id="websocket-intent-flow",
    )
    app = create_app(config, engine.allocator.sm, engine=engine)
    app.state.frame_logger = _FrameSink()
    payload = {
        "episode_id": engine.episode_id,
        "command_id": "websocket-intent-1",
        "label": "websocket flow",
        "bbox": [6, 6, 12, 12],
        "mode": "search_priority",
        "priority": "high",
        "weight": 1.0,
        "valid_duration_min": 20.0,
        "revisit_interval_min": None,
    }

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as websocket:
            websocket.receive_json()
            response = client.post("/api/intents", json=payload)
            assert response.status_code == 202
            engine.apply_pending_intent_commands()
            future = broadcast_frame_sync(app)
            assert future is not None
            future.result(timeout=5)
            frame = websocket.receive_json()

    assert [intent["label"] for intent in frame["intents"]] == ["websocket flow"]
    assert frame["intent_statuses"][0]["intent_id"] == frame["intents"][0]["intent_id"]


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
    assert payload["control"]["observation"]["schema_version"] == "control-observation/v2"
    assert payload["control"]["safety"]["max_invalid_commands"] == 3
    assert "heuristic" in payload["control"]
    assert payload["uav"]["count"] == config.uav.count
    assert "count_max" not in payload["uav"]
    assert payload["ship"]["population"]["total_count"] == config.ship.population.total_count
    assert "initial_ship_count" not in payload["ship"]
    assert "target_ship_count" not in payload["ship"]
    assert payload["sensor"]["passive"]["detection_range_cells"] == 10.0
    assert payload["mission_alignment"]["information_update"]["value_alpha"] == 0.45


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


def test_replay_endpoint_normalizes_legacy_vessel_inventory(tmp_path, monkeypatch):
    replay = tmp_path / "legacy.jsonl"
    replay.write_text(
        json.dumps({
            "frame_id": 1,
            "scenario_vessels": [{"vessel_class": "research", "ais_mode": "silent"}],
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    app = create_app(ConfigLoader.load(), StateManager(ConfigLoader.load()))

    with TestClient(app) as client:
        frame = client.get("/api/replay", params={"file": replay.name}).json()["frames"][0]

    vessel = frame["scenario_vessels"][0]
    assert vessel["vessel_class"] == "type_ii"
    assert vessel["ais_enabled"] is False
    assert vessel["surveillance_stage"] == "undetected"


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

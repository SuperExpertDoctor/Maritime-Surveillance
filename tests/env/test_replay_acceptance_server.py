import socket

import pytest
from fastapi.testclient import TestClient

from scripts.run_replay_acceptance_server import (
    _require_free_port,
    _require_replay_directory,
    create_replay_app,
)
from src.vis.backend import server


def test_acceptance_app_serves_only_the_requested_replay_root(tmp_path, monkeypatch):
    replay = tmp_path / "v07-seed42-frames.jsonl"
    replay.write_text('{"frame_id": 7, "episode_id": "episode-7"}\n', encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "outside.jsonl"
    outside.write_text('{"frame_id": 99}\n', encoding="utf-8")
    monkeypatch.setattr(server, "OUTPUT_DIR", "outputs")

    app = create_replay_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/replay/list").json() == {"files": [replay.name]}
        response = client.get("/api/replay", params={"file": replay.name})

    assert response.status_code == 200
    assert response.json()["frames"][0]["episode_id"] == "episode-7"
    assert outside.name not in response.json().get("files", [])


def test_acceptance_server_rejects_missing_or_empty_replay_root(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        _require_replay_directory(str(tmp_path / "missing"))

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no JSONL artifacts"):
        _require_replay_directory(str(empty))


def test_acceptance_server_reports_port_conflict_without_killing_processes():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            _require_free_port(listener.getsockname()[1])
    finally:
        listener.close()

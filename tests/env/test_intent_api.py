from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from src.env.simulation import SimulationEngine
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.vis.backend.server import create_app


class OfflineGateway:
    def request_json(self, **_kwargs):
        return ModelResult("offline", False, None, ("offline",), "transport")

    def request_text(self, **_kwargs):
        return ModelResult("offline-text", False, None, ("offline",), "transport")


def _engine(*, queue_limit: int | None = None) -> SimulationEngine:
    config = ConfigLoader.load()
    if queue_limit is not None:
        intent = replace(config.mission.intent, mutation_queue_limit=queue_limit)
        mission = replace(config.mission, intent=intent)
        config = replace(config, mission=mission)
    return SimulationEngine(config, seed=42, llm_gateway=OfflineGateway())


def _payload(engine: SimulationEngine, command_id: str = "cmd-1", **changes):
    payload = {
        "episode_id": engine.episode_id,
        "command_id": command_id,
        "label": "  harbor watch  ",
        "bbox": [6, 6, 12, 12],
        "mode": "search_priority",
        "priority": "high",
        "weight": 1.0,
        "valid_duration_min": 20.0,
        "revisit_interval_min": None,
    }
    payload.update(changes)
    return payload


def test_http_request_is_queued_before_simulation_thread_applies_it():
    engine = _engine()
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        response = client.post("/api/intents", json=_payload(engine))
        assert response.status_code == 202
        assert response.json() == {
            "command_id": "cmd-1",
            "status": "queued",
        }
        assert engine.intents.active() == ()

        engine.apply_pending_intent_commands()
        intents = client.get("/api/intents").json()
        assert len(intents["intents"]) == 1
        assert intents["intents"][0]["label"] == "harbor watch"
        assert client.get("/api/intent-commands/cmd-1").json()["status"] == "applied"


def test_intent_commands_are_idempotent_and_conflicts_are_explicit():
    engine = _engine()
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        assert client.post("/api/intents", json=_payload(engine)).status_code == 202
        assert client.post("/api/intents", json=_payload(engine)).status_code == 202
        conflict = client.post(
            "/api/intents",
            json=_payload(engine, label="another focus"),
        )
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "command_conflict"


def test_revision_conflict_is_reported_when_the_queued_command_is_applied():
    engine = _engine()
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        assert client.post("/api/intents", json=_payload(engine)).status_code == 202
        engine.apply_pending_intent_commands()
        intent_id = engine.intents.active()[0].intent_id
        patch = client.patch(
            f"/api/intents/{intent_id}",
            json={
                "episode_id": engine.episode_id,
                "command_id": "cmd-2",
                "expected_revision": 1,
                "priority": "low",
            },
        )
        competing = client.patch(
            f"/api/intents/{intent_id}",
            json={
                "episode_id": engine.episode_id,
                "command_id": "cmd-3",
                "expected_revision": 1,
                "weight": 1.5,
            },
        )
        assert patch.status_code == 202
        assert competing.status_code == 202
        engine.apply_pending_intent_commands()
        results = {
            command_id: client.get(f"/api/intent-commands/{command_id}").json()
            for command_id in ("cmd-2", "cmd-3")
        }
        assert sorted(result["status"] for result in results.values()) == [
            "applied", "rejected",
        ]
        rejected = next(result for result in results.values() if result["status"] == "rejected")
        assert rejected["error_code"] == "revision_conflict"


def test_api_rejects_old_episode_and_malformed_requests():
    engine = _engine()
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        old_episode = client.post(
            "/api/intents",
            json=_payload(engine, episode_id="old-episode"),
        )
        assert old_episode.status_code == 409
        assert old_episode.json()["error_code"] == "episode_conflict"

        malformed = client.post(
            "/api/intents",
            json=_payload(engine, label=""),
        )
        assert malformed.status_code == 422
        assert malformed.json()["error_code"] == "invalid_request"


def test_full_queue_returns_429_and_replay_is_read_only():
    engine = _engine(queue_limit=1)
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        assert client.post("/api/intents", json=_payload(engine)).status_code == 202
        full = client.post(
            "/api/intents",
            json=_payload(engine, command_id="cmd-2"),
        )
        assert full.status_code == 429

    replay_app = create_app(
        engine.config,
        engine.allocator.sm,
        engine=engine,
        replay_mode=True,
    )
    with TestClient(replay_app) as client:
        response = client.post("/api/intents", json=_payload(engine))
    assert response.status_code == 409
    assert response.json()["error_code"] == "replay_read_only"


def test_runtime_retry_is_queued_for_a_paused_model_and_abort_finishes_episode():
    engine = _engine()
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    engine._runtime_status = "paused_model"
    with TestClient(app) as client:
        retry = client.post(
            "/api/runtime/retry",
            json={"episode_id": engine.episode_id, "command_id": "retry-1"},
        )
        assert retry.status_code == 202
        assert engine.clock.time == 0.0
        engine.apply_pending_runtime_commands()
        retry_result = client.get("/api/intent-commands/retry-1").json()
        assert retry_result["status"] == "applied"
        assert retry_result["error_code"] is None

        engine._runtime_status = "running"
        abort = client.post(
            "/api/runtime/abort",
            json={"episode_id": engine.episode_id, "command_id": "abort-1"},
        )
        assert abort.status_code == 202
        engine.apply_pending_runtime_commands()
        assert engine.runtime_status == "finished"
        assert client.get("/api/intent-commands/abort-1").json()["status"] == "applied"
        assert client.post("/api/intents", json=_payload(engine, command_id="late")).status_code == 409


def test_retry_is_rejected_when_the_model_is_not_paused():
    engine = _engine()
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/retry",
            json={"episode_id": engine.episode_id, "command_id": "retry-1"},
        )
    assert response.status_code == 409
    assert response.json()["error_code"] == "runtime_not_paused"


def test_reset_rejects_pending_commands_from_the_previous_episode():
    engine = _engine()
    app = create_app(engine.config, engine.allocator.sm, engine=engine)

    with TestClient(app) as client:
        old_episode = engine.episode_id
        assert client.post("/api/intents", json=_payload(engine)).status_code == 202
        engine.reset(seed=43)
        result = client.get("/api/intent-commands/cmd-1").json()
        assert result["status"] == "rejected"
        assert result["error_code"] == "episode_reset"
        assert engine.episode_id != old_episode
        assert client.post(
            "/api/intents",
            json=_payload(engine, episode_id=old_episode, command_id="old-2"),
        ).status_code == 409


def test_bounded_red_retries_pause_until_manual_retry_and_resume_in_place():
    import json
    from src.mission.llm_gateway import LLMGateway

    class RecoveringTransport:
        calls = 0

        def complete(self, **kwargs):
            assert kwargs['role'] == 'red_commander'
            self.calls += 1
            if self.calls <= 6:
                raise TimeoutError('Request timed out.')
            snapshot = json.loads(kwargs['messages'][1]['content'])['snapshot']
            return json.dumps({
                'schema_version': 'red-plan/v1', 'snapshot_id': snapshot['snapshot_id'],
                'valid_for_min': 3.0, 'notes': '',
                'commands': [{
                    'ship_id': ship_id, 'heading_offset_deg': 12.0, 'speed_kn': 18.0,
                    'zigzag_heading_deg': 0.0, 'zigzag_period_min': 10.0, 'phase_deg': 37.0,
                } for ship_id, _stage in snapshot['active_signature']],
            })

    transport = RecoveringTransport()
    engine = SimulationEngine(ConfigLoader.load(), seed=42,
                              llm_gateway=LLMGateway(transport=transport))
    target = next(ship for ship in engine.ships if ship.vessel_class == 'type_ii')
    engine.surveillance_stages.set_fact(target.id, 'sar', True, 0.0, 'test-retry')
    episode = engine.episode_id
    positions = [ship.float_position for ship in engine.ships]
    app = create_app(engine.config, engine.allocator.sm, engine=engine)
    engine.step()
    assert transport.calls == 3
    assert engine.runtime_status == 'paused_model'
    for _ in range(5):
        engine.step()
    assert transport.calls == 3

    with TestClient(app) as client:
        for command_id, expected_status, expected_calls in [
            ('failed-retry', 'rejected', 6), ('successful-retry', 'applied', 7),
        ]:
            response = client.post('/api/runtime/retry', json={
                'episode_id': episode, 'command_id': command_id,
            })
            assert response.status_code == 202
            engine.apply_pending_runtime_commands()
            result = client.get(f'/api/intent-commands/{command_id}').json()
            assert result['status'] == expected_status
            assert transport.calls == expected_calls
            assert engine.clock.time == 0.0
            assert engine.episode_id == episode
            assert [ship.float_position for ship in engine.ships] == positions
            if expected_status == 'rejected':
                assert engine.runtime_status == 'paused_model'
                for _ in range(5):
                    engine.step()
                assert transport.calls == 6

    assert engine.runtime_status == 'running'
    assert engine.blocked_role is None
    assert target._navigation_params is not None
    assert engine.last_result['trigger_type'] == 'model_resumed'
    assert engine.last_result['action'] == 'red_decision_retry_succeeded'

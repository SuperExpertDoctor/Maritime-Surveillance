import math

from fastapi.testclient import TestClient

from scripts.evaluate_mixed_maritime import _FixtureGateway
from scripts.replay_restoration_scenarios import build_scenario
from src.env.simulation import SimulationEngine
from src.mission.contracts import IntentCommand, VesselCommand, VisualDetection
from src.schedule.config_loader import ConfigLoader
from src.vis.backend.server import create_app


def _payload(label="operator focus", *, bbox=(8, 8, 12, 12), duration=20.0):
    return {
        "label": label,
        "bbox": list(bbox),
        "mode": "search_priority",
        "priority": "high",
        "weight": 1.0,
        "valid_duration_min": duration,
        "revisit_interval_min": None,
    }


def test_intent_commands_apply_at_boundary_update_expire_and_publish_candidates():
    engine = build_scenario("intent-lifecycle", seed=42, transport="fixture")
    create = IntentCommand(
        "intent-create-integration",
        engine.episode_id,
        "create",
        None,
        None,
        _payload(),
    )
    assert engine.intent_commands.enqueue(create).status == "queued"
    assert not any(
        intent.label == "operator focus" for intent in engine.intents.active()
    )

    applied = engine.apply_pending_intent_commands()
    assert applied[0].status == "applied"
    intent = applied[0].intent
    assert intent is not None
    assert intent.revision == 1
    published = engine.published_intent_snapshot()
    assert any(item.intent_id == intent.intent_id for item in published["intents"])
    assert any(
        item.intent_id == intent.intent_id
        for item in published["statuses"]
    )

    snapshot = engine.allocator.build_mission_snapshot(
        0.0,
        intents=engine.intents.intents(),
        intent_statuses=engine._evaluate_intent_statuses(0.0),
    )
    intent_tasks = [
        candidate for candidate in snapshot.candidates
        if intent.intent_id in candidate.intent_ids
    ]
    assert intent_tasks
    assert all(candidate.feasible_uav_ids for candidate in intent_tasks)

    update = IntentCommand(
        "intent-update-integration",
        engine.episode_id,
        "update",
        intent.intent_id,
        1,
        {"label": "operator focus updated", "priority": "medium"},
    )
    engine.intent_commands.enqueue(update)
    updated = engine.apply_pending_intent_commands()[0]
    assert updated.status == "applied"
    assert updated.intent.revision == 2
    assert updated.intent.label == "operator focus updated"

    stale = IntentCommand(
        "intent-stale-update",
        engine.episode_id,
        "update",
        intent.intent_id,
        1,
        {"label": "must be rejected"},
    )
    engine.intent_commands.enqueue(stale)
    stale_result = engine.apply_pending_intent_commands()[0]
    assert stale_result.status == "rejected"
    assert stale_result.error_code == "revision_conflict"

    invalid = IntentCommand(
        "intent-invalid-bbox",
        engine.episode_id,
        "create",
        None,
        None,
        _payload("land", bbox=(1, 1, 2, 2)),
    )
    engine.intent_commands.enqueue(invalid)
    invalid_result = engine.apply_pending_intent_commands()[0]
    assert invalid_result.status == "rejected"
    assert invalid_result.error_code == "invalid_intent"

    cancel = IntentCommand(
        "intent-cancel-integration",
        engine.episode_id,
        "cancel",
        intent.intent_id,
        2,
        {},
    )
    engine.intent_commands.enqueue(cancel)
    cancelled = engine.apply_pending_intent_commands()[0]
    assert cancelled.status == "applied"
    assert cancelled.intent.revision == 3
    assert cancelled.intent.lifecycle == "cancelled"

    short = IntentCommand(
        "intent-expire-integration",
        engine.episode_id,
        "create",
        None,
        None,
        _payload("short", duration=1.0),
    )
    engine.intent_commands.enqueue(short)
    short_intent = engine.apply_pending_intent_commands()[0].intent
    engine.clock.tick()
    engine.apply_pending_intent_commands()
    expired = next(
        item for item in engine.intents.intents()
        if item.intent_id == short_intent.intent_id
    )
    assert expired.lifecycle == "expired"
    assert any(
        event["type"] == "intent_expired"
        and event["data"]["intent_id"] == short_intent.intent_id
        for event in engine.allocator.sm.get_recent_events(0.0)
    )

    contact_store = engine.allocator.sm.contacts
    protected_id = contact_store.ingest_visual(VisualDetection(
        "EO-PROTECTED-INTENT", 1.0, "eo", engine.uavs[0].id,
        (27.0, 27.0), (0.0, 0.0), 0.05, (27.0, 25.2), 1.8,
        "open_water",
    ))
    contact_store.reserve(protected_id, engine.uavs[0].id, "P-PROTECTED")
    engine.allocator.sm.update_uav_status(
        engine.uavs[0].id, "tracking", engine.allocator.sm.get_uav(
            engine.uavs[0].id
        ).position,
        target_group_id=protected_id,
    )
    protected_snapshot = engine.allocator.build_mission_snapshot(
        1.0,
        intents=engine.intents.intents(),
        intent_statuses=engine._evaluate_intent_statuses(1.0),
    )
    assert all(
        edge.uav_id != engine.uavs[0].id
        for edge in protected_snapshot.feasible_edges
        if any(
            candidate.task_id == edge.task_id and candidate.intent_ids
            for candidate in protected_snapshot.candidates
        )
    )


def test_replay_intent_write_is_rejected_by_the_real_api():
    engine = build_scenario("intent-lifecycle", seed=42, transport="fixture")
    app = create_app(
        engine.config, engine.allocator.sm, engine=engine, replay_mode=True,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/intents",
            json={
                "episode_id": engine.episode_id,
                "command_id": "replay-intent-write",
                **_payload(),
            },
        )
    assert response.status_code == 409
    assert response.json()["error_code"] == "replay_read_only"


def _free_position(engine):
    cols, rows = engine.config.grid.resolution
    for col in range(2, cols - 2):
        for row in range(2, rows - 2):
            position = (col + 0.5, row + 0.5)
            if engine.ship_land_mask[col, row] or engine.obstacle_mask[col, row]:
                continue
            if any(math.dist(position, ship.float_position) < 1.0 for ship in engine.ships):
                continue
            return position
    raise AssertionError("fixture has no free water position")


class _InvalidRedGateway(_FixtureGateway):
    def request_json(self, *, role, snapshot_id, user_payload, validate, **kwargs):
        if role != "red_commander":
            return super().request_json(
                role=role,
                snapshot_id=snapshot_id,
                user_payload=user_payload,
                validate=validate,
                **kwargs,
            )
        payload = {
            "schema_version": "red-plan/v1",
            "snapshot_id": snapshot_id,
            "valid_for_min": 1.0,
            "commands": [],
            "notes": "invalid fixture plan",
        }
        errors = tuple(validate(payload)) if validate else ()
        return self._result(role, snapshot_id, payload, errors)


def test_runtime_vessel_lifecycle_and_red_motion_share_the_real_engine_boundary():
    engine = build_scenario("vessel-red-lifecycle", seed=42, transport="fixture")
    target = next(ship for ship in engine.ships if ship.vessel_class == "type_ii")
    engine.surveillance_stages.set_fact(target.id, "sar", True, 0.0, "t11f-detected")
    before = target.float_position

    engine.step()

    assert engine.runtime_status == "running"
    assert engine.clock.time == 1.0
    assert target.float_position != before
    assert target._navigation_params is not None

    created_command = VesselCommand(
        command_id="t11f-create",
        episode_id=engine.episode_id,
        operation="create",
        vessel_class="type_ii",
        position_cells=_free_position(engine),
    )
    engine.vessel_commands.enqueue(created_command)
    created = engine.apply_pending_vessel_commands()[0]
    assert created.status == "applied"
    created_ship = next(ship for ship in engine.ships if ship.id == created.vessel_id)
    engine._ais_force_refresh_ids.add(created_ship.id)
    engine._refresh_ais_signals(2.0)
    signal = created_ship.ais_signal
    assert signal is not None
    assert engine._ais_history[signal.mmsi]

    before_disable = tuple(engine._ais_history[signal.mmsi])
    disable = VesselCommand(
        command_id="t11f-disable-ais",
        episode_id=engine.episode_id,
        operation="set_ais",
        vessel_id=created_ship.id,
        expected_revision=created.revision,
        ais_enabled=False,
    )
    engine.vessel_commands.enqueue(disable)
    disabled = engine.apply_pending_vessel_commands()[0]
    assert disabled.status == "applied"
    engine._refresh_ais_signals(3.0)
    assert created_ship.ais_signal is None
    assert tuple(engine._ais_history[signal.mmsi]) == before_disable

    wrong_episode = VesselCommand(
        command_id="t11f-wrong-episode",
        episode_id="other-episode",
        operation="delete",
        vessel_id=created_ship.id,
        expected_revision=disabled.revision,
    )
    engine.vessel_commands.enqueue(wrong_episode)
    wrong = engine.apply_pending_vessel_commands()[0]
    assert wrong.status == "rejected"
    assert wrong.error_code == "episode_conflict"
    assert any(ship.id == created_ship.id for ship in engine.ships)

    delete = VesselCommand(
        command_id="t11f-delete",
        episode_id=engine.episode_id,
        operation="delete",
        vessel_id=created_ship.id,
        expected_revision=disabled.revision,
    )
    engine.vessel_commands.enqueue(delete)
    removed = engine.apply_pending_vessel_commands()[0]
    assert removed.status == "applied"
    assert created_ship.id not in {ship.id for ship in engine.ships}
    assert created_ship.id not in engine._ship_position_history
    assert created_ship.id not in engine._emitter_track_ids
    assert not any(
        probe.uav_id == created_ship.id for probe in engine.allocator.sm.get_probe_sessions()
    )
    assert any(
        event["type"] == "vessel_removed"
        and event["data"]["vessel_id"] == created_ship.id
        for event in engine.allocator.sm.get_recent_events(0.0)
    )


def test_invalid_red_response_pauses_before_motion_or_clock_progress():
    engine = SimulationEngine(
        ConfigLoader.load(),
        seed=42,
        llm_gateway=_InvalidRedGateway(),
        episode_id="t11f-invalid-red",
    )
    target = next(ship for ship in engine.ships if ship.vessel_class == "type_ii")
    engine.surveillance_stages.set_fact(target.id, "sar", True, 0.0, "t11f-detected")
    before = tuple(ship.float_position for ship in engine.ships)

    engine.step()

    assert engine.runtime_status == "paused_model"
    assert engine.blocked_role == "red_commander"
    assert engine.clock.time == 0.0
    assert tuple(ship.float_position for ship in engine.ships) == before
    assert any(
        event["type"] == "red_decision_blocked"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )

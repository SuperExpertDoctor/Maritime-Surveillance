from fastapi.testclient import TestClient

from scripts.replay_restoration_scenarios import build_scenario
from src.mission.contracts import IntentCommand, VisualDetection
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

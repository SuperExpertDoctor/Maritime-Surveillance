from fastapi.testclient import TestClient

from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.runtime_journal import RuntimeJournal, publish_algorithm_events
from src.vis.backend.server import create_app


def test_journal_cursor_survives_bounded_retention_and_reconnect():
    journal = RuntimeJournal(capacity=2)
    for index in range(3):
        journal.append("llm", "info", "started", sim_time_min=index)

    assert [entry["id"] for entry in journal.since(0)] == [2, 3]
    assert [entry["id"] for entry in journal.since(2)] == [3]
    assert journal.since(3) == []


def test_journal_filters_episodes_but_advances_cursor_past_other_episodes():
    journal = RuntimeJournal()
    journal.append("llm", "info", "started", episode_id="run-old")
    journal.append("llm", "warning", "retry", episode_id="run-new")
    assert [item["status"] for item in journal.since(0, episode_id="run-new")] == ["retry"]
    assert journal.cursor == 2


def test_runtime_log_endpoint_uses_cursor_and_keeps_failure_metadata_small():
    config = ConfigLoader.load()
    app = create_app(config, StateManager(config))
    app.state.runtime_journal.append(
        "llm", "warning", "retry", sim_time_min=24, role="decision_maker",
        call_id="call-1", attempt=2,
    )
    with TestClient(app) as client:
        first = client.get("/api/runtime/logs?after=0")
        assert first.status_code == 200
        assert first.json()["entries"][0]["status"] == "retry"
        cursor = first.json()["cursor"]
        assert client.get(f"/api/runtime/logs?after={cursor}").json()["entries"] == []


def test_algorithm_events_are_deduplicated_and_replayed_as_statuses():
    config = ConfigLoader.load()
    state = StateManager(config)
    state.episode_id = "operator-run"
    state.current_time = 14
    state.add_event("mission_assignment_committed", {"task_ids": ["T-1"]})
    state.add_event("route_plan_failed", {"uav_id": "UAV-1"})
    journal = RuntimeJournal()
    seen = set()

    publish_algorithm_events(state, journal, seen)
    publish_algorithm_events(state, journal, seen)

    assert [item["status"] for item in journal.since(0)] == [
        "mission_assignment_committed", "route_plan_failed",
    ]
    assert [item["data"]["status"] for item in state.get_events_by_type("runtime_log")] == [
        "mission_assignment_committed", "route_plan_failed",
    ]


def test_decision_endpoint_returns_reason_content_only_for_current_episode():
    config = ConfigLoader.load()
    state = StateManager(config)
    state.episode_id = "operator-run"
    state.current_time = 14
    state.add_event("allocation_decision", {
        "reason_content": "Reassign the northern sweep.",
        "trigger_source": "periodic", "involved_uav_ids": ["UAV-1"],
    })
    app = create_app(config, state)
    with TestClient(app) as client:
        decision = client.get("/api/runtime/decisions?episode_id=operator-run")
        assert decision.status_code == 200
        assert decision.json()["decisions"][0]["data"]["reason_content"] == "Reassign the northern sweep."
        assert client.get("/api/runtime/decisions?episode_id=old-run").json() == {"decisions": []}

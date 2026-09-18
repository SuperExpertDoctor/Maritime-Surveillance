from types import SimpleNamespace

from src.env.simulation import SimulationEngine
from src.mission.contracts import AssignmentBatch
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader


class OfflineGateway:
    def request_json(self, **_kwargs):
        return ModelResult("offline", False, None, ("offline",), "transport")

    def request_text(self, **_kwargs):
        return ModelResult("offline-text", False, None, ("offline",), "transport")


def _engine():
    return SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=OfflineGateway())


def _failure(engine, category="transport"):
    engine._record_decision_maker_failure({
        "snapshot_id": "failure-snapshot",
        "llm_cycle": {
            "failure_category": category,
            "failure_stage": "transport",
        },
    })


def test_three_heavy_decision_failures_pause_only_the_mission_role():
    engine = _engine()

    _failure(engine)
    _failure(engine)
    assert engine.runtime_status == "running"
    _failure(engine)

    assert engine.runtime_status == "paused_model"
    assert engine.blocked_role == "decision_maker"
    assert engine._decision_failure_streak == 3
    assert engine.allocator.sm.get_recent_events(0.0)[-1]["type"] == (
        "mission_model_paused"
    )


def test_auth_failure_pauses_immediately_without_guessing_from_message_text():
    engine = _engine()

    _failure(engine, "http_402")

    assert engine.runtime_status == "paused_model"
    assert engine.blocked_role == "decision_maker"
    assert engine._decision_failure_streak == 1


def test_paused_step_does_not_advance_clock_or_count_again():
    engine = _engine()
    engine._set_runtime_state("paused_model", "decision_maker")
    engine._decision_failure_streak = 3
    before = engine.clock.time

    result = engine.step()

    assert engine.clock.time == before
    assert engine._decision_failure_streak == 3
    assert result["trigger_type"] == "none"


def test_decision_maker_retry_uses_one_frozen_clock_decision_and_recovers():
    engine = _engine()
    engine._set_runtime_state("paused_model", "decision_maker")
    calls = []
    batch = SimpleNamespace(selection_call_id="retry-call")

    def retry(current_time, **kwargs):
        calls.append((current_time, kwargs))
        return (
            {
                "trigger_type": "heavy",
                "action": "mission_selection_approved",
                "snapshot_id": "retry-snapshot",
                "llm_cycle": {"success": True},
            },
            batch,
        )

    engine.allocator.mission_step = retry
    engine.apply_assignment_batch = lambda _batch: True
    before = engine.clock.time

    engine.retry_blocked_decision()

    assert engine.runtime_status == "running"
    assert engine.clock.time == before
    assert calls == [(before, {
        "active_tasks": (),
        "intents": (),
        "intent_statuses": (),
        "force_heavy": True,
    })]
    assert engine._decision_failure_streak == 0


def test_failed_decision_maker_retry_stays_paused():
    engine = _engine()
    engine._set_runtime_state("paused_model", "decision_maker")
    engine.allocator.mission_step = lambda _time, **_kwargs: (
        {"trigger_type": "heavy", "action": "mission_selection_unavailable"},
        None,
    )

    engine.retry_blocked_decision()

    assert engine.runtime_status == "paused_model"
    assert engine.blocked_role == "decision_maker"


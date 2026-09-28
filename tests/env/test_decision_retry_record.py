from types import SimpleNamespace
from unittest.mock import Mock

from src.env.simulation import SimulationEngine


def test_failed_manual_retry_records_the_decision_without_claiming_assignments():
    state = SimpleNamespace(add_event=Mock(), current_time=17)
    result = {
        "trigger_type": "heavy", "trigger_source": "retry",
        "action": "mission_selection_failed", "llm_cycle": {"reason_content": "weather"},
    }
    allocator = SimpleNamespace(
        sm=state, mission_step=Mock(return_value=(result, None)),
        mission_scheduler=SimpleNamespace(
            last_selection_failure_category="timeout", last_selection_errors=[]),
    )
    engine = Mock()
    engine.runtime_status = "paused_model"
    engine.blocked_role = "decision_maker"
    engine.clock.time = 17
    engine.allocator = allocator
    engine._mission_task_records = {}
    engine.intents.intents.return_value = ()
    engine._evaluate_intent_statuses.return_value = ()

    SimulationEngine.retry_blocked_decision(engine)

    event = next(call.args[1] for call in state.add_event.call_args_list
                 if call.args[0] == "allocation_decision")
    assert event["trigger_source"] == "retry"
    assert event["reason_content"] == "weather"
    assert event["status"] == "failed"
    assert event["assignments"] == []


def test_skipped_manual_retry_does_not_commit_a_batch():
    state = SimpleNamespace(add_event=Mock(), current_time=17)
    allocator = SimpleNamespace(
        sm=state,
        mission_step=Mock(return_value=({"trigger_type": "heavy", "action": "mission_selection_skipped"}, object())),
        trigger_manager=SimpleNamespace(clear_heavy_retry=Mock()),
    )
    engine = Mock()
    engine.runtime_status = "paused_model"
    engine.blocked_role = "decision_maker"
    engine.clock.time = 17
    engine.allocator = allocator
    engine._mission_task_records = {}
    engine.intents.intents.return_value = ()
    engine._evaluate_intent_statuses.return_value = ()

    SimulationEngine.retry_blocked_decision(engine)

    engine.apply_assignment_batch.assert_not_called()
    assert not any(call.args[0] == "allocation_decision" for call in state.add_event.call_args_list)

from types import SimpleNamespace

from src.schedule.decision_records import build_decision_record


def test_committed_decision_keeps_reason_and_actual_assignments():
    batch = SimpleNamespace(assignments=(
        SimpleNamespace(task_id="T-4", uav_id="UAV-2"),
    ))
    result = {
        "trigger_type": "heavy", "trigger_source": "event",
        "snapshot_id": "snap-4", "selected_task_ids": ["T-4", "T-5"],
        "llm_cycle": {
            "call_id": "call-4", "reason_content": "优先处理高价值目标",
            "affected_uav_ids": ["UAV-1"],
        },
    }

    record = build_decision_record(result, batch, applied=True, time_min=24)

    assert record["reason_content"] == "优先处理高价值目标"
    assert record["selected_task_ids"] == ["T-4", "T-5"]
    assert record["assignments"] == [{"task_id": "T-4", "uav_id": "UAV-2"}]
    assert record["involved_uav_ids"] == ["UAV-1", "UAV-2"]
    assert record["status"] == "committed"


def test_failed_decision_never_claims_a_committed_assignment():
    result = {"trigger_type": "heavy", "trigger_source": "periodic",
              "llm_cycle": {"call_id": "call-5", "reason_content": ""}}
    record = build_decision_record(result, None, applied=False, time_min=31)
    assert record["status"] == "failed"
    assert record["assignments"] == []
    assert record["reason_content"] == ""


def test_light_pairing_is_not_an_llm_decision():
    assert build_decision_record({"trigger_type": "light"}, None, False, 31) is None


def test_skipped_model_selection_is_not_reported_as_failed_llm_call():
    assert build_decision_record({"trigger_type": "heavy", "action": "mission_selection_skipped"}, None, False, 31) is None

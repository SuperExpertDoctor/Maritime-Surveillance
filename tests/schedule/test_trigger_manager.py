import pytest
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.schedule.trigger_manager import TriggerManager
from src.mission.contracts import InfoFieldDelta


@pytest.fixture
def config():
    return ConfigLoader.load()


@pytest.fixture
def sm(config):
    return StateManager(config)


def test_initial_trigger_is_none(sm):
    tm = TriggerManager(sm)
    d = tm.check(0.0)
    assert d.trigger_type == "none"


def test_periodic_heavy_trigger(sm, config):
    tm = TriggerManager(sm)
    cycle = config.llm.heavy_cycle_min
    d = tm.check(cycle)
    assert d.trigger_type == "heavy"


def test_initial_deployment_triggers_heavy_without_waiting_for_periodic_cycle(sm):
    tm = TriggerManager(sm)

    decision = tm.check(1.0)

    assert decision.trigger_type == "heavy"
    assert decision.reason == "initial fleet deployment"


def test_decision_failure_retries_after_one_simulation_minute(sm):
    tm = TriggerManager(sm)
    sm.cycle = 1

    tm.schedule_heavy_retry(10.0, reason="decision_deadline_exceeded")

    assert tm.check(10.99).trigger_type == "none"
    decision = tm.check(11.0)
    assert decision.trigger_type == "heavy"
    assert decision.reason == "retry after decision_deadline_exceeded"
    assert tm.check(11.0).trigger_type == "none"


def test_uav_search_complete_light_trigger(sm):
    tm = TriggerManager(sm)
    tm.notify_event("search_complete", time=10.0, uav_id="UAV-1", region_id="S1")
    d = tm.check(10.0)
    assert d.trigger_type == "light"


def test_uav_returned_heavy_trigger(sm):
    tm = TriggerManager(sm)
    tm.notify_event("uav_returned", time=15.0, uav_id="UAV-3",
                    position={"col": 18, "row": 8}, marker_position={"col": 18, "row": 8})
    d = tm.check(15.0)
    assert d.trigger_type == "heavy"


@pytest.mark.parametrize(
    "event_type",
    ["assessment_changed", "resource_available", "intent_changed", "intent_expired"],
)
def test_mission_state_changes_trigger_heavy_replanning(sm, event_type):
    tm = TriggerManager(sm)
    tm.notify_event(event_type, time=5.0, event_id="same-frame")

    decision = tm.check(5.0)

    assert decision.trigger_type == "heavy"


def test_urgent_information_delta_triggers_one_versioned_heavy_plan(sm):
    sm.cycle = 1
    tm = TriggerManager(sm)
    tm.notify_information_delta(InfoFieldDelta(
        previous_version=6,
        version=7,
        changed_bbox=(2, 2, 5, 5),
        max_abs_value_delta=0.0,
        value_changed=False,
        crossed_candidate_threshold=False,
        urgent=True,
        reason_codes=("evasive_maneuver",),
        cause_evidence_ids=("EV-1",),
    ), time=7.0)

    first = tm.check(7.0)
    second = tm.check(7.0)
    assert first.trigger_type == "heavy"
    assert first.information_version == 7
    assert second.trigger_type == "none"

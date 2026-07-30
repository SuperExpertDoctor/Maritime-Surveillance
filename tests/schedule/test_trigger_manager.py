import pytest
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.schedule.trigger_manager import TriggerManager, TriggerDecision


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

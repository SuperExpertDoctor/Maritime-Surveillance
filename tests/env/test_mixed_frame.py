import json

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_builder import build_frame


def _frame(engine, **overrides):
    return build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
        **overrides,
    )


def test_v2_frame_publishes_contacts_and_runtime_state_without_truth_leaks(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "mixed-frame-offline")
    engine = SimulationEngine(ConfigLoader.load(), seed=41)

    frame = _frame(engine)

    assert frame["schema_version"] == "mission-frame/v2"
    assert frame["episode_id"] == engine.episode_id
    assert frame["runtime_status"] == "running"
    assert frame["blocked_role"] is None
    assert frame["memory_version"] == "baseline"
    assert frame["intents"] == []
    assert frame["intent_statuses"] == []
    assert frame["intent_events"] == []

    assert frame["contacts"]
    contact = frame["contacts"][0]
    assert {
        "contact_id", "revision", "state", "vessel_class", "ais_mmsi",
        "first_seen_min", "last_seen_min", "estimated_position",
        "estimated_velocity", "uncertainty_cells", "assigned_uav_id",
        "active_probe_id", "last_assessment", "cleared_at_min",
        "next_probe_not_before_min", "samples",
    } <= set(contact)
    assert contact["samples"]
    sample = contact["samples"][0]
    assert {
        "sample_id", "observed_at_min", "source", "source_id", "position",
        "velocity", "uncertainty_cells", "observer_position",
        "measured_range_cells", "navigation_context",
    } <= set(sample)

    observed_ship = engine.ships[0]
    engine._handle_detection(engine.uavs[0], observed_ship, 1.0)
    observed = _frame(engine)
    public_ship = next(ship for ship in observed["ships"] if ship["id"] == observed_ship.id)
    assert "is_evasive" not in public_ship
    assert "discrimination" not in public_ship
    assert "environment_vessel_class" not in json.dumps(public_ship, sort_keys=True)
    assert any(item["contact_id"] for item in observed["contacts"])


def test_v2_frame_defaults_are_safe_for_old_or_empty_state():
    config = ConfigLoader.load()
    state = StateManager(config)

    frame = build_frame(state, cycle=0, config=config, include_matrices=False)

    assert frame["schema_version"] == "mission-frame/v2"
    assert frame["episode_id"] == ""
    assert frame["contacts"] == []
    assert frame["intents"] == []
    assert frame["intent_statuses"] == []
    assert frame["intent_events"] == []
    assert frame["runtime_status"] == "running"
    assert frame["blocked_role"] is None
    assert frame["memory_version"] == "baseline"


def test_state_manager_vessel_inventory_is_a_deep_copied_operator_read_model():
    state = StateManager(ConfigLoader.load())
    item = {
        "scenario_entity_id": "scenario-vessel-1",
        "revision": 2,
        "position": [4.0, 5.0],
        "vessel_class": "type_ii",
        "ais_enabled": False,
        "ais_controllable": True,
        "surveillance_stage": "detected",
    }
    state.publish_vessel_inventory([item])
    item["position"][0] = 99

    inventory = state.get_vessel_inventory()
    inventory[0]["position"][0] = 88

    assert state.get_vessel_inventory()[0]["position"] == [4.0, 5.0]

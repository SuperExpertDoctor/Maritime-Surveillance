from copy import deepcopy

from src.vis.backend.replay_adapter import normalize_replay_frame


def test_old_replay_is_normalized_without_rewriting_source():
    old = {
        "scenario_vessels": [{
            "scenario_entity_id": "legacy-1",
            "revision": 4,
            "position": [4.0, 5.0],
            "vessel_class": "research",
            "ais_mode": "silent",
        }],
        "configured_vessel_count": 8,
    }
    original = deepcopy(old)

    new = normalize_replay_frame(old)

    vessel = new["scenario_vessels"][0]
    assert vessel["vessel_class"] == "type_ii"
    assert vessel["ais_enabled"] is False
    assert vessel["ais_controllable"] is True
    assert vessel["surveillance_stage"] == "undetected"
    assert new["initial_vessel_count"] == 8
    assert old == original


def test_replay_adapter_defaults_type_i_ais_and_does_not_share_nested_state():
    old = {"scenario_vessels": [{"vessel_class": "civilian", "position": [1, 2]}]}

    normalized = normalize_replay_frame(old)
    normalized["scenario_vessels"][0]["position"][0] = 99

    assert normalized["scenario_vessels"][0]["ais_enabled"] is True
    assert old["scenario_vessels"][0]["position"] == [1, 2]


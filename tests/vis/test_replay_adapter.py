from copy import deepcopy
import hashlib
import json

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


def test_replay_adapter_fills_historical_defaults_and_keeps_routes_empty():
    old = {
        "frame_id": 17,
        "uavs": [{"id": "UAV-1", "status": "tracking", "position": [3, 4]}],
        "events": [{"type": "target_found", "time": 2.5, "data": {"id": "C-1"}}],
    }
    source = hashlib.sha256(
        json.dumps(old, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    normalized = normalize_replay_frame(old)

    assert normalized["contacts"] == []
    assert normalized["scenario_vessels"] == []
    assert normalized["uavs"][0]["planned_path"] == []
    assert normalized["uavs"][0]["mission_route"] == []
    assert normalized["uavs"][0]["position"] == [3, 4]
    assert hashlib.sha256(
        json.dumps(old, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest() == source
    assert old["uavs"][0] == {"id": "UAV-1", "status": "tracking", "position": [3, 4]}


def test_old_replay_keeps_unknown_persistent_coverage_null():
    old = {"info_matrix": [[1.0]], "value_matrix": [[0.5]]}

    normalized = normalize_replay_frame(old)

    assert normalized["coverage_metrics"] is None


def test_new_replay_preserves_persistent_coverage_as_a_deep_copy():
    coverage = {
        "schema_version": "persistent-coverage/future",
        "episode_id": "replay-coverage",
        "as_of_min": 17.0,
        "windows": [{"minutes": 60, "covered_cells": 3}],
    }
    source = {"coverage_metrics": coverage}

    normalized = normalize_replay_frame(source)
    assert normalized["coverage_metrics"] == coverage
    assert normalize_replay_frame(json.loads(json.dumps(source)))["coverage_metrics"] == coverage
    normalized["coverage_metrics"]["windows"][0]["covered_cells"] = 99

    assert source["coverage_metrics"] == coverage
    assert coverage["windows"][0]["covered_cells"] == 3

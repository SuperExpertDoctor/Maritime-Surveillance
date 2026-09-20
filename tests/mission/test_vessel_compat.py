from typing import get_args

import pytest

from src.mission.contracts import VesselClass, VesselCommand
from src.mission.vessel_compat import (
    normalize_legacy_ais_enabled,
    normalize_legacy_ship_config,
    normalize_legacy_vessel_class,
)
from src.schedule.config_loader import ConfigLoader, PopulationConfig, allocate_population


def test_new_runtime_contract_uses_only_type_i_and_type_ii():
    assert get_args(VesselClass) == ("unknown", "type_i", "type_ii")
    assert PopulationConfig(8, 0.625, 0.375).allocate() == {
        "type_i": 5,
        "type_ii": 3,
    }
    assert allocate_population(8, {"type_i": 0.625, "type_ii": 0.375}) == {
        "type_i": 5,
        "type_ii": 3,
    }


@pytest.mark.parametrize(
    "value, expected",
    [
        ("civilian", "type_i"),
        ("research", "type_ii"),
        ("military", "type_ii"),
        ("target", "type_ii"),
    ],
)
def test_legacy_class_values_are_normalized_only_at_input(value, expected):
    assert normalize_legacy_vessel_class(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(True, True), (False, False), ("civilian", True), ("silent", False)],
)
def test_legacy_ais_values_are_normalized_at_input(value, expected):
    assert normalize_legacy_ais_enabled(value) is expected


def test_legacy_ship_config_normalization_does_not_mutate_input():
    legacy = {
        "population": {
            "total_count": 2,
            "civilian_ratio": 1.0,
            "research_ratio": 0.0,
        },
        "target_ais_on_probability": 0.25,
    }

    normalized = normalize_legacy_ship_config(legacy)

    assert normalized["population"] == {
        "total_count": 2,
        "type_i_ratio": 1.0,
        "type_ii_ratio": 0.0,
    }
    assert normalized["type_ii_ais_on_probability"] == 0.25
    assert legacy["population"]["civilian_ratio"] == 1.0


def test_new_ship_config_rejects_old_population_keys(tmp_path):
    path = tmp_path / "ship.yaml"
    path.write_text(
        "population: {total_count: 2, civilian_ratio: 1, research_ratio: 0}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="type_i_ratio"):
        ConfigLoader.load(ship_path=path)


def test_set_ais_command_has_a_boolean_payload():
    command = VesselCommand(
        "a-1",
        "episode-1",
        "set_ais",
        "Ship-2",
        4,
        None,
        None,
        False,
    )
    assert command.ais_enabled is False

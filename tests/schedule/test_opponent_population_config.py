from dataclasses import replace

import pytest
import yaml

from src.schedule.config_loader import ConfigLoader, OpponentPopulationConfig


def load(tmp_path, value, *, omit=False):
    data = yaml.safe_load(open("configs/ship.yaml"))
    data.pop("opponent_population", None)
    if not omit:
        data["opponent_population"] = value
    path = tmp_path / "ship.yaml"
    path.write_text(yaml.safe_dump(data))
    return ConfigLoader.load(ship_path=path).ship.opponent_population


def test_defaults_enabled_and_legacy_omission(tmp_path):
    config = ConfigLoader.load().ship.opponent_population
    assert config.enabled is True and config.max_active == 5
    assert config.type_i_probability == 0.5
    assert load(tmp_path, None, omit=True) == OpponentPopulationConfig()


@pytest.mark.parametrize("value", [
    None, [], False, {"typo": 1}, {"enabled": "true"}, {"enabled": 1},
    {"interval_min_min": 0}, {"interval_min_min": -1},
    {"interval_min_min": True}, {"interval_max_min": "3"},
    {"interval_max_min": float("inf")}, {"interval_min_min": float("nan")},
    {"interval_min_min": 9, "interval_max_min": 8},
    {"max_active": 0}, {"max_active": 1.5}, {"max_active": True},
    {"max_active_type_i": -1}, {"max_active_type_ii": True},
    {"max_active_type_i": 1.5}, {"max_active_type_ii": 0},
    {"type_i_probability": -0.1}, {"type_i_probability": 1.1},
    {"type_i_probability": False}, {"type_i_probability": float("nan")},
])
def test_invalid_config_is_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="opponent_population"):
        load(tmp_path, value)


def test_disabled_still_validates_and_accepts_custom_values(tmp_path):
    config = load(tmp_path, dict(enabled=False, interval_min_min=0.5,
                                interval_max_min=2, max_active=3,
                                type_i_probability=0))
    assert config.enabled is False and config.interval_min_min == 0.5
    with pytest.raises(ValueError, match="opponent_population"):
        replace(config, max_active=-1)

"""界面「算法参数设置」的服务端校验与覆盖配置测试。

只测试纯函数（settings_payload / validate_values / apply_overrides /
load_overrides + ConfigLoader 的覆盖合并），不触发 /api/settings 的
进程重启路径。
"""

import pytest
import yaml

from src.schedule.config_loader import ConfigLoader
from src.vis.backend.settings_schema import (
    OVERRIDES_NAME,
    apply_overrides,
    load_overrides,
    settings_payload,
    validate_values,
)


@pytest.fixture()
def config():
    return ConfigLoader.load("configs")


def _fields(config):
    return {
        f["key"]: f
        for group in settings_payload(config)["groups"]
        for f in group["fields"]
    }


def test_payload_groups_and_chinese_labels(config):
    payload = settings_payload(config)
    assert [g["id"] for g in payload["groups"]] == [
        "uav", "ship_i", "ship_ii", "mission", "contact",
    ]
    fields = _fields(config)
    speed = fields["uav.cruise_speed_kmh"]
    assert speed["label"] == "巡航速度"
    assert speed["unit"] == "km/h"
    assert speed["value"] == config.uav.cruise_speed_kmh
    # 所有字段都必须有中文标签，不允许出现英文字段名
    for key, field in fields.items():
        assert field["label"], key
        assert not field["label"].isascii() or field["label"] == "SAR", key


def test_validate_rejects_non_positive_speed(config):
    errors = validate_values({"uav.cruise_speed_kmh": 0}, config)
    assert "uav.cruise_speed_kmh" in errors
    errors = validate_values({"uav.cruise_speed_kmh": -5}, config)
    assert "uav.cruise_speed_kmh" in errors
    assert validate_values({"uav.cruise_speed_kmh": 160}, config) == {}


def test_validate_rejects_unknown_and_non_numeric(config):
    assert "not.a.real.key" in validate_values({"not.a.real.key": 1}, config)
    errors = validate_values({"uav.cruise_speed_kmh": "abc"}, config)
    assert "uav.cruise_speed_kmh" in errors


def test_validate_cross_field_pairs(config):
    # speed_min 必须小于 speed_max
    errors = validate_values({"ship.speed_min_kn": 99}, config)
    assert "ship.speed_min_kn" in errors
    # 占比之和必须为 1（修改一侧时按合并后的值校验）
    errors = validate_values({"ship.population.type_i_ratio": 0.9}, config)
    assert "ship.population.type_i_ratio" in errors
    ok = validate_values(
        {"ship.population.type_i_ratio": 0.7, "ship.population.type_ii_ratio": 0.3},
        config,
    )
    assert ok == {}


def test_validate_integer_type(config):
    errors = validate_values({"uav.count_max": 1.5}, config)
    assert "uav.count_max" in errors
    assert validate_values({"uav.count_max": 8}, config) == {}


def test_apply_overrides_roundtrip(tmp_path, config):
    import glob, shutil
    for path in glob.glob("configs/*.yaml"):
        shutil.copy(path, tmp_path)
    apply_overrides(
        {"uav.cruise_speed_kmh": 200.0, "ship.speed_max_kn": 25.0},
        str(tmp_path),
    )
    data = yaml.safe_load((tmp_path / OVERRIDES_NAME).read_text(encoding="utf-8"))
    assert data["uav"]["cruise_speed_kmh"] == 200.0
    assert data["ship"]["speed_max_kn"] == 25.0
    assert load_overrides(str(tmp_path)) == data
    merged = ConfigLoader.load(str(tmp_path))
    assert merged.uav.cruise_speed_kmh == 200.0
    assert merged.ship.speed_max_kn == 25.0
    # 未覆盖项保持原值
    assert merged.uav.endurance_h == config.uav.endurance_h


def test_apply_overrides_empty_writes_empty(tmp_path):
    apply_overrides({}, str(tmp_path))
    data = yaml.safe_load((tmp_path / OVERRIDES_NAME).read_text(encoding="utf-8"))
    assert not data

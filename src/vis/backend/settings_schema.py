"""算法参数设置界面使用的字段目录与校验。

每条字段对应 configs/*.yaml 中的一个配置键。界面只显示中文名称，
内部用 ``key``（``<yaml文件名>.<嵌套路径>``）定位。前端与服务端共用同一
份规则：前端做即时红框提示，服务端在写入前做权威校验。
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import yaml


CONFIG_ROOT = Path(__file__).resolve().parents[3] / "configs"
OVERRIDES_NAME = "ui-overrides.yaml"

# 分组：key → 左侧导航。ship_i / ship_ii 为 I/II 类船舶页签；
# ``shared`` 标记两类船舶共用的机动参数（在两个页签中都会出现）。
GROUPS = (
    ("uav", "无人机（UAV）"),
    ("ship_i", "I 类船舶"),
    ("ship_ii", "II 类船舶"),
    ("mission", "覆盖与调度"),
    ("contact", "接触与识别"),
)

# type: number（浮点）/ integer / boolean
# lt / gt: 与另一字段的数值比较约束（以提交后的合并值为准）
# shared: I/II 类船舶共用参数，两个页签都显示
# editable: 允许经界面写入 ui-overrides.yaml
FIELDS = (
    # ---------------- 无人机（UAV） ----------------
    dict(key="uav.count_max", group="uav", label="无人机数量", unit="架",
         type="integer", min=1, max=40),
    dict(key="uav.cruise_speed_kmh", group="uav", label="巡航速度", unit="km/h",
         type="number", min=1.0, max=600.0),
    dict(key="uav.sortie_endurance_h", group="uav", label="单次出动续航时间", unit="小时",
         type="number", min=0.5, max=24.0),
    dict(key="uav.endurance_h", group="uav", label="平台总续航时间", unit="小时",
         type="number", min=1.0, max=96.0),
    dict(key="uav.refuel_time_min", group="uav", label="基地补给时间", unit="分钟",
         type="number", min=0.0, max=240.0),
    dict(key="uav.lifecycle_rotation_start_min", group="uav",
         label="轮换机制启动时间", unit="分钟", type="number", min=0.0, max=1440.0),
    dict(key="uav.lifecycle_search_dwell_min", group="uav",
         label="轮换搜索驻留时间", unit="分钟", type="number", min=0.0, max=120.0),
    # ---------------- I 类船舶（含两类共用机动参数） ----------------
    dict(key="ship.population.type_i_ratio", group="ship_i",
         label="初始船舶中 I 类占比", unit="", type="number", min=0.0, max=1.0,
         pair_sum="ship.population.type_ii_ratio"),
    dict(key="ship.ais_update_interval_min", group="ship_i",
         label="AIS 更新间隔", unit="分钟", type="number", min=0.1, max=60.0),
    dict(key="ship.ais_position_noise_cells", group="ship_i",
         label="AIS 定位误差", unit="栅格", type="number", min=0.0, max=5.0),
    # ---------------- II 类船舶 ----------------
    dict(key="ship.population.type_ii_ratio", group="ship_ii",
         label="初始船舶中 II 类占比", unit="", type="number", min=0.0, max=1.0,
         pair_sum="ship.population.type_i_ratio"),
    dict(key="ship.type_ii_ais_on_probability", group="ship_ii",
         label="AIS 开启概率", unit="", type="number", min=0.0, max=1.0),
    dict(key="ship.opponent_population.type_i_probability", group="ship_ii",
         label="随机新船为 I 类的概率", unit="", type="number", min=0.0, max=1.0),
    dict(key="ship.red_decision_cycle_min", group="ship_ii",
         label="II 类决策周期", unit="分钟", type="number", min=0.5, max=60.0),
    dict(key="ship.red_plan_valid_min", group="ship_ii",
         label="II 类计划有效期", unit="分钟", type="number", min=0.5, max=120.0),
    dict(key="ship.zigzag_heading_max_deg", group="ship_ii",
         label="蛇形机动航向幅度", unit="度", type="number", min=0.0, max=90.0),
    dict(key="ship.zigzag_period_min_min", group="ship_ii",
         label="蛇形机动周期下限", unit="分钟", type="number", min=0.5, max=120.0,
         lt="ship.zigzag_period_max_min"),
    dict(key="ship.zigzag_period_max_min", group="ship_ii",
         label="蛇形机动周期上限", unit="分钟", type="number", min=1.0, max=240.0,
         gt="ship.zigzag_period_min_min"),
    dict(key="ship.min_evasion_heading_deg", group="ship_ii",
         label="最小规避转向角", unit="度", type="number", min=0.0, max=90.0),
    dict(key="ship.min_evasion_speed_delta_kn", group="ship_ii",
         label="最小规避速度增量", unit="节", type="number", min=0.0, max=30.0),
    # -------- 两类船舶共用的机动参数（在两个页签中都会出现） --------
    dict(key="ship.speed_kn", group="ship_i", groups=("ship_i", "ship_ii"),
         label="巡航速度", unit="节", type="number", min=0.1, max=60.0, shared=True),
    dict(key="ship.speed_min_kn", group="ship_i", groups=("ship_i", "ship_ii"),
         label="最小速度", unit="节", type="number", min=0.0, max=60.0, shared=True,
         lt="ship.speed_max_kn"),
    dict(key="ship.speed_max_kn", group="ship_i", groups=("ship_i", "ship_ii"),
         label="最大速度", unit="节", type="number", min=0.1, max=120.0, shared=True,
         gt="ship.speed_min_kn"),
    dict(key="ship.max_turn_rate_deg_min", group="ship_i", groups=("ship_i", "ship_ii"),
         label="最大转向角速率", unit="度/分", type="number", min=0.1, max=180.0, shared=True),
    dict(key="ship.yaw_time_constant_min", group="ship_i", groups=("ship_i", "ship_ii"),
         label="转向时间常数", unit="分钟", type="number", min=0.1, max=30.0, shared=True),
    dict(key="ship.heading_control_gain_per_min", group="ship_i", groups=("ship_i", "ship_ii"),
         label="转向控制增益", unit="1/分", type="number", min=0.01, max=5.0, shared=True),
    dict(key="ship.turn_speed_loss_fraction", group="ship_i", groups=("ship_i", "ship_ii"),
         label="转向速度损失比", unit="", type="number", min=0.0, max=0.5, shared=True),
    dict(key="ship.max_acceleration_kn_per_min", group="ship_i", groups=("ship_i", "ship_ii"),
         label="最大加速度", unit="节/分", type="number", min=0.1, max=60.0, shared=True),
    dict(key="ship.heading_offset_max_deg", group="ship_i", groups=("ship_i", "ship_ii"),
         label="航向偏置上限", unit="度", type="number", min=0.0, max=180.0, shared=True),
    dict(key="ship.navigation_horizon_min", group="ship_i", groups=("ship_i", "ship_ii"),
         label="导航规划时域", unit="分钟", type="number", min=1.0, max=120.0, shared=True),
    # ---------------- 覆盖与调度 ----------------
    dict(key="mission.scheduling.reassignment_cooldown_min", group="mission",
         label="重新分配冷却时间", unit="分钟", type="number", min=1.0, max=480.0),
    dict(key="mission.scheduling.max_tasks_in_prompt", group="mission",
         label="提示窗口任务上限", unit="个", type="integer", min=4, max=200),
    dict(key="mission.scheduling.allow_probe_preempt_search", group="mission",
         label="允许核查任务抢占搜索", unit="", type="boolean"),
    dict(key="mission.scheduling.allow_intent_preempt_search", group="mission",
         label="允许重点区抢占搜索", unit="", type="boolean"),
    dict(key="mission.coverage.min_search_uav_fraction", group="mission",
         label="搜索兵力占比下限", unit="", type="number", min=0.0, max=1.0,
         lt="mission.coverage.search_uav_fraction_max"),
    dict(key="mission.coverage.search_uav_fraction_max", group="mission",
         label="搜索兵力占比上限", unit="", type="number", min=0.0, max=1.0,
         gt="mission.coverage.min_search_uav_fraction"),
    dict(key="mission.coverage.primary_window_min", group="mission",
         label="覆盖率主窗口时长", unit="分钟", type="number", min=5.0, max=480.0),
    # ---------------- 接触与识别 ----------------
    dict(key="mission.contact.stale_after_min", group="contact",
         label="接触过期时间", unit="分钟", type="number", min=1.0, max=120.0),
    dict(key="mission.contact.assessment_interval_min", group="contact",
         label="接触评估周期", unit="分钟", type="number", min=0.5, max=30.0),
    dict(key="mission.contact.assessment_confidence_min", group="contact",
         label="评估置信度阈值", unit="", type="number", min=0.0, max=1.0),
    dict(key="mission.contact.probe_timeout_min", group="contact",
         label="核查任务超时时间", unit="分钟", type="number", min=1.0, max=240.0),
)

_FIELD_INDEX = {field["key"]: field for field in FIELDS}


def _dig(data: dict, dotted: str):
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    if not isinstance(node, dict):
        return None
    return node.get(parts[-1])


def _set_dig(data: dict, dotted: str, value) -> None:
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _config_to_dict(config) -> dict:
    """把加载后的 AppConfig dataclass 摊平成 {key: value} 供界面显示。"""
    from dataclasses import asdict

    raw = {
        "uav": asdict(config.uav),
        "ship": asdict(config.ship),
        "mission": asdict(config.mission),
    }
    out = {}
    for field in FIELDS:
        key = field["key"]
        # key 形如 "uav.count_max" / "ship.population.type_i_ratio"
        parts = key.split(".")
        section = parts[0]
        out[key] = _dig(raw[section], ".".join(parts[1:]))
    return out


def settings_payload(config) -> dict:
    """GET /api/settings 返回的分组字段与当前值。"""
    values = _config_to_dict(config)
    groups = []
    for group_id, title in GROUPS:
        fields = []
        for field in FIELDS:
            members = field.get("groups") or (field["group"],)
            if group_id not in members:
                continue
            item = {
                "key": field["key"],
                "label": field["label"],
                "unit": field.get("unit", ""),
                "type": field["type"],
                "min": field.get("min"),
                "max": field.get("max"),
                "value": values[field["key"]],
            }
            if field.get("shared"):
                item["shared"] = True
            for rule in ("lt", "gt", "pair_sum"):
                if field.get(rule):
                    item[rule] = field[rule]
            groups_note = "（I/II 类共用）" if field.get("shared") else ""
            item["note"] = groups_note
            fields.append(item)
        groups.append({"id": group_id, "title": title, "fields": fields})
    return {"groups": groups}


def _validate_one(field: dict, value, merged: dict) -> str | None:
    """单个字段校验；返回中文错误文案或 None。"""
    label = field["label"]
    ftype = field["type"]
    if ftype == "boolean":
        if not isinstance(value, bool):
            return f"{label}：取值必须是 是/否"
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{label}：请输入数字"
    if not math.isfinite(value):
        return f"{label}：请输入有限数字"
    if ftype == "integer" and not float(value).is_integer():
        return f"{label}：请输入整数"
    minimum = field.get("min")
    maximum = field.get("max")
    if minimum is not None and value < minimum:
        return f"{label}：不能小于 {minimum}"
    if maximum is not None and value > maximum:
        return f"{label}：不能大于 {maximum}"
    other_key = field.get("lt")
    if other_key:
        other = merged.get(other_key)
        if isinstance(other, (int, float)) and not value < other:
            other_label = _FIELD_INDEX[other_key]["label"]
            return f"{label}：必须小于「{other_label}」"
    other_key = field.get("gt")
    if other_key:
        other = merged.get(other_key)
        if isinstance(other, (int, float)) and not value > other:
            other_label = _FIELD_INDEX[other_key]["label"]
            return f"{label}：必须大于「{other_label}」"
    pair_key = field.get("pair_sum")
    if pair_key:
        other = merged.get(pair_key)
        if isinstance(other, (int, float)) and abs(value + other - 1.0) > 1e-6:
            other_label = _FIELD_INDEX[pair_key]["label"]
            return f"{label}：与「{other_label}」之和必须为 1"
    return None


def validate_values(values: dict, config) -> dict:
    """校验一份 {key: value} 提交；返回 {key: 错误文案}（为空即合法）。"""
    if not isinstance(values, dict):
        return {"_": "请求格式不正确"}
    errors: dict[str, str] = {}
    unknown = sorted(set(values) - set(_FIELD_INDEX))
    if unknown:
        return {key: "未知参数" for key in unknown}
    merged = _config_to_dict(config)
    merged.update(values)
    for key, value in values.items():
        message = _validate_one(_FIELD_INDEX[key], value, merged)
        if message:
            errors[key] = message
    return errors


def load_overrides(config_dir: str | os.PathLike[str]) -> dict:
    """读取 configs/ui-overrides.yaml（不存在时返回空）。"""
    path = Path(config_dir) / OVERRIDES_NAME
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    return data if isinstance(data, dict) else {}


def apply_overrides(values: dict, config_dir: str | os.PathLike[str]) -> Path:
    """把校验通过的字段写入 ui-overrides.yaml（与其它手改文件隔离）。

    文件结构镜像各 yaml 的分层：``uav: {count_max: 10}``、
    ``ship: {population: {type_i_ratio: 0.5}}``、``mission: {scheduling: ...}``。
    """
    overrides = load_overrides(config_dir)
    for key, value in values.items():
        parts = key.split(".")
        section = parts[0]
        node = overrides.setdefault(section, {})
        _set_dig(node, ".".join(parts[1:]), value)
    path = Path(config_dir) / OVERRIDES_NAME
    header = (
        "# 本文件由界面「算法参数设置」写入，会被 ConfigLoader 合并到同名配置之上。\n"
        "# 删除本文件即可恢复 configs/*.yaml 中的默认值。\n"
    )
    path.write_text(
        header + yaml.safe_dump(overrides, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )
    return path

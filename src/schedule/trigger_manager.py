from dataclasses import dataclass, field
import math
from copy import deepcopy

from src.mission.contracts import InfoFieldDelta
from src.schedule.state_manager import StateManager


@dataclass
class TriggerDecision:
    trigger_type: str  # "light" | "heavy" | "none"
    reason: str = ""
    affected_uavs: list[str] = field(default_factory=list)
    information_version: int = 0
    source: str = "unknown"


_EVENT_ENTITY_KEYS = (
    "uav_id", "contact_id", "intent_id", "task_id", "vessel_id", "ship_id",
    "storm_id", "group_id", "region_id", "base_id", "target_id",
    "handoff_id", "track_id", "stage",
)

# 事件类型 → 中文名。决策原因直接展示给用户，禁止使用内部英文代号。
_EVENT_NAMES = {
    "contact_created": "新接触",
    "contact_merged": "接触合并",
    "contact_lost": "接触失联",
    "assessment_changed": "评估更新",
    "type_i_assessed": "I类船评估完成",
    "type_ii_assessed": "II类船评估完成",
    "type_i_released": "I类船解除",
    "type_ii_confirmed": "II类船确认",
    "resource_available": "资源空闲",
    "mission_task_released": "任务释放",
    "intent_changed": "重点区变更",
    "intent_expired": "重点区过期",
    "uav_returned": "无人机返航",
    "target_found": "发现目标",
    "target_lost": "目标丢失",
    "lifecycle_completed": "轮换完成",
    "target_departed": "目标驶离",
    "storm_spawned": "风暴生成",
    "storm_dissipated": "风暴消散",
    "handoff_required": "需要交接",
    "ais_transmission_changed": "AIS状态变更",
    "surveillance_stage_changed": "侦察阶段变更",
    "search_complete": "搜索完成",
    "uav_refueled": "加油完成",
    "base_capacity_full": "基地机位满",
    "uav_fuel_low_warning": "油量不足预警",
    "information_delta": "信息场更新",
}

# 信息场更新的 reason code → 中文名。
_REASON_CODE_NAMES = {
    "scan_sar": "SAR扫描",
    "scan_search": "搜索扫描",
    "scan_track": "跟踪扫描",
    "scan_optical": "光电扫描",
    "evasive_maneuver": "规避机动",
    "passive_position": "被动定位",
    "passive_bearing": "被动测向",
    "ais_position": "AIS定位",
    "type_ii_assessment": "II类评估",
    "violation_assessment": "违规评估",
    "handoff": "目标交接",
    "evidence_expired": "证据过期",
    "time_decay": "时间衰减",
    "contact_created": "新接触",
}


def _describe_event(event: dict) -> str:
    """One human-readable clause naming the event and its subject."""
    etype = str(event.get("type", "unknown"))
    name = _EVENT_NAMES.get(etype, etype)
    if etype == "information_delta":
        bits = []
        if event.get("information_version") is not None:
            bits.append(f"v{event['information_version']}")
        bits.extend(
            _REASON_CODE_NAMES.get(str(code), str(code))
            for code in event.get("reason_codes") or ()
        )
        return f"{name}({', '.join(bits)})" if bits else name
    for key in _EVENT_ENTITY_KEYS:
        value = event.get(key)
        if value:
            return f"{name}({value})"
    return name


def _events_description(events) -> str:
    """Join per-event clauses; duplicates collapse so the reason stays factual."""
    return "、".join(dict.fromkeys(_describe_event(event) for event in events))


class TriggerManager:
    def __init__(self, sm: StateManager):
        self._sm = sm
        self._pending_events: list[dict] = []
        self._event_dedup_expires: dict[tuple[str, str], float] = {}
        self._last_heavy_time: float = 0.0
        self._last_light_time: float | None = None
        self._heavy_retry_at: float | None = None
        self._heavy_retry_reason: str = "decision_failed"
        self._last_checked_events: tuple[dict, ...] = ()

    def notify_event(self, event_type: str, time: float, **kwargs) -> None:
        # Keep deduplication independent from the pending queue.  check()
        # consumes pending events, but a repeated edge remains suppressed
        # until its explicit expiry time.
        event_time = float(time)
        self._prune_event_dedup(event_time)
        uav_id = str(kwargs.get("uav_id", ""))
        key = (str(event_type), uav_id)
        if event_time < self._event_dedup_expires.get(key, -math.inf):
            return
        self._event_dedup_expires[key] = event_time + 5.0
        self._pending_events.append({
            "type": event_type,
            "time": event_time,
            **kwargs,
        })

    def notify_information_delta(self, delta: InfoFieldDelta, *, time: float | None = None) -> None:
        if not isinstance(delta, InfoFieldDelta):
            raise TypeError("delta must be InfoFieldDelta")
        event_time = self._sm.current_time if time is None else float(time)
        cause_ids = tuple(delta.cause_evidence_ids)
        if any(
            event["type"] == "information_delta"
            and event.get("information_version") == delta.version
            and event.get("cause_evidence_ids") == cause_ids
            for event in self._pending_events
        ):
            return
        self._pending_events.append({
            "type": "information_delta",
            "time": event_time,
            "information_version": delta.version,
            "urgent": delta.urgent,
            "value_changed": delta.value_changed,
            "max_abs_value_delta": delta.max_abs_value_delta,
            "crossed_candidate_threshold": delta.crossed_candidate_threshold,
            "reason_codes": tuple(delta.reason_codes),
            "cause_evidence_ids": cause_ids,
        })

    def pending_events_for_test(self) -> tuple[dict, ...]:
        """Expose a read-only event view for end-to-end trigger assertions."""
        return tuple(deepcopy((*self._last_checked_events, *self._pending_events)))

    def check(self, current_time: float) -> TriggerDecision:
        """检查是否需要触发，返回决策。"""
        self._prune_event_dedup(float(current_time))
        decision = self._check_events(current_time)
        if decision.trigger_type != "none":
            return decision

        if self._heavy_retry_at is not None and current_time >= self._heavy_retry_at:
            reason = self._heavy_retry_reason
            self._heavy_retry_at = None
            return TriggerDecision(
                trigger_type="heavy",
                reason=f"上次决策失败后重试({reason})",
                source="retry",
            )

        # The fleet begins with no approved SAR partition.  Waiting an entire
        # periodic cycle before the first real LLM decision strands every UAV
        # at its land base during the most valuable coverage window.
        if self._sm.cycle == 0 and current_time > 0.0:
            return TriggerDecision(
                trigger_type="heavy",
                reason="初始编队部署",
                source="initial",
            )

        # 周期定时（独立于事件）
        cycle = self._sm.config.llm.heavy_cycle_min
        if current_time >= cycle and current_time - self._last_heavy_time >= cycle:
            return TriggerDecision(
                trigger_type="heavy",
                reason=f"周期性重规划(每{cycle}分钟)",
                source="periodic",
            )

        return TriggerDecision("none")

    def _check_events(self, current_time: float) -> TriggerDecision:
        """处理 pending 事件，返回基于事件的决策。"""
        if not self._pending_events:
            self._last_checked_events = ()
            return TriggerDecision("none")

        # 过滤 5min 内的事件
        recent = [e for e in self._pending_events
                  if current_time - e["time"] <= 5.0]
        # All queued events are either processed now or stale; neither should
        # be reconsidered on the next simulation step.
        self._pending_events = []
        self._last_checked_events = tuple(recent)

        if not recent:
            return TriggerDecision("none")

        # Heavy: structural changes requiring LLM re-planning
        heavy_types = {
            "contact_created",
            "contact_merged",
            "contact_lost",
            "assessment_changed",
            "type_i_assessed",
            "type_ii_assessed",
            "type_i_released",
            "type_ii_confirmed",
            "resource_available",
            "mission_task_released",
            "intent_changed",
            "intent_expired",
            "uav_returned",
            "target_found",
            "target_lost",
            "lifecycle_completed",
            # GOAL2: tracking resource released — need LLM to re-plan regions
            "target_departed",
            # GOAL2: dynamic environment — storms may open/block searchable area
            "storm_spawned",
            "storm_dissipated",
            "handoff_required",
            "ais_transmission_changed",
            "surveillance_stage_changed",
        }
        # Light: incremental adjustments handled by Hungarian pairing only
        light_types = {
            "search_complete",
            "uav_refueled",
            # GOAL2: base congestion — re-pair idle UAVs without LLM
            "base_capacity_full",
            # GOAL2: proactive fuel warning — pre-assign replacement UAV
            "uav_fuel_low_warning",
        }

        info_heavy = [
            event for event in recent
            if event["type"] == "information_delta"
            and (
                event.get("urgent")
                or event.get("value_changed")
                and (
                    event.get("max_abs_value_delta", 0.0) >= 0.05
                    or event.get("crossed_candidate_threshold")
                )
                or "evidence_expired" in event.get("reason_codes", ())
            )
        ]
        heavy_count = sum(1 for e in recent if e["type"] in heavy_types) + len(info_heavy)
        light_count = sum(1 for e in recent if e["type"] in light_types)

        # 重量触发条件：任一 heavy 事件，或 5min 内 >=3 个事件
        if heavy_count > 0 or heavy_count + light_count >= 3:
            affected = list(set(
                e.get("uav_id", "") for e in recent
                if e.get("uav_id", "")
            ))
            return TriggerDecision(
                trigger_type="heavy",
                reason=_events_description(recent),
                source="event",
                affected_uavs=affected,
                information_version=max(
                    (int(event.get("information_version", 0)) for event in recent),
                    default=0,
                ),
            )

        # 轻量触发
        if light_count > 0 and (
            self._last_light_time is None
            or current_time - self._last_light_time >= 5.0
        ):
            affected = [e.get("uav_id", "") for e in recent
                       if e.get("uav_id", "") and e["type"] in light_types]
            return TriggerDecision(
                trigger_type="light",
                reason=_events_description(recent),
                source="event",
                affected_uavs=affected,
            )

        return TriggerDecision("none")

    def mark_triggered(self, trigger_type: str, time: float) -> None:
        if trigger_type == "heavy":
            self._last_heavy_time = time
            if self._heavy_retry_at is not None and time >= self._heavy_retry_at:
                self._heavy_retry_at = None
        elif trigger_type == "light":
            self._last_light_time = float(time)

    def schedule_heavy_retry(self, current_time: float, *, reason: str) -> None:
        """Schedule a failed model decision for one simulation minute later."""
        if (
            isinstance(current_time, bool)
            or not isinstance(current_time, (int, float))
            or not math.isfinite(float(current_time))
            or current_time < 0.0
        ):
            raise ValueError("current_time must be finite and non-negative")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        retry_at = float(current_time) + 1.0
        if self._heavy_retry_at is None or retry_at > self._heavy_retry_at:
            self._heavy_retry_at = retry_at
            self._heavy_retry_reason = reason.strip()

    def clear_heavy_retry(self) -> None:
        """Discard an automatic retry once an operator decision succeeds."""
        self._heavy_retry_at = None

    def _prune_event_dedup(self, current_time: float) -> None:
        for key, expires_at in tuple(self._event_dedup_expires.items()):
            if current_time >= expires_at:
                del self._event_dedup_expires[key]

from dataclasses import dataclass, field
from src.schedule.state_manager import StateManager


@dataclass
class TriggerDecision:
    trigger_type: str  # "light" | "heavy" | "none"
    reason: str = ""
    affected_uavs: list[str] = field(default_factory=list)


class TriggerManager:
    def __init__(self, sm: StateManager):
        self._sm = sm
        self._pending_events: list[dict] = []
        self._last_heavy_time: float = 0.0
        self._last_light_time: float = 0.0

    def notify_event(self, event_type: str, time: float, **kwargs) -> None:
        # Dedup: skip duplicate (event_type, uav_id) within a 5-min window
        # to prevent a single UAV from flooding the event queue with the
        # same event type (e.g. repeated storm_avoidance or fuel warnings).
        uav_id = kwargs.get("uav_id", "")
        if uav_id:
            for existing in self._pending_events:
                if (
                    existing["type"] == event_type
                    and existing.get("uav_id") == uav_id
                    and time - existing["time"] <= 5.0
                ):
                    return  # duplicate suppressed
        self._pending_events.append({
            "type": event_type,
            "time": time,
            **kwargs,
        })

    def check(self, current_time: float) -> TriggerDecision:
        """检查是否需要触发，返回决策。"""
        decision = self._check_events(current_time)
        if decision.trigger_type != "none":
            return decision

        # 周期定时（独立于事件）
        cycle = self._sm.config.llm.heavy_cycle_min
        if current_time >= cycle and current_time - self._last_heavy_time >= cycle:
            return TriggerDecision(
                trigger_type="heavy",
                reason=f"periodic {cycle}min cycle",
            )

        return TriggerDecision("none")

    def _check_events(self, current_time: float) -> TriggerDecision:
        """处理 pending 事件，返回基于事件的决策。"""
        if not self._pending_events:
            return TriggerDecision("none")

        # 过滤 5min 内的事件
        recent = [e for e in self._pending_events
                  if current_time - e["time"] <= 5.0]
        # All queued events are either processed now or stale; neither should
        # be reconsidered on the next simulation step.
        self._pending_events = []

        if not recent:
            return TriggerDecision("none")

        # Heavy: structural changes requiring LLM re-planning
        heavy_types = {
            "uav_returned",
            "target_found",
            "target_lost",
            "lifecycle_completed",
            # GOAL2: tracking resource released — need LLM to re-plan regions
            "target_departed",
            "civilian_released",
            "target_military",
            # GOAL2: dynamic environment — storms may open/block searchable area
            "storm_spawned",
            "storm_dissipated",
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

        heavy_count = sum(1 for e in recent if e["type"] in heavy_types)
        light_count = sum(1 for e in recent if e["type"] in light_types)

        # 重量触发条件：任一 heavy 事件，或 5min 内 >=3 个事件
        if heavy_count > 0 or heavy_count + light_count >= 3:
            affected = list(set(
                e.get("uav_id", "") for e in recent
                if e.get("uav_id", "")
            ))
            return TriggerDecision(
                trigger_type="heavy",
                reason=f"{heavy_count} heavy + {light_count} light events",
                affected_uavs=affected,
            )

        # 轻量触发
        if light_count > 0:
            affected = [e.get("uav_id", "") for e in recent
                       if e.get("uav_id", "") and e["type"] in light_types]
            return TriggerDecision(
                trigger_type="light",
                reason=f"{light_count} light events",
                affected_uavs=affected,
            )

        return TriggerDecision("none")

    def mark_triggered(self, trigger_type: str, time: float) -> None:
        if trigger_type == "heavy":
            self._last_heavy_time = time
        elif trigger_type == "light":
            self._last_light_time = time

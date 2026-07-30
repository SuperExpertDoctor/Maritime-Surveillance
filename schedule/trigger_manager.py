from dataclasses import dataclass, field
from schedule.state_manager import StateManager


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
        self._pending_events = [e for e in self._pending_events
                                if current_time - e["time"] > 5.0]

        if not recent:
            return TriggerDecision("none")

        heavy_types = {"uav_returned", "target_found", "target_lost"}
        light_types = {"search_complete", "uav_refueled"}

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

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


class TriggerManager:
    def __init__(self, sm: StateManager):
        self._sm = sm
        self._pending_events: list[dict] = []
        self._last_heavy_time: float = 0.0
        self._last_light_time: float = 0.0
        self._heavy_retry_at: float | None = None
        self._heavy_retry_reason: str = "decision_failed"
        self._last_checked_events: tuple[dict, ...] = ()

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
        decision = self._check_events(current_time)
        if decision.trigger_type != "none":
            return decision

        if self._heavy_retry_at is not None and current_time >= self._heavy_retry_at:
            reason = self._heavy_retry_reason
            self._heavy_retry_at = None
            return TriggerDecision(
                trigger_type="heavy",
                reason=f"retry after {reason}",
            )

        # The fleet begins with no approved SAR partition.  Waiting an entire
        # periodic cycle before the first real LLM decision strands every UAV
        # at its land base during the most valuable coverage window.
        if self._sm.cycle == 0 and current_time > 0.0:
            return TriggerDecision(
                trigger_type="heavy",
                reason="initial fleet deployment",
            )

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
                reason=f"{heavy_count} heavy + {light_count} light events",
                affected_uavs=affected,
                information_version=max(
                    (int(event.get("information_version", 0)) for event in recent),
                    default=0,
                ),
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
            if self._heavy_retry_at is not None and time >= self._heavy_retry_at:
                self._heavy_retry_at = None
        elif trigger_type == "light":
            self._last_light_time = time

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

"""Source-derived progressive surveillance state for environment vessels."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal


SurveillanceStage = Literal["undetected", "detected", "probing", "tracking"]

_PRIORITY = {"undetected": 0, "detected": 1, "probing": 2, "tracking": 3}
_SOURCE_STAGE = {
    "sar": "detected",
    "passive": "detected",
    "probe": "probing",
    "eo_lock": "tracking",
}


@dataclass(frozen=True)
class SurveillanceState:
    ship_id: str
    stage: SurveillanceStage
    revision: int
    changed_at_min: float
    cause_id: str


class SurveillanceStageRegistry:
    """Derive one stage from the active observation sources for each vessel."""

    def __init__(self) -> None:
        self._classes: dict[str, str] = {}
        self._facts: dict[str, dict[str, tuple[bool, str]]] = {}
        self._states: dict[str, SurveillanceState] = {}

    def register(
        self, ship_id: str, vessel_class: str, now_min: float,
    ) -> SurveillanceState:
        if not isinstance(ship_id, str) or not ship_id:
            raise ValueError("ship_id must be a non-empty string")
        if vessel_class not in {"type_i", "type_ii"}:
            raise ValueError("vessel_class must be type_i or type_ii")
        self._time(now_min)
        if ship_id in self._states:
            return self._states[ship_id]
        self._classes[ship_id] = vessel_class
        self._facts[ship_id] = {}
        state = SurveillanceState(ship_id, "undetected", 0, float(now_min), "register")
        self._states[ship_id] = state
        return state

    def set_fact(
        self,
        ship_id: str,
        source: str,
        active: bool,
        now_min: float,
        cause_id: str,
    ) -> SurveillanceState | None:
        if ship_id not in self._states:
            raise KeyError(ship_id)
        if source not in _SOURCE_STAGE:
            return None
        if source == "ais" or self._classes[ship_id] == "type_i":
            return None
        if type(active) is not bool:
            raise TypeError("active must be bool")
        self._time(now_min)
        if not isinstance(cause_id, str) or not cause_id:
            raise ValueError("cause_id must be a non-empty string")
        previous_fact = self._facts[ship_id].get(source)
        current_fact = (active, cause_id)
        if previous_fact == current_fact:
            return None
        if active:
            self._facts[ship_id][source] = current_fact
        else:
            self._facts[ship_id].pop(source, None)
        previous = self._states[ship_id]
        stage, selected_source, selected_cause = self._derive(ship_id)
        if stage == previous.stage and selected_cause == previous.cause_id:
            return None
        state = SurveillanceState(
            ship_id,
            stage,
            previous.revision + 1,
            float(now_min),
            selected_cause,
        )
        self._states[ship_id] = state
        return state

    def remove(self, ship_id: str) -> None:
        self._classes.pop(ship_id, None)
        self._facts.pop(ship_id, None)
        self._states.pop(ship_id, None)

    def snapshot(self, ship_id: str) -> SurveillanceState:
        return self._states[ship_id]

    def _derive(self, ship_id: str) -> tuple[SurveillanceStage, str | None, str]:
        active = self._facts[ship_id]
        if not active:
            return "undetected", None, "sources_cleared"
        source = max(
            active,
            key=lambda item: (_PRIORITY[_SOURCE_STAGE[item]], item),
        )
        return _SOURCE_STAGE[source], source, active[source][1]

    @staticmethod
    def _time(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("now_min must be finite and non-negative")
        value = float(value)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("now_min must be finite and non-negative")
        return value


__all__ = ["SurveillanceStage", "SurveillanceState", "SurveillanceStageRegistry"]

"""Source-derived progressive surveillance state for environment vessels.

The registry is now a thin adapter over ``StateKernel``: observation
sources are facts on the ``obs:<source>`` channel, the vessel class is a
``vessel_class`` fact pinned at registration, and ``_SurveillanceRule`` is
the domain rule that derives the stage. Public semantics are unchanged —
same revision behaviour, same error types, same snapshot shape.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from src.mission.state_kernel import Derivation, EntityState, Fact, StateKernel


SurveillanceStage = Literal["undetected", "detected", "probing", "tracking"]

_DOMAIN = "surveillance"
_PRIORITY = {"undetected": 0, "detected": 1, "probing": 2, "tracking": 3}
_SOURCE_STAGE = {
    "sar": "detected",
    "passive": "detected",
    "probe": "probing",
    # A live track task pins the stage even when the EO link flaps:
    # the aircraft still owns the target, so brief sensor dropouts must
    # not demote it back to probing and restart the probe pipeline.
    "track": "tracking",
    "eo_lock": "tracking",
}


@dataclass(frozen=True)
class SurveillanceState:
    ship_id: str
    stage: SurveillanceStage
    revision: int
    changed_at_min: float
    cause_id: str


class _SurveillanceRule:
    domain = _DOMAIN

    def evaluate(self, entity_id, facts, prior, now_min):
        vessel_class = facts.get("vessel_class")
        if vessel_class is not None and vessel_class.value == "type_i":
            return Derivation("undetected", "type_i_pinned")
        active = {
            channel[4:]: fact
            for channel, fact in facts.items()
            if channel.startswith("obs:") and fact.value is True
        }
        if not active:
            return Derivation("undetected", "sources_cleared")
        source = max(
            active,
            key=lambda item: (_PRIORITY[_SOURCE_STAGE[item]], item),
        )
        return Derivation(_SOURCE_STAGE[source], active[source].source_id)


class SurveillanceStageRegistry:
    """Derive one stage from the active observation sources for each vessel."""

    def __init__(self, kernel: StateKernel | None = None) -> None:
        self._kernel = kernel if kernel is not None else StateKernel()
        if not self._kernel.has_rule(_DOMAIN):
            self._kernel.register_rule(_SurveillanceRule())
        # Registration order index keeps snapshot/derive behaviour identical
        # to the pre-kernel store (entities are always registered before use).
        self._classes: dict[str, str] = {}

    def register(
        self, ship_id: str, vessel_class: str, now_min: float,
    ) -> SurveillanceState:
        if not isinstance(ship_id, str) or not ship_id:
            raise ValueError("ship_id must be a non-empty string")
        if vessel_class not in {"type_i", "type_ii"}:
            raise ValueError("vessel_class must be type_i or type_ii")
        self._time(now_min)
        if ship_id in self._classes:
            return self._entity(self._kernel.state(_DOMAIN, ship_id))
        self._classes[ship_id] = vessel_class
        entity = self._kernel.register_entity(
            _DOMAIN,
            ship_id,
            "undetected",
            now_min,
            "register",
            facts=(
                Fact(_DOMAIN, ship_id, "vessel_class", vessel_class, float(now_min), "register"),
            ),
        )
        return self._entity(entity)

    def set_fact(
        self,
        ship_id: str,
        source: str,
        active: bool,
        now_min: float,
        cause_id: str,
    ) -> SurveillanceState | None:
        if ship_id not in self._classes:
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
        entity = self._kernel.ingest(
            _DOMAIN, ship_id, f"obs:{source}", active, now_min, cause_id,
        )
        if entity is None:
            return None
        return self._entity(entity)

    def remove(self, ship_id: str) -> None:
        self._classes.pop(ship_id, None)
        self._kernel.remove(_DOMAIN, ship_id)

    def snapshot(self, ship_id: str) -> SurveillanceState:
        return self._entity(self._kernel.state(_DOMAIN, ship_id))

    @staticmethod
    def _entity(entity: EntityState) -> SurveillanceState:
        return SurveillanceState(
            entity.entity_id,
            entity.state,
            entity.revision,
            entity.changed_at_min,
            entity.cause_id,
        )

    @staticmethod
    def _time(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("now_min must be finite and non-negative")
        value = float(value)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("now_min must be finite and non-negative")
        return value


__all__ = ["SurveillanceStage", "SurveillanceState", "SurveillanceStageRegistry"]

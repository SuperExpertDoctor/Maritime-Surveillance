"""Airspeed-based phase separation for cooperative standoff tracking."""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


class PhaseCoordinator:
    def __init__(self, k_phase: float = 0.2, min_factor: float = 0.8, max_factor: float = 1.2):
        self.k_phase = k_phase
        self.min_factor = min_factor
        self.max_factor = max_factor

    def compute_phase_offsets(
        self,
        uavs_on_orbit: Iterable,
        target_position: Sequence[float] = (0.0, 0.0),
    ) -> list[float]:
        """Return wrapped errors to evenly spaced phases.

        Inputs may be numeric phase angles, ``(x,y,...)`` poses, dictionaries
        with ``position``, or objects exposing ``position``.
        """
        items = list(uavs_on_orbit)
        if not items:
            return []
        phases = [self._phase(item, target_position) for item in items]
        order = sorted(range(len(phases)), key=lambda idx: phases[idx])
        anchor = phases[order[0]]
        errors = [0.0] * len(phases)
        for rank, original_index in enumerate(order):
            desired = anchor + 2.0 * math.pi * rank / len(phases)
            errors[original_index] = _wrap_pi(desired - phases[original_index])
        return errors

    def adjust_airspeeds(self, phase_errors: Iterable[float], v_nominal: float) -> list[float]:
        if v_nominal <= 0:
            raise ValueError("v_nominal must be positive")
        return [
            v_nominal * max(
                self.min_factor,
                min(self.max_factor, 1.0 + self.k_phase * float(error) / math.pi),
            )
            for error in phase_errors
        ]

    @staticmethod
    def _phase(item, target: Sequence[float]) -> float:
        if isinstance(item, (int, float)):
            return float(item) % (2.0 * math.pi)
        if isinstance(item, dict):
            position = item.get("position", (0.0, 0.0))
        else:
            position = getattr(item, "position", item)
        x, y = float(position[0]), float(position[1])
        return math.atan2(y - float(target[1]), x - float(target[0])) % (2.0 * math.pi)


__all__ = ["PhaseCoordinator"]

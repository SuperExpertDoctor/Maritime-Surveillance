"""Passive signal observations: bearing-only reports and conditional positions."""

from __future__ import annotations

import hashlib
import math
import random

from src.mission.config import PassiveConfig
from src.mission.contracts import PassiveBearingObservation, PassivePosition


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:16]}"


class PassiveSensor:
    """One UAV's passive receiver. It never publishes range or power."""

    def __init__(self, config: PassiveConfig, *, seed: int | None = None,
                 rng: random.Random | None = None) -> None:
        if rng is not None and seed is not None:
            raise ValueError("provide either seed or rng")
        self.config = config
        self._rng = rng if rng is not None else random.Random(seed)

    def observe(
        self,
        sample_id: str,
        observer_uav_id: str,
        observer_position_cells: tuple[float, float],
        emitter_track_id: str,
        burst_id: str | None,
        emitter_position_cells: tuple[float, float],
        source_power_at_reference_db: float,
        observed_at_min: float,
        *,
        burst_active: bool = True,
    ) -> PassiveBearingObservation | None:
        """Return a bearing only after all environment-side gates pass."""
        if not burst_active or not burst_id:
            return None
        dx = float(emitter_position_cells[0]) - float(observer_position_cells[0])
        dy = float(emitter_position_cells[1]) - float(observer_position_cells[1])
        distance = math.hypot(dx, dy)
        if distance > self.config.detection_range_cells:
            # The hard range gate is before all stochastic draws.
            return None
        distance_for_power = max(distance, self.config.reference_distance_cells)
        received_power = (
            float(source_power_at_reference_db)
            - 20.0 * math.log10(distance_for_power / self.config.reference_distance_cells)
            + self._rng.gauss(0.0, self.config.received_power_std_db)
        )
        if received_power < self.config.minimum_received_power_db:
            return None
        probability = self.config.reference_detection_probability * math.exp(
            -((distance / self.config.range_scale_cells) ** 2)
        )
        if self._rng.random() >= probability:
            return None
        true_bearing = math.degrees(math.atan2(dy, dx)) % 360.0
        measured_bearing = (
            true_bearing + self._rng.gauss(0.0, self.config.bearing_std_deg)
        ) % 360.0
        observation_id = _stable_id(
            "OBS", sample_id, observer_uav_id, emitter_track_id, burst_id,
        )
        return PassiveBearingObservation(
            observation_id=observation_id,
            sample_id=sample_id,
            emitter_track_id=emitter_track_id,
            burst_id=burst_id,
            observed_at_min=float(observed_at_min),
            observer_uav_id=observer_uav_id,
            observer_position_cells=tuple(observer_position_cells),
            bearing_deg=measured_bearing,
            bearing_std_deg=self.config.bearing_std_deg,
        )


class PassivePositionResolver:
    """Apply the exact same-sample/source/burst multi-UAV release gate."""

    def release(
        self,
        observations: list[PassiveBearingObservation] | tuple[PassiveBearingObservation, ...],
        emitter_position_at_sample: tuple[float, float],
    ) -> PassivePosition | None:
        if not observations:
            return None
        groups = {
            (item.sample_id, item.emitter_track_id, item.burst_id)
            for item in observations
        }
        if len(groups) != 1:
            return None
        sample_id, emitter_track_id, burst_id = next(iter(groups))
        by_observer: dict[str, PassiveBearingObservation] = {}
        for item in sorted(observations, key=lambda value: (value.observer_uav_id, value.observation_id)):
            by_observer.setdefault(item.observer_uav_id, item)
        if len(by_observer) < 2:
            return None
        selected = tuple(by_observer[key] for key in sorted(by_observer))
        position_id = _stable_id("POS", sample_id, emitter_track_id, burst_id)
        return PassivePosition(
            position_id=position_id,
            emitter_track_id=emitter_track_id,
            burst_id=burst_id,
            sample_id=sample_id,
            observed_at_min=selected[0].observed_at_min,
            position_cells=tuple(emitter_position_at_sample),
            source_observation_ids=tuple(item.observation_id for item in selected),
        )


__all__ = ["PassivePositionResolver", "PassiveSensor"]

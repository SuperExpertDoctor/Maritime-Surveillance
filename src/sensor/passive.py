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
    """Triangulate noisy bearing reports after the multi-UAV release gate."""

    def __init__(self, *, association_radius_cells: float = 1.0) -> None:
        if not math.isfinite(association_radius_cells) or association_radius_cells <= 0.0:
            raise ValueError("association_radius_cells must be finite and positive")
        self.association_radius_cells = float(association_radius_cells)

    def release(
        self,
        observations: list[PassiveBearingObservation] | tuple[PassiveBearingObservation, ...],
        emitter_position_at_sample: tuple[float, float] | None = None,
    ) -> PassivePosition | None:
        """Release an estimated position; the optional truth argument is ignored.

        It remains in the signature for compatibility with older simulation
        callers, but published positions are derived only from measured
        observer poses and noisy bearings.
        """
        del emitter_position_at_sample
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
        position = self._triangulate(selected)
        if position is None:
            return None
        position_id = _stable_id("POS", sample_id, emitter_track_id, burst_id)
        return PassivePosition(
            position_id=position_id,
            emitter_track_id=emitter_track_id,
            burst_id=burst_id,
            sample_id=sample_id,
            observed_at_min=selected[0].observed_at_min,
            position_cells=position,
            source_observation_ids=tuple(item.observation_id for item in selected),
        )

    def _triangulate(
        self,
        observations: tuple[PassiveBearingObservation, ...],
    ) -> tuple[float, float] | None:
        """Solve the least-squares intersection of measured bearing rays."""
        normal_matrix = [[0.0, 0.0], [0.0, 0.0]]
        right_hand_side = [0.0, 0.0]
        directions: list[tuple[float, float]] = []
        for observation in observations:
            angle = math.radians(observation.bearing_deg)
            direction = (math.cos(angle), math.sin(angle))
            normal = (-direction[1], direction[0])
            origin = observation.observer_position_cells
            directions.append(direction)
            normal_matrix[0][0] += normal[0] * normal[0]
            normal_matrix[0][1] += normal[0] * normal[1]
            normal_matrix[1][0] += normal[1] * normal[0]
            normal_matrix[1][1] += normal[1] * normal[1]
            projection = normal[0] * origin[0] + normal[1] * origin[1]
            right_hand_side[0] += normal[0] * projection
            right_hand_side[1] += normal[1] * projection

        determinant = (
            normal_matrix[0][0] * normal_matrix[1][1]
            - normal_matrix[0][1] * normal_matrix[1][0]
        )
        if abs(determinant) <= 1e-9:
            return None
        position = (
            (
                right_hand_side[0] * normal_matrix[1][1]
                - normal_matrix[0][1] * right_hand_side[1]
            ) / determinant,
            (
                normal_matrix[0][0] * right_hand_side[1]
                - right_hand_side[0] * normal_matrix[1][0]
            ) / determinant,
        )
        for observation, direction in zip(observations, directions):
            origin = observation.observer_position_cells
            along_ray = (
                (position[0] - origin[0]) * direction[0]
                + (position[1] - origin[1]) * direction[1]
            )
            if along_ray < -self.association_radius_cells:
                return None
            cross_track = abs(
                (position[0] - origin[0]) * direction[1]
                - (position[1] - origin[1]) * direction[0]
            )
            if cross_track > self.association_radius_cells:
                return None
        return position


__all__ = ["PassivePositionResolver", "PassiveSensor"]

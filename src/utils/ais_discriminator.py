"""Position-based AIS military/civilian discriminator."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from src.env.ais_signal import AISSignal


@dataclass(frozen=True)
class EOMeasurement:
    relative_bearing_rad: float
    distance_cells: float


@dataclass(frozen=True)
class DiscriminateResult:
    is_military: bool
    confidence: float
    reason: str
    discrepancy_cells: float | None = None

    def to_dict(self) -> dict:
        return {
            "is_military": self.is_military,
            "confidence": self.confidence,
            "reason": self.reason,
            "discrepancy_cells": self.discrepancy_cells,
        }


class AISDiscriminator:
    def __init__(self, discrepancy_threshold_cells: float = 2.0):
        if discrepancy_threshold_cells <= 0:
            raise ValueError("AIS discrepancy threshold must be positive")
        self.discrepancy_threshold_cells = float(discrepancy_threshold_cells)

    @staticmethod
    def estimate_target_position(
        uav_pose: Sequence[float],
        eo_measurement: EOMeasurement | Mapping[str, float],
    ) -> tuple[float, float]:
        """Triangulate target coordinates from the EO bearing/range reading."""
        if isinstance(eo_measurement, Mapping):
            bearing = float(eo_measurement["relative_bearing_rad"])
            distance = float(eo_measurement["distance_cells"])
        else:
            bearing = eo_measurement.relative_bearing_rad
            distance = eo_measurement.distance_cells
        heading = float(uav_pose[2])
        return (
            float(uav_pose[0]) + distance * math.cos(heading + bearing),
            float(uav_pose[1]) + distance * math.sin(heading + bearing),
        )

    def discriminate(
        self,
        ais_signal: AISSignal | None,
        estimated_position: Sequence[float],
    ) -> DiscriminateResult:
        if ais_signal is None:
            return DiscriminateResult(True, 1.0, "AIS silent", None)
        discrepancy = math.dist(ais_signal.reported_position, estimated_position[:2])
        if discrepancy > self.discrepancy_threshold_cells:
            confidence = min(1.0, 0.7 + (discrepancy - self.discrepancy_threshold_cells) / 4.0)
            return DiscriminateResult(True, confidence, "AIS position discrepancy", discrepancy)
        confidence = max(0.7, 1.0 - discrepancy / max(self.discrepancy_threshold_cells * 2.0, 1e-9))
        return DiscriminateResult(False, confidence, "AIS position consistent", discrepancy)


__all__ = ["AISDiscriminator", "DiscriminateResult", "EOMeasurement"]

"""Electro-optical target visibility and visualisable field of view."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


@dataclass(frozen=True)
class FOVCone:
    origin: tuple[float, float]
    target: tuple[float, float]
    heading: float
    half_angle: float
    max_range: float
    polygon: tuple[tuple[float, float], ...]


class EOSensor:
    def __init__(self, fov_deg: float = 8.0, max_range_cells: float = 2.5):
        if not 0 < fov_deg < 180 or max_range_cells <= 0:
            raise ValueError("invalid EO field-of-view configuration")
        self.fov_deg = float(fov_deg)
        self.max_range_cells = float(max_range_cells)

    def is_target_visible(
        self,
        uav_position: Sequence[float],
        target_position: Sequence[float],
        max_range: float | None = None,
    ) -> bool:
        return math.dist(tuple(map(float, uav_position[:2])), tuple(map(float, target_position[:2]))) <= (
            self.max_range_cells if max_range is None else max_range
        )

    def compute_fov(
        self,
        uav_position: Sequence[float],
        heading: float,
        target_position: Sequence[float],
    ) -> FOVCone:
        origin = (float(uav_position[0]), float(uav_position[1]))
        target = (float(target_position[0]), float(target_position[1]))
        bearing = math.atan2(target[1] - origin[1], target[0] - origin[0])
        half = math.radians(self.fov_deg) / 2.0
        radius = min(self.max_range_cells, max(math.dist(origin, target) * 1.1, 0.25))
        left = (origin[0] + radius * math.cos(bearing - half), origin[1] + radius * math.sin(bearing - half))
        right = (origin[0] + radius * math.cos(bearing + half), origin[1] + radius * math.sin(bearing + half))
        return FOVCone(origin, target, bearing, half, radius, (origin, left, right))


__all__ = ["EOSensor", "FOVCone"]

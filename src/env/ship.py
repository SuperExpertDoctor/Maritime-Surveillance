"""Continuous target-ship model with bounded sinusoidal zigzag escape."""
from __future__ import annotations

import math
import random

from src.schedule.datatypes import GridCoord


class Ship:
    def __init__(
        self,
        ship_id: str,
        initial_position: GridCoord,
        speed_kn: float,
        zigzag_amplitude_km: float,
        zigzag_period_min: float,
        cell_size_km: float = 10.0,
    ):
        self.id = ship_id
        self._col, self._row = float(initial_position.col), float(initial_position.row)
        self.speed_kn = speed_kn
        self.speed_cells_per_min = speed_kn * 1.852 / 60.0 / cell_size_km
        self.zigzag_amplitude_cells = zigzag_amplitude_km / cell_size_km
        self.zigzag_period_min = zigzag_period_min
        self._detected = False
        self._phase = random.uniform(0, 2 * math.pi)
        self._base_heading = random.uniform(0, 2 * math.pi)
        self.group_id: str | None = None
        self.trail: list[tuple[float, float]] = []

    @property
    def position(self) -> GridCoord:
        return GridCoord(int(round(self._col)), int(round(self._row)))

    @position.setter
    def position(self, value: GridCoord) -> None:
        self._col, self._row = float(value.col), float(value.row)

    @property
    def float_position(self) -> tuple[float, float]:
        return self._col, self._row

    @property
    def detected(self) -> bool:
        return self._detected

    def mark_detected(self) -> None:
        self._detected = True

    def step(self, dt_min: float) -> None:
        if not self._detected or dt_min <= 0:
            return
        angular_rate = 2.0 * math.pi / max(self.zigzag_period_min, 1e-6)
        # Convert the sinusoidal cross-track derivative into a heading offset.
        lateral_velocity = self.zigzag_amplitude_cells * angular_rate * math.cos(self._phase)
        heading_offset = math.atan2(lateral_velocity, max(self.speed_cells_per_min, 1e-6))
        heading = self._base_heading + heading_offset
        self._col += self.speed_cells_per_min * math.cos(heading) * dt_min
        self._row += self.speed_cells_per_min * math.sin(heading) * dt_min
        self._phase = (self._phase + angular_rate * dt_min) % (2 * math.pi)

        bounced = False
        if self._col < 0 or self._col > 29:
            self._base_heading = math.pi - self._base_heading
            bounced = True
        if self._row < 0 or self._row > 29:
            self._base_heading = -self._base_heading
            bounced = True
        self._col = max(0.0, min(29.0, self._col))
        self._row = max(0.0, min(29.0, self._row))
        if bounced:
            self._phase = (self._phase + math.pi / 3.0) % (2 * math.pi)

        self.trail.append(self.float_position)
        if len(self.trail) > 120:
            self.trail.pop(0)


__all__ = ["Ship"]

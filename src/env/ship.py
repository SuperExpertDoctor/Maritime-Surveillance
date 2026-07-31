"""GOAL2 ship types, coherent formations, island avoidance, and departure."""
from __future__ import annotations

from enum import Enum
import math
import random
from typing import Iterable

from src.env.obstacle import Island
from src.schedule.datatypes import GridCoord


class ShipType(str, Enum):
    AIRCRAFT_CARRIER = "carrier"
    DESTROYER = "destroyer"


class Ship:
    def __init__(
        self,
        ship_id: str,
        initial_position: GridCoord,
        speed_kn: float,
        zigzag_amplitude_km: float,
        zigzag_period_min: float,
        cell_size_km: float = 10.0,
        *,
        ship_type: ShipType = ShipType.DESTROYER,
        group_id: str | None = None,
        base_heading: float | None = None,
        formation_offset: tuple[float, float] = (0.0, 0.0),
        actual_military: bool = True,
    ):
        self.id = ship_id
        self._col, self._row = float(initial_position.col), float(initial_position.row)
        self.speed_kn = float(speed_kn)
        self.speed_cells_per_min = self.speed_kn * 1.852 / 60.0 / cell_size_km
        self.zigzag_amplitude_cells = zigzag_amplitude_km / cell_size_km
        self.zigzag_period_min = zigzag_period_min
        self.ship_type = ShipType(ship_type)
        self.group_id = group_id
        self.formation_offset = tuple(map(float, formation_offset))
        self.actual_military = bool(actual_military)
        self.is_military: bool | None = None
        self.ais_signal = None
        self.discrimination = None
        self.estimated_position: tuple[float, float] | None = None
        self.departed = False
        self._detected = False
        self._being_tracked = False
        self._phase = random.uniform(0, 2 * math.pi)
        self._base_heading = random.uniform(0, 2 * math.pi) if base_heading is None else float(base_heading)
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

    @property
    def base_heading(self) -> float:
        return self._base_heading

    def mark_detected(self) -> None:
        self._detected = True

    def set_tracked(self, tracked: bool) -> None:
        self._being_tracked = bool(tracked)

    def set_ais_signal(self, signal) -> None:
        self.ais_signal = signal

    def _motion_heading(self) -> float:
        angular_rate = 2.0 * math.pi / max(self.zigzag_period_min, 1e-6)
        lateral_velocity = self.zigzag_amplitude_cells * angular_rate * math.cos(self._phase)
        return self._base_heading + math.atan2(lateral_velocity, max(self.speed_cells_per_min, 1e-6))

    def _is_safe_segment(self, start, end, islands: Iterable[Island]) -> bool:
        return not any(island.intersects_segment(start, end) for island in islands)

    def _avoid_islands(self, heading: float, dt_min: float, islands: Iterable[Island]) -> float:
        islands = list(islands)
        if not islands:
            return heading
        start = self.float_position
        distance = self.speed_cells_per_min * dt_min
        candidate = (start[0] + distance * math.cos(heading), start[1] + distance * math.sin(heading))
        if self._is_safe_segment(start, candidate, islands):
            return heading
        # Turn toward the first free lateral route; this is a formation-wide
        # heading change because the engine gives every member the same base
        # heading.  The small discrete set is sufficient at 10 km resolution.
        for delta in (math.pi / 2, -math.pi / 2, math.pi / 4, -math.pi / 4, math.pi):
            detour = self._base_heading + delta
            candidate = (start[0] + distance * math.cos(detour), start[1] + distance * math.sin(detour))
            if self._is_safe_segment(start, candidate, islands):
                self._base_heading = detour
                return detour
        return heading

    def step(self, dt_min: float, islands: Iterable[Island] = ()) -> None:
        """Advance one coherent-formation member while respecting islands."""
        if self.departed or dt_min <= 0:
            return
        heading = self._avoid_islands(self._motion_heading(), dt_min, islands)
        self._col += self.speed_cells_per_min * math.cos(heading) * dt_min
        self._row += self.speed_cells_per_min * math.sin(heading) * dt_min
        angular_rate = 2.0 * math.pi / max(self.zigzag_period_min, 1e-6)
        self._phase = (self._phase + angular_rate * dt_min) % (2 * math.pi)

        outside = self._col < -0.5 or self._col > 29.5 or self._row < -0.5 or self._row > 29.5
        if outside and self._detected and self._being_tracked:
            self.departed = True
        elif outside:
            if self._col < 0 or self._col > 29:
                self._base_heading = math.pi - self._base_heading
            if self._row < 0 or self._row > 29:
                self._base_heading = -self._base_heading
            self._col = max(0.0, min(29.0, self._col))
            self._row = max(0.0, min(29.0, self._row))
            self._phase = (self._phase + math.pi / 3.0) % (2 * math.pi)

        self.trail.append(self.float_position)
        if len(self.trail) > 120:
            self.trail.pop(0)


__all__ = ["Ship", "ShipType"]

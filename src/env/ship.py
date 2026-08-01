"""GOAL2 ship types, coherent formations, island avoidance, and departure."""
from __future__ import annotations

from enum import Enum
import math
import random
from typing import Iterable

from src.env.obstacle import Island
from src.schedule.datatypes import GridCoord


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


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
        zigzag_heading_deg: float = 18.0,
        max_turn_rate_deg_min: float = 12.0,
        yaw_time_constant_min: float = 2.5,
        heading_control_gain_per_min: float = 0.35,
        turn_speed_loss_fraction: float = 0.12,
    ):
        self.id = ship_id
        self._col, self._row = float(initial_position.col), float(initial_position.row)
        self.speed_kn = float(speed_kn)
        self.speed_cells_per_min = self.speed_kn * 1.852 / 60.0 / cell_size_km
        self.zigzag_amplitude_cells = zigzag_amplitude_km / cell_size_km
        self.zigzag_period_min = zigzag_period_min
        self.zigzag_heading_limit_rad = math.radians(zigzag_heading_deg)
        self.max_turn_rate_rad_per_min = math.radians(max_turn_rate_deg_min)
        self.yaw_time_constant_min = max(float(yaw_time_constant_min), 1e-3)
        self.heading_control_gain_per_min = max(float(heading_control_gain_per_min), 1e-3)
        self.turn_speed_loss_fraction = _clamp(float(turn_speed_loss_fraction), 0.0, 0.5)
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
        self._evasive = False
        self._phase = random.uniform(0, 2 * math.pi)
        self._base_heading = random.uniform(0, 2 * math.pi) if base_heading is None else float(base_heading)
        self.heading_rad = self._base_heading
        self._yaw_rate_rad_per_min = 0.0
        direction_key = group_id or ship_id
        self._evasion_direction = 1.0 if sum(map(ord, direction_key)) % 2 else -1.0
        self._avoidance_heading: float | None = None
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

    @property
    def is_evading(self) -> bool:
        return self._evasive

    @property
    def turn_rate_deg_min(self) -> float:
        return math.degrees(self._yaw_rate_rad_per_min)

    @property
    def surface_search_radar_range_cells(self) -> float:
        """Sea-search radar footprint rendered around the target vessel."""
        return 4.0 if self.ship_type is ShipType.AIRCRAFT_CARRIER else 3.0

    def set_tracked(self, tracked: bool) -> None:
        tracked = bool(tracked)
        if tracked and not self._being_tracked:
            # All members of one formation use the same deterministic phase
            # and direction, while the yaw model prevents an instantaneous turn.
            self._phase = 0.0
            self._evasive = True
        self._being_tracked = tracked

    def set_ais_signal(self, signal) -> None:
        self.ais_signal = signal

    def _motion_heading(self) -> float:
        if not self._evasive:
            return self._base_heading
        angular_rate = 2.0 * math.pi / max(self.zigzag_period_min, 1e-6)
        requested_offset = math.atan2(
            self.zigzag_amplitude_cells * angular_rate,
            max(self.speed_cells_per_min, 1e-6),
        )
        course_amplitude = min(abs(requested_offset), self.zigzag_heading_limit_rad)
        return self._base_heading + self._evasion_direction * course_amplitude * math.sin(self._phase)

    def _is_safe_segment(self, start, end, islands: Iterable[Island]) -> bool:
        return not any(island.intersects_segment(start, end) for island in islands)

    def _avoid_islands(self, heading: float, dt_min: float, islands: Iterable[Island]) -> float:
        islands = list(islands)
        if not islands:
            self._avoidance_heading = None
            return heading
        start = self.float_position
        # Start a gradual turn well before the next one-minute segment reaches
        # land. The look-ahead is deliberately larger than one coarse grid cell.
        distance = max(self.speed_cells_per_min * dt_min, 1.5)
        candidate = (start[0] + distance * math.cos(heading), start[1] + distance * math.sin(heading))
        if self._is_safe_segment(start, candidate, islands):
            self._avoidance_heading = None
            return heading
        if self._avoidance_heading is not None:
            detour = self._avoidance_heading
            candidate = (start[0] + distance * math.cos(detour), start[1] + distance * math.sin(detour))
            if self._is_safe_segment(start, candidate, islands):
                return detour
        for delta in (math.pi / 4, -math.pi / 4, math.pi / 2, -math.pi / 2, math.pi):
            detour = heading + delta
            candidate = (start[0] + distance * math.cos(detour), start[1] + distance * math.sin(detour))
            if self._is_safe_segment(start, candidate, islands):
                self._avoidance_heading = detour
                return detour
        return heading

    def _avoid_boundaries(self, heading: float) -> float:
        if self._detected and self._being_tracked:
            return heading
        distance = max(1.0, self.speed_cells_per_min * 10.0)
        projected_col = self._col + distance * math.cos(heading)
        projected_row = self._row + distance * math.sin(heading)
        reflected = heading
        if projected_col < 0.0 or projected_col > 29.0:
            reflected = math.pi - reflected
            self._base_heading = math.pi - self._base_heading
        if projected_row < 0.0 or projected_row > 29.0:
            reflected = -reflected
            self._base_heading = -self._base_heading
        return reflected

    def _integrate_yaw(self, desired_heading: float, dt_min: float) -> float:
        old_heading = self.heading_rad
        old_rate = self._yaw_rate_rad_per_min
        error = _wrap_pi(desired_heading - old_heading)
        commanded_rate = _clamp(
            self.heading_control_gain_per_min * error,
            -self.max_turn_rate_rad_per_min,
            self.max_turn_rate_rad_per_min,
        )
        response = 1.0 - math.exp(-dt_min / self.yaw_time_constant_min)
        new_rate = old_rate + response * (commanded_rate - old_rate)
        new_rate = _clamp(
            new_rate,
            -self.max_turn_rate_rad_per_min,
            self.max_turn_rate_rad_per_min,
        )
        delta = 0.5 * (old_rate + new_rate) * dt_min
        if delta * error > 0.0 and abs(delta) > abs(error):
            delta = error
            new_rate = 0.0
        self.heading_rad = _wrap_pi(old_heading + delta)
        self._yaw_rate_rad_per_min = new_rate
        return _wrap_pi(old_heading + delta / 2.0)

    def step(self, dt_min: float, islands: Iterable[Island] = ()) -> None:
        """Advance one coherent-formation member while respecting islands."""
        if self.departed or dt_min <= 0:
            return
        start = self.float_position
        desired_heading = self._avoid_boundaries(self._motion_heading())
        desired_heading = self._avoid_islands(desired_heading, dt_min, islands)
        motion_heading = self._integrate_yaw(desired_heading, dt_min)
        turn_fraction = abs(self._yaw_rate_rad_per_min) / max(self.max_turn_rate_rad_per_min, 1e-9)
        effective_speed = self.speed_cells_per_min * (1.0 - self.turn_speed_loss_fraction * turn_fraction)
        self._col += effective_speed * math.cos(motion_heading) * dt_min
        self._row += effective_speed * math.sin(motion_heading) * dt_min
        angular_rate = 2.0 * math.pi / max(self.zigzag_period_min, 1e-6)
        if self._evasive:
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

        self.trail.append(self.float_position)
        if len(self.trail) > 120:
            self.trail.pop(0)


__all__ = ["Ship", "ShipType"]

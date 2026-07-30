"""Continuous-pose fixed-wing UAV entity with mission-aware sensors."""
from __future__ import annotations

import math
from typing import Sequence

from src.env.dubins import Pose
from src.env.eo_sensor import EOSensor, FOVCone
from src.env.sar_sensor import SARSensor
from src.schedule.datatypes import BBox, GridCoord
from src.utils.track_orbit import LGVFTracker


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


class UAVEntity:
    def __init__(
        self,
        uav_id: str,
        base_position: GridCoord,
        endurance_h: float,
        cruise_speed_kmh: float,
        cell_size_km: float = 10.0,
        R_min: float = 1.0,
    ):
        self.id = uav_id
        self._col = float(base_position.col)
        self._row = float(base_position.row)
        self._base_col = float(base_position.col)
        self._base_row = float(base_position.row)
        self.heading_rad = -math.pi / 2.0
        self.endurance_h = endurance_h
        self.cruise_speed_kmh = cruise_speed_kmh
        self.cell_size_km = cell_size_km
        self.R_min = R_min
        self.fuel_remaining_pct = 1.0
        self.status = "idle"
        self.sensor_mode = "off"
        self.assigned_region: BBox | None = None
        self.target_group_id: str | None = None
        self.waypoints: list[Pose] = []
        self.planned_path: list[Pose] = []
        self.trail: list[tuple[float, float]] = []
        self._wp_index = 0
        self._transit_end_index = 0
        self._scan_ranges: list[tuple[int, int, str]] = []
        self.sar_look_direction = "right"
        self.sar_footprint: list[GridCoord] = []
        self.eo_fov: FOVCone | None = None
        self.search_complete_pending = False
        self.completed_searches_since_refuel = 0
        self._mission_kind = ""
        self._fuel_low_reported = False
        self._fuel_consumption_rate = 1.0 / (endurance_h * 60.0)
        self.lgvf = LGVFTracker(R_min=R_min)
        self.sar_sensor = SARSensor()
        self.eo_sensor = EOSensor()

    @property
    def position(self) -> GridCoord:
        return GridCoord(
            int(max(0, min(29, round(self._col)))),
            int(max(0, min(29, round(self._row)))),
        )

    @position.setter
    def position(self, value: GridCoord) -> None:
        self._col, self._row = float(value.col), float(value.row)

    @property
    def float_position(self) -> tuple[float, float]:
        return self._col, self._row

    @property
    def pose(self) -> Pose:
        return self._col, self._row, self.heading_rad

    @property
    def heading_deg(self) -> float:
        return math.degrees(self.heading_rad) % 360.0

    @property
    def mission_kind(self) -> str:
        return self._mission_kind

    @property
    def remaining_path(self) -> list[Pose]:
        if self._wp_index >= len(self.waypoints):
            return [self.pose]
        return [self.pose, *self.waypoints[self._wp_index:]]

    @property
    def base_position(self) -> GridCoord:
        return GridCoord(int(self._base_col), int(self._base_row))

    @base_position.setter
    def base_position(self, value: GridCoord) -> None:
        self._base_col, self._base_row = float(value.col), float(value.row)

    def assign_mission(
        self,
        region_bbox: BBox,
        waypoints: Sequence[Sequence[float] | GridCoord],
        transit_end_index: int | None = None,
        scan_ranges: Sequence[tuple[int, int, str]] | None = None,
    ) -> None:
        self.assigned_region = region_bbox
        self.target_group_id = None
        self._mission_kind = "search"
        self._set_route(waypoints)
        self._transit_end_index = max(0, transit_end_index if transit_end_index is not None else 1)
        self._scan_ranges = list(scan_ranges or [])
        self.status = "transit"
        self.sensor_mode = "off"
        self.search_complete_pending = False

    def start_tracking(self, target_group_id: str, target_position: Sequence[float], R_d: float = 1.8) -> None:
        entry = self.lgvf.plan_entry(self.pose, target_position, R_d)
        self.target_group_id = target_group_id
        self._mission_kind = "track_entry"
        self._set_route(entry.waypoints)
        self._transit_end_index = len(self.waypoints) - 1
        self.status = "transit"
        self.sensor_mode = "off"

    def plan_return(self, waypoints: Sequence[Sequence[float]]) -> None:
        self._mission_kind = "return"
        self._set_route(waypoints)
        self._transit_end_index = len(self.waypoints) - 1
        self.status = "returning"
        self.sensor_mode = "off"
        self.sar_footprint = []
        self.eo_fov = None

    def _set_route(self, waypoints: Sequence[Sequence[float] | GridCoord]) -> None:
        route: list[Pose] = []
        for waypoint in waypoints:
            if hasattr(waypoint, "col"):
                x, y = float(waypoint.col), float(waypoint.row)
                heading = route[-1][2] if route else self.heading_rad
            else:
                x, y = float(waypoint[0]), float(waypoint[1])
                heading = float(waypoint[2]) if len(waypoint) >= 3 else (
                    route[-1][2] if route else self.heading_rad
                )
            route.append((x, y, _wrap_pi(heading)))
        if not route or math.dist(route[0][:2], self.float_position) > 1e-6:
            route.insert(0, self.pose)
        self.waypoints = route
        self.planned_path = list(route)
        self._wp_index = 1 if len(route) > 1 else len(route)

    def step(
        self,
        dt_min: float,
        target_position: Sequence[float] | None = None,
        tracking_speed_cells_min: float | None = None,
    ) -> bool:
        """Advance the vehicle and report a newly reached low-fuel threshold."""
        if dt_min <= 0:
            return False
        if self.status not in ("idle", "refueling"):
            self.fuel_remaining_pct = max(
                0.0,
                self.fuel_remaining_pct - self._fuel_consumption_rate * dt_min,
            )

        if self.status == "tracking" and target_position is not None:
            self._step_tracking(dt_min, target_position, tracking_speed_cells_min)
        else:
            self._follow_route(dt_min)

        self.trail.append(self.float_position)
        if len(self.trail) > 240:
            self.trail.pop(0)

        if (
            self.fuel_remaining_pct <= 0.08
            and self.status not in ("returning", "idle", "refueling")
            and not self._fuel_low_reported
        ):
            self._fuel_low_reported = True
            return True
        return False

    def _follow_route(self, dt_min: float) -> None:
        remaining = self.cruise_speed_kmh / self.cell_size_km / 60.0 * dt_min
        while remaining > 1e-9 and self._wp_index < len(self.waypoints):
            target = self.waypoints[self._wp_index]
            distance = math.dist(self.float_position, target[:2])
            if distance <= 1e-9:
                self.heading_rad = target[2]
                self._wp_index += 1
                self._update_route_state()
                continue
            if remaining >= distance:
                self._col, self._row, self.heading_rad = target
                remaining -= distance
                self._wp_index += 1
                self._update_route_state()
            else:
                ratio = remaining / distance
                heading_delta = _wrap_pi(target[2] - self.heading_rad)
                self._col += (target[0] - self._col) * ratio
                self._row += (target[1] - self._row) * ratio
                self.heading_rad = _wrap_pi(self.heading_rad + heading_delta * ratio)
                remaining = 0.0
        self._update_scan_direction()

    def _update_route_state(self) -> None:
        if self._mission_kind == "search" and self._wp_index > self._transit_end_index:
            self.status = "searching"
            self.sensor_mode = "sar"
        elif self._mission_kind == "track_entry" and self._wp_index >= len(self.waypoints):
            self.status = "tracking"
            self.sensor_mode = "eo"
        elif self._mission_kind == "return" and self._wp_index >= len(self.waypoints):
            self.status = "refueling"
            self.sensor_mode = "off"

        if self._mission_kind == "search" and self._wp_index >= len(self.waypoints):
            self.status = "idle"
            self.sensor_mode = "off"
            self.search_complete_pending = True
            self.sar_footprint = []

    def _update_scan_direction(self) -> None:
        if self._mission_kind != "search" or self._wp_index >= len(self.waypoints):
            return
        route_index = max(0, self._wp_index - 1)
        for start, end, look in self._scan_ranges:
            if start <= route_index <= end:
                self.sar_look_direction = look
                self.status = "searching"
                self.sensor_mode = "sar"
                return
        self.status = "transit"
        self.sensor_mode = "off"
        self.sar_footprint = []

    def _step_tracking(
        self,
        dt_min: float,
        target_position: Sequence[float],
        speed_command: float | None = None,
    ) -> None:
        speed = speed_command or self.cruise_speed_kmh / self.cell_size_km / 60.0
        rate, speed = self.lgvf.compute_guidance(self.pose, target_position, 1.8, speed)
        mid = self.heading_rad + rate * dt_min / 2.0
        self._col += speed * math.cos(mid) * dt_min
        self._row += speed * math.sin(mid) * dt_min
        self.heading_rad = _wrap_pi(self.heading_rad + rate * dt_min)
        self._col = max(0.0, min(29.0, self._col))
        self._row = max(0.0, min(29.0, self._row))
        self.eo_fov = self.eo_sensor.compute_fov(
            self.float_position, self.heading_rad, target_position
        )

    def refuel(self) -> None:
        self.fuel_remaining_pct = 1.0
        self.status = "idle"
        self.sensor_mode = "off"
        self._fuel_low_reported = False
        self.completed_searches_since_refuel = 0
        self.target_group_id = None
        self.assigned_region = None
        self.waypoints = []
        self.planned_path = []


__all__ = ["UAVEntity"]

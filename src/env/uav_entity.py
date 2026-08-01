"""Continuous-pose fixed-wing UAV entity with mission-aware sensors."""
from __future__ import annotations

import math
from typing import Sequence

from src.env.dubins import Pose
from src.env.eo_sensor import EOSensor, FOVCone
from src.env.sar_sensor import SARSensor
from src.schedule.datatypes import BBox, GridCoord
from src.utils.ais_discriminator import EOMeasurement
from src.utils.storm_avoider import StormAvoider, ThreatLevel
from src.utils.track_orbit import LGVFTracker


# The standard mission lasts eight hours at one simulation sample per minute.
# Keep its full flight history so the "full" trail mode is not silently truncated.
MAX_VISUAL_TRAIL_POINTS = 480


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
        self.mission_route: list[Pose] = []  # GOAL2: full route persisted for viz even after waypoint consumption
        self._wp_index = 0
        self._transit_end_index = 0
        self._scan_ranges: list[tuple[int, int, str]] = []
        self.sar_look_direction = "right"
        # A stripmap image is formed by coherently integrating successive
        # side-looking pulses along a stable track.  The instantaneous
        # footprint is intentionally short; the visible aperture history is
        # retained separately for the renderer and diagnostics.
        self.sar_along_track_cells = 0.8
        self.sar_aperture_length_cells = 5.0
        self.sar_heading_tolerance_rad = math.radians(2.0)
        self.sar_imaging = False
        self.sar_scan_heading_rad: float | None = None
        self.sar_heading_error_deg = 0.0
        self.sar_aperture_track: list[tuple[float, float]] = []
        self.sar_footprint: list[GridCoord] = []
        self.eo_fov: FOVCone | None = None
        self.search_complete_pending = False
        self.completed_searches_since_refuel = 0
        self._mission_kind = ""
        self._fuel_low_reported = False
        self.fuel_warning_sent = False  # GOAL2: proactive 25% fuel warning flag
        self._holding_center: tuple[float, float] | None = None
        self.avoidance_level = 0
        self.avoidance_path: list[Pose] = []
        self._avoidance_index = 0
        self._distance_this_step = 0.0
        self.lgvf = LGVFTracker(R_min=R_min)
        self.sar_sensor = SARSensor()
        self.eo_sensor = EOSensor()
        self.storm_avoider = StormAvoider(eo_detection_range_cells=self.eo_sensor.max_range_cells)

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
    def home_base_grid(self) -> tuple[float, float]:
        """Grid coordinates of the base this UAV is assigned to (for viz path rendering)."""
        return self._base_col, self._base_row

    @property
    def heading_deg(self) -> float:
        return math.degrees(self.heading_rad) % 360.0

    @property
    def total_range_km(self) -> float:
        """Fixed sortie range determined by the airframe and fuel load."""
        return self.cruise_speed_kmh * self.endurance_h

    @property
    def remaining_range_km(self) -> float:
        return self.fuel_remaining_pct * self.total_range_km

    @property
    def remaining_range_cells(self) -> float:
        return self.remaining_range_km / self.cell_size_km

    @property
    def total_range_cells(self) -> float:
        return self.total_range_km / self.cell_size_km

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
        self._clear_sar_acquisition()
        self.search_complete_pending = False

    def start_tracking(self, target_group_id: str, target_position: Sequence[float], R_d: float = 1.8) -> None:
        entry = self.lgvf.plan_entry(self.pose, target_position, R_d)
        self.target_group_id = target_group_id
        self._mission_kind = "track_entry"
        self._set_route(entry.waypoints)
        self._transit_end_index = len(self.waypoints) - 1
        self.status = "transit"
        self.sensor_mode = "off"
        self._clear_sar_acquisition()

    def cancel_tracking(self) -> None:
        """Release a classified civilian/departed target for search reuse."""
        self.target_group_id = None
        self.assigned_region = None
        self._mission_kind = ""
        self.waypoints = []
        self.planned_path = []
        self._wp_index = 0
        self.status = "idle"
        self.sensor_mode = "off"
        self._clear_sar_acquisition()
        self.eo_fov = None
        self.avoidance_level = 0
        self.avoidance_path = []
        self._avoidance_index = 0

    def measure_target(
        self,
        target_position: Sequence[float],
        storms=(),
    ) -> EOMeasurement | None:
        """Return the EO bearing/range observation, or None when obscured."""
        if not self.eo_sensor.is_target_visible(self.float_position, target_position):
            return None
        if any(
            storm.contains(self.float_position) or storm.contains(target_position)
            for storm in storms
        ):
            return None
        bearing = math.atan2(
            float(target_position[1]) - self._row,
            float(target_position[0]) - self._col,
        )
        return EOMeasurement(
            relative_bearing_rad=_wrap_pi(bearing - self.heading_rad),
            distance_cells=math.dist(self.float_position, target_position[:2]),
        )

    def plan_return(self, waypoints: Sequence[Sequence[float]]) -> None:
        self._mission_kind = "return"
        self._set_route(waypoints)
        self._transit_end_index = len(self.waypoints) - 1
        self.status = "returning"
        self.sensor_mode = "off"
        self.sar_footprint = []
        self._clear_sar_acquisition()
        self.eo_fov = None
        self._holding_center = None
        self.avoidance_level = 0
        self.avoidance_path = []
        self._avoidance_index = 0

    def start_holding(self, base_position: GridCoord | Sequence[float]) -> None:
        """Orbit a full recovery base until a refuelling slot opens."""
        self._holding_center = (float(base_position[0]), float(base_position[1]))
        self._mission_kind = "holding"
        self.waypoints = []
        self.planned_path = []
        self._wp_index = 0
        self.status = "holding"
        self.sensor_mode = "off"
        self.sar_footprint = []
        self._clear_sar_acquisition()
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
        self.mission_route = list(route)  # GOAL2: persisted full route for viz
        self._wp_index = 1 if len(route) > 1 else len(route)

    def step(
        self,
        dt_min: float,
        target_position: Sequence[float] | None = None,
        tracking_speed_cells_min: float | None = None,
        storm_zones=(),
    ) -> bool:
        """Advance the vehicle and report a newly reached low-fuel threshold."""
        if dt_min <= 0:
            return False
        airborne = self.status not in ("idle", "refueling")
        self._distance_this_step = 0.0

        if self.status == "tracking" and target_position is not None:
            self._step_tracking(dt_min, target_position, tracking_speed_cells_min, storm_zones)
        elif self.status == "holding" and self._holding_center is not None:
            self._step_holding(dt_min)
        else:
            self._follow_route(dt_min)

        if airborne:
            # Fuel represents a fixed mileage budget, not a clock budget.
            # Accumulate the actual route/orbit segments rather than the
            # chord between two simulation ticks, which undercounts turns.
            self.fuel_remaining_pct = max(
                0.0,
                self.fuel_remaining_pct
                - self._distance_this_step / self.total_range_cells,
            )

        self.trail.append(self.float_position)
        if len(self.trail) > MAX_VISUAL_TRAIL_POINTS:
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
                self._distance_this_step += distance
                self._wp_index += 1
                self._update_route_state()
            else:
                ratio = remaining / distance
                heading_delta = _wrap_pi(target[2] - self.heading_rad)
                self._col += (target[0] - self._col) * ratio
                self._row += (target[1] - self._row) * ratio
                self.heading_rad = _wrap_pi(self.heading_rad + heading_delta * ratio)
                self._distance_this_step += remaining
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
            self._clear_sar_acquisition()

    def _update_scan_direction(self) -> None:
        if self._mission_kind != "search" or self._wp_index >= len(self.waypoints):
            self._clear_sar_acquisition()
            return
        route_index = max(0, self._wp_index - 1)
        for start, end, look in self._scan_ranges:
            if start <= route_index <= end:
                self.sar_look_direction = look
                # The scan ranges are produced from the planner's straight
                # legs.  Do not start collecting at either connector point:
                # first establish the commanded heading, then accumulate a
                # coherent synthetic aperture while it remains stable.
                heading_index = min(max(start + 1, route_index), end)
                desired_heading = self.waypoints[heading_index][2]
                heading_error = abs(_wrap_pi(self.heading_rad - desired_heading))
                self.sar_scan_heading_rad = desired_heading
                self.sar_heading_error_deg = math.degrees(heading_error)
                stable_leg = start < route_index < end
                self.sar_imaging = (
                    stable_leg
                    and heading_error <= self.sar_heading_tolerance_rad
                    and self.avoidance_level == 0
                )
                if self.sar_imaging:
                    self.status = "searching"
                    self.sensor_mode = "sar"
                    self._append_sar_aperture_position()
                else:
                    self.status = "transit"
                    self.sensor_mode = "off"
                    self.sar_footprint = []
                    self.sar_aperture_track = []
                return
        self.status = "transit"
        self.sensor_mode = "off"
        self.sar_footprint = []
        self._clear_sar_acquisition()

    def _append_sar_aperture_position(self) -> None:
        """Keep only the recent, straight-line synthetic aperture path."""
        position = self.float_position
        if not self.sar_aperture_track:
            self.sar_aperture_track.append(position)
            return
        if math.dist(position, self.sar_aperture_track[-1]) <= 1e-6:
            return
        self.sar_aperture_track.append(position)
        while (
            len(self.sar_aperture_track) > 1
            and self._aperture_length() > self.sar_aperture_length_cells
        ):
            self.sar_aperture_track.pop(0)

    def _aperture_length(self) -> float:
        return sum(
            math.dist(start, end)
            for start, end in zip(
                self.sar_aperture_track,
                self.sar_aperture_track[1:],
            )
        )

    def _clear_sar_acquisition(self) -> None:
        self.sar_imaging = False
        self.sar_scan_heading_rad = None
        self.sar_heading_error_deg = 0.0
        self.sar_aperture_track = []

    def _step_tracking(
        self,
        dt_min: float,
        target_position: Sequence[float],
        speed_command: float | None = None,
        storm_zones=(),
    ) -> None:
        speed = speed_command or self.cruise_speed_kmh / self.cell_size_km / 60.0
        storms = list(storm_zones)
        assessment = self.storm_avoider.detect_threat(
            self.pose, target_position, storms, speed, dt_min, 1.8,
        )
        self.avoidance_level = int(assessment.level)
        if assessment.level is ThreatLevel.LEVEL_2:
            if not self.avoidance_path or self._avoidance_index >= len(self.avoidance_path):
                path = self.storm_avoider.plan_avoidance(
                    self.pose, target_position, storms, self.R_min,
                )
                if path:
                    self.avoidance_path = path
                    self._avoidance_index = 1
            if self.avoidance_path and self._avoidance_index < len(self.avoidance_path):
                self._follow_avoidance_route(dt_min, target_position, storms)
                return
            # There is no curvature-constrained route that both avoids the
            # cloud and keeps EO range.  Safety takes priority over tracking.
            assessment = type(assessment)(ThreatLevel.LEVEL_3, assessment.storm, assessment.distance_cells)
            self.avoidance_level = int(assessment.level)
        if assessment.level is ThreatLevel.LEVEL_3:
            self.avoidance_path = []
            self._avoidance_index = 0
            self.sensor_mode = "off"
            self.eo_fov = None
            storm = assessment.storm
            safe_radius = float(getattr(storm, "half_extent", 1.0)) + 1.5
            rate, speed = self.lgvf.compute_guidance(
                self.pose, storm.center, safe_radius, speed,
                storm_zones=storms,
            )
            self._integrate_guidance(rate, speed, dt_min)
            self._enforce_storm_clearance(storms)
            return
        else:
            self.avoidance_path = []
            self._avoidance_index = 0
        desired_radius = 1.8
        if assessment.level is ThreatLevel.LEVEL_1:
            radial = (
                self._col - float(target_position[0]),
                self._row - float(target_position[1]),
            )
            storm_relative = (
                float(assessment.storm.center[0]) - float(target_position[0]),
                float(assessment.storm.center[1]) - float(target_position[1]),
            )
            # Expand the orbit away from an inner cloud, but contract it when
            # the cloud is on the outward side of the current orbit.
            desired_radius = 1.25 if radial[0] * storm_relative[0] + radial[1] * storm_relative[1] > 0 else 2.4
        rate, speed = self.lgvf.compute_guidance(
            self.pose,
            target_position,
            desired_radius,
            speed,
            storm_zones=storms,
        )
        self._integrate_guidance(rate, speed, dt_min)
        self._enforce_storm_clearance(storms)
        self.eo_fov = self.eo_sensor.compute_fov(
            self.float_position, self.heading_rad, target_position
        )

    def _integrate_guidance(self, rate: float, speed: float, dt_min: float) -> None:
        mid = self.heading_rad + rate * dt_min / 2.0
        self._col += speed * math.cos(mid) * dt_min
        self._row += speed * math.sin(mid) * dt_min
        self.heading_rad = _wrap_pi(self.heading_rad + rate * dt_min)
        self._col = max(0.0, min(29.0, self._col))
        self._row = max(0.0, min(29.0, self._row))
        self._distance_this_step += abs(speed) * dt_min

    def _follow_avoidance_route(
        self,
        dt_min: float,
        target_position: Sequence[float],
        storms,
    ) -> None:
        remaining = self.cruise_speed_kmh / self.cell_size_km / 60.0 * dt_min
        while remaining > 1e-9 and self._avoidance_index < len(self.avoidance_path):
            target = self.avoidance_path[self._avoidance_index]
            distance = math.dist(self.float_position, target[:2])
            if distance <= 1e-9:
                self.heading_rad = target[2]
                self._avoidance_index += 1
                continue
            if remaining >= distance:
                self._col, self._row, self.heading_rad = target
                remaining -= distance
                self._distance_this_step += distance
                self._avoidance_index += 1
            else:
                ratio = remaining / distance
                self._col += (target[0] - self._col) * ratio
                self._row += (target[1] - self._row) * ratio
                self.heading_rad = _wrap_pi(
                    self.heading_rad + _wrap_pi(target[2] - self.heading_rad) * ratio
                )
                self._distance_this_step += remaining
                remaining = 0.0
        self.eo_fov = self.eo_sensor.compute_fov(
            self.float_position, self.heading_rad, target_position
        )
        self._enforce_storm_clearance(storms)

    def _enforce_storm_clearance(self, storms) -> None:
        """Last-resort guard against a dynamic cloud crossing a trajectory."""
        for storm in storms:
            margin = self.storm_avoider.safety_margin_cells
            if not storm.contains(self.float_position, margin):
                continue
            dx = self._col - storm.center[0]
            dy = self._row - storm.center[1]
            extent = storm.half_extent + margin + 1e-4
            if abs(dx) >= abs(dy):
                self._col = storm.center[0] + (extent if dx >= 0 else -extent)
            else:
                self._row = storm.center[1] + (extent if dy >= 0 else -extent)

    def _step_holding(self, dt_min: float) -> None:
        speed = self.cruise_speed_kmh / self.cell_size_km / 60.0
        rate, commanded_speed = self.lgvf.compute_guidance(
            self.pose, self._holding_center, 1.2, speed
        )
        self._integrate_guidance(rate, commanded_speed, dt_min)

    def refuel(self) -> None:
        self.fuel_remaining_pct = 1.0
        self.status = "idle"
        self.sensor_mode = "off"
        self._clear_sar_acquisition()
        self._fuel_low_reported = False
        self.fuel_warning_sent = False  # GOAL2: reset proactive warning
        self.completed_searches_since_refuel = 0
        self.target_group_id = None
        self.assigned_region = None
        self.waypoints = []
        self.planned_path = []
        self._holding_center = None
        self.avoidance_level = 0
        self.avoidance_path = []
        self._avoidance_index = 0


__all__ = ["UAVEntity"]

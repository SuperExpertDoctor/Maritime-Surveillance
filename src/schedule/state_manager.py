"""Authoritative scheduling state and information-field facade."""
from __future__ import annotations

from typing import Optional

import numpy as np

from src.schedule.config_loader import AppConfig
from src.schedule.datatypes import BBox, GridCoord, Marker, Region, TargetReport, UAVState
from src.schedule.info_field import InfoField


class StateManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.current_time = 0.0
        self.cycle = 0
        self.lifecycle_mode = False
        self.info_field = InfoField(config)
        self._uavs = [
            UAVState(
                id=f"UAV-{index + 1}",
                status="idle",
                position=GridCoord(*config.environment.base_position),
            )
            for index in range(config.uav.count_max)
        ]
        self._search_regions: list[Region] = []
        self._track_regions: list[Region] = []
        self._previous_search_regions: list[Region] = []
        self._markers: list[Marker] = []
        self._marker_counter = 0
        self._events: list[dict] = []
        self._known_target_groups: set[str] = set()
        self._target_reports: dict[str, TargetReport] = {}
        self.obstacles: list = []
        self.obstacle_mask = np.zeros(config.grid.resolution, dtype=bool)
        self.obstacle_version = 0
        self.land_mask = np.zeros(config.grid.resolution, dtype=bool)
        self._base_positions: tuple[tuple[int, int], ...] = (config.environment.base_position,)

    def step(self, current_time: float) -> None:
        self.current_time = current_time
        self.info_field.update_decay(current_time)
        values = self.get_value_matrix()
        for region in self._search_regions:
            b = region.bbox
            scan_times = self.info_field.last_scan_time[
                b.col_start:b.col_end, b.row_start:b.row_end
            ]
            region.completion_pct = float(np.isfinite(scan_times).mean() * 100) if scan_times.size else 0.0
            region.avg_info = self.get_avg_info_in_bbox(b)
            patch = values[b.col_start:b.col_end, b.row_start:b.row_end]
            region.info_value = float(patch.mean()) if patch.size else 0.0

    # UAV management -------------------------------------------------
    def get_all_uavs(self) -> list[UAVState]:
        return self._uavs

    def get_uav(self, uav_id: str) -> Optional[UAVState]:
        return next((uav for uav in self._uavs if uav.id == uav_id), None)

    def get_available_uavs(self) -> list[UAVState]:
        return [uav for uav in self._uavs if uav.status == "idle"]

    def update_uav_status(
        self,
        uav_id: str,
        status: str,
        position: GridCoord,
        assigned_region_id: Optional[str] = None,
        fuel_remaining_pct: Optional[float] = None,
        target_group_id: Optional[str] = None,
        heading_deg: Optional[float] = None,
        sensor_mode: Optional[str] = None,
    ) -> None:
        uav = self.get_uav(uav_id)
        if uav is None:
            return
        uav.status = status
        uav.position = position
        if assigned_region_id is not None:
            uav.assigned_region_id = assigned_region_id
        if fuel_remaining_pct is not None:
            uav.fuel_remaining_pct = fuel_remaining_pct
        if target_group_id is not None:
            uav.target_group_id = target_group_id
        if heading_deg is not None:
            uav.heading_deg = heading_deg
        if sensor_mode is not None:
            uav.sensor_mode = sensor_mode

    def clear_uav_assignment(self, uav_id: str) -> None:
        uav = self.get_uav(uav_id)
        if uav:
            uav.assigned_region_id = None
            uav.target_group_id = None

    # Environment ----------------------------------------------------
    def set_environment_obstacles(self, obstacles: list, mask) -> None:
        self.obstacles = list(obstacles)
        normalized = np.array(mask, dtype=np.bool_, copy=True)
        if not np.array_equal(self.obstacle_mask, normalized):
            self.obstacle_version += 1
        self.obstacle_mask = normalized

    def set_land_mask(self, mask) -> None:
        """Publish the reset-specific mainland cells to all schedulers."""
        normalized = np.asarray(mask, dtype=bool)
        if normalized.shape != self.obstacle_mask.shape:
            raise ValueError("land mask must match the grid resolution")
        self.land_mask = normalized

    def set_base_positions(self, positions) -> None:
        """Publish reset-specific land bases to scheduling and coverage code."""
        normalized = tuple((int(position[0]), int(position[1])) for position in positions)
        if not normalized:
            raise ValueError("at least one base position is required")
        self._base_positions = normalized
        for uav in self._uavs:
            if uav.status == "idle":
                uav.position = GridCoord(*normalized[0])

    def get_base_positions(self) -> tuple[tuple[int, int], ...]:
        return self._base_positions

    # Region management ----------------------------------------------
    def set_search_regions(self, regions: list[Region]) -> None:
        self._previous_search_regions = list(self._search_regions)
        self._search_regions = regions

    def get_search_regions(self) -> list[Region]:
        return self._search_regions

    def get_active_search_regions(self) -> list[Region]:
        return [region for region in self._search_regions if region.status == "active"]

    def get_previous_search_regions(self) -> list[Region]:
        return self._previous_search_regions

    def get_track_regions(self) -> list[Region]:
        return self._track_regions

    def retire_search_regions_overlapping_tracks(
        self,
    ) -> list[tuple[Region, Optional[str]]]:
        """Remove search work that no longer has exclusive airspace."""
        if not self._track_regions or not self._search_regions:
            return []

        retired: list[tuple[Region, Optional[str]]] = []
        retained: list[Region] = []
        for region in self._search_regions:
            overlaps_track = any(
                self._bboxes_overlap(region.bbox, track.bbox)
                for track in self._track_regions
            )
            if not overlaps_track:
                retained.append(region)
                continue

            assigned_uav_id = region.assigned_uav_id
            retired.append((region, assigned_uav_id))
            region.status = "stale"
            region.assigned_uav_id = None
            if assigned_uav_id:
                uav = self.get_uav(assigned_uav_id)
                if uav and uav.assigned_region_id == region.id:
                    uav.assigned_region_id = None

        if not retired:
            return []

        previous_by_id = {
            region.id: region for region in self._previous_search_regions
        }
        previous_by_id.update({region.id: region for region, _ in retired})
        self._previous_search_regions = list(previous_by_id.values())
        self._search_regions = retained
        return retired

    @staticmethod
    def _bboxes_overlap(a: BBox, b: BBox) -> bool:
        return not (
            a.col_end <= b.col_start
            or b.col_end <= a.col_start
            or a.row_end <= b.row_start
            or b.row_end <= a.row_start
        )

    def get_track_region_for_group(self, target_group_id: str) -> Optional[Region]:
        return next(
            (region for region in self._track_regions if region.target_group_id == target_group_id),
            None,
        )

    def is_target_group_known(self, target_group_id: str) -> bool:
        return target_group_id in self._known_target_groups

    def record_target_observation(
        self,
        group_id: str,
        position: GridCoord,
        source_uav_id: str,
        observed_at: Optional[float] = None,
    ) -> TargetReport:
        """Store a sensor-derived fix without exposing any ship truth state."""
        timestamp = self.current_time if observed_at is None else float(observed_at)
        normalized = GridCoord(int(round(position.col)), int(round(position.row)))
        previous = self._target_reports.get(group_id)
        velocity = (0.0, 0.0)
        observations = 1
        if previous is not None:
            elapsed = timestamp - previous.observed_at
            if elapsed > 1e-6:
                raw_velocity = (
                    (normalized.col - previous.position.col) / elapsed,
                    (normalized.row - previous.position.row) / elapsed,
                )
                # EO fixes may be noisy.  Keep a useful uncertainty growth
                # rate without allowing a one-cell quantization jump to make
                # the successor search area leap across the map.
                speed = float(np.hypot(*raw_velocity))
                scale = min(1.0, 0.20 / speed) if speed else 1.0
                velocity = (raw_velocity[0] * scale, raw_velocity[1] * scale)
            else:
                velocity = previous.velocity_cells_per_min
            observations = previous.observation_count + 1
        report = TargetReport(
            group_id=group_id,
            position=normalized,
            observed_at=timestamp,
            source_uav_id=source_uav_id,
            velocity_cells_per_min=velocity,
            observation_count=observations,
        )
        self._target_reports[group_id] = report
        self._known_target_groups.add(group_id)
        return report

    def get_target_report(self, group_id: str) -> Optional[TargetReport]:
        return self._target_reports.get(group_id)

    def get_target_reports(self) -> list[TargetReport]:
        return sorted(self._target_reports.values(), key=lambda item: item.group_id)

    def clear_target_report(self, group_id: str) -> None:
        self._target_reports.pop(group_id, None)

    def create_track_region(self, target_group_id: str, center: GridCoord) -> Region:
        existing = self.get_track_region_for_group(target_group_id)
        if existing is not None:
            return existing
        col, row = center
        half = 2
        region = Region(
            id=f"T{len(self._track_regions) + 1}",
            bbox=BBox(
                max(0, col - half),
                max(0, row - half),
                min(self.config.grid.resolution[1], col + half),
                min(self.config.grid.resolution[0], row + half),
            ),
            type="track",
            priority="high",
            created_cycle=self.cycle,
            target_group_id=target_group_id,
        )
        self._track_regions.append(region)
        self._known_target_groups.add(target_group_id)
        return region

    def update_track_region_center(self, region_id: str, new_center: GridCoord) -> None:
        for region in self._track_regions:
            if region.id != region_id:
                continue
            col, row = new_center
            region.bbox = BBox(
                max(0, col - 2),
                max(0, row - 2),
                min(self.config.grid.resolution[1], col + 2),
                min(self.config.grid.resolution[0], row + 2),
            )
            return

    def release_track_region(
        self,
        region_id: str,
        source_uav_id: str = "",
        *,
        create_marker: bool = True,
    ) -> None:
        for region in list(self._track_regions):
            if region.id != region_id:
                continue
            center = GridCoord(
                (region.bbox.col_start + region.bbox.col_end) // 2,
                (region.bbox.row_start + region.bbox.row_end) // 2,
            )
            if create_marker:
                self._marker_counter += 1
                marker = Marker(
                    id=f"MK{self._marker_counter}",
                    position=center,
                    created_time=self.current_time,
                    source_uav_id=source_uav_id,
                )
                self._markers.append(marker)
                self.info_field.add_marker(marker.position, self.current_time, marker.id)
            self._track_regions.remove(region)
            return

    def get_active_markers(self) -> list[Marker]:
        return self._markers

    # Events ---------------------------------------------------------
    def add_event(self, event_type: str, data: dict) -> None:
        self._events.append({"type": event_type, "time": self.current_time, "data": data})

    def get_recent_events(self, since_time: float) -> list[dict]:
        return [event for event in self._events if event["time"] >= since_time]

    # Information field facade -------------------------------------
    def scan_bbox(self, bbox: BBox, current_time: float, is_track: bool = False) -> None:
        self.info_field.scan_bbox(bbox, current_time, is_track)

    def scan_cell(self, coord: GridCoord, current_time: float, is_track: bool = False) -> None:
        self.info_field.scan_cell(coord, current_time, is_track)

    def get_info_matrix(self):
        return self.info_field.get_info_matrix()

    def get_value_matrix(self):
        return self.info_field.get_value_matrix(self.current_time)

    def get_searchable_mask(self) -> np.ndarray:
        """Return cells that can be searched under the operational rules."""
        searchable = ~np.asarray(self.obstacle_mask, dtype=bool).copy()
        searchable &= ~self.land_mask
        if searchable.size:
            searchable[0, :] = False
            searchable[-1, :] = False
            searchable[:, 0] = False
            searchable[:, -1] = False
            for col, row in self._base_positions:
                searchable[col, row] = False
        return searchable

    def get_coverage_stats(self) -> dict[str, float | int]:
        """Measure unique coverage over searchable sea cells only."""
        searchable = self.get_searchable_mask()
        scanned = np.isfinite(self.info_field.last_scan_time) & searchable
        searchable_cells = int(searchable.sum())
        scanned_cells = int(scanned.sum())
        coverage_pct = (
            scanned_cells / searchable_cells * 100.0
            if searchable_cells
            else 0.0
        )
        return {
            "scanned_searchable_cells": scanned_cells,
            "searchable_cells": searchable_cells,
            "coverage_pct": coverage_pct,
        }

    def get_avg_info_in_bbox(self, bbox: BBox) -> float:
        return self.info_field.get_avg_info_in_bbox(bbox)

    def get_avg_value_in_bbox(self, bbox: BBox) -> float:
        return self.info_field.get_avg_value_in_bbox(bbox, self.current_time)


__all__ = ["StateManager"]

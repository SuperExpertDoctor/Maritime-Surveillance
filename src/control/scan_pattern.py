"""Deprecated coarse scan waypoint compatibility helpers."""

from __future__ import annotations

import warnings

from src.control.heuristic.coverage import scan_endpoint_poses
from src.schedule.datatypes import BBox, GridCoord
from src.utils.coverage_planner import CoveragePlanner


def generate_scan_waypoints(bbox: BBox, swath_cells: int = 1) -> list[GridCoord]:
    """Return legacy grid scan endpoints from the shared coverage planner."""
    warnings.warn(
        "generate_scan_waypoints() is deprecated; use CoverageController",
        DeprecationWarning,
        stacklevel=2,
    )
    coverage = CoveragePlanner().plan(
        bbox,
        (float(bbox.col_start), float(bbox.row_start), 0.0),
        swath_cells,
        1.0,
        direction="vertical",
    )
    return [GridCoord(round(pose[0]), round(pose[1])) for pose in scan_endpoint_poses(coverage)]


def estimate_coverage_time(
    bbox: BBox,
    cruise_speed_kmh: float,
    sar_swath_km: int,
    cell_size_km: int = 10,
    efficiency: float = 0.75,
) -> float:
    """Estimate coverage time in minutes."""
    width = bbox.col_end - bbox.col_start
    height = bbox.row_end - bbox.row_start
    area_km2 = width * height * cell_size_km * cell_size_km
    return area_km2 / (cruise_speed_kmh * sar_swath_km * efficiency) * 60.0

"""Deprecated grid waypoint compatibility helpers."""

from __future__ import annotations

import math
import warnings

import numpy as np

from src.control.heuristic.navigation import AStarNavigator
from src.schedule.datatypes import BBox, GridCoord


def navigate_to_region(
    current: GridCoord, target_bbox: BBox, cell_size_km: float = 10.0
) -> list[GridCoord]:
    """Return legacy grid waypoints through the curvature-safe navigator."""
    del cell_size_km
    warnings.warn(
        "navigate_to_region() is deprecated; use AStarNavigator through a controller",
        DeprecationWarning,
        stacklevel=2,
    )
    path = AStarNavigator().plan_to_region(
        (float(current.col), float(current.row), 0.0),
        target_bbox,
        np.zeros((30, 30), dtype=bool),
        r_min=1.0,
    )
    return [GridCoord(round(pose[0]), round(pose[1])) for pose in path]


def grid_distance(a: GridCoord, b: GridCoord) -> float:
    """Return Euclidean distance in grid cells."""
    return math.sqrt((a.col - b.col) ** 2 + (a.row - b.row) ** 2)


def travel_time(
    a: GridCoord,
    b: GridCoord,
    cruise_speed_kmh: float,
    cell_size_km: float = 10.0,
) -> float:
    """Return travel time in minutes."""
    return grid_distance(a, b) * cell_size_km / cruise_speed_kmh * 60.0

"""Pure, process-safe search-route planning helpers.

The simulation main process owns task assignment and state.  This module has
no access to that state: it accepts immutable snapshots and returns a route,
which makes it safe to execute in a ``ProcessPoolExecutor``.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from src.env.dubins import DubinsPath, Pose
from src.schedule.datatypes import BBox
from src.utils.coverage_planner import CoveragePlanner
from src.utils.obstacle_avoider import ObstacleAvoider


@dataclass(frozen=True)
class SearchRouteRequest:
    uav_id: str
    start_pose: Pose
    bbox: tuple[int, int, int, int]
    swath_width: float
    r_min: float
    obstacle_mask: np.ndarray
    unscanned_mask: np.ndarray
    allow_revisit: bool
    direction: str | None = None
    seed: int = 17
    along_track_cells: float | None = None
    allow_fallback: bool = True


@dataclass(frozen=True)
class SearchRoutePlan:
    uav_id: str
    path: tuple[Pose, ...]
    transit_end_index: int
    scan_ranges: tuple[tuple[int, int, str], ...]
    scanned_swath_count: int


def _path_in_bounds(path: tuple[Pose, ...] | list[Pose], shape: tuple[int, int]) -> bool:
    cols, rows = shape
    return all(
        0 <= math.floor(pose[0]) < cols and 0 <= math.floor(pose[1]) < rows
        for pose in path
    )


def _nearest_free_point(point: Pose, mask: np.ndarray) -> Pose | None:
    """Project a blocked pose onto the nearest free cell centre, keeping heading."""
    col0, row0 = int(math.floor(point[0])), int(math.floor(point[1]))
    cols, rows = mask.shape
    best: tuple[float, float, float] | None = None
    max_radius = max(cols, rows)
    for radius in range(1, max_radius + 1):
        for dcol in range(-radius, radius + 1):
            for drow in (-radius, radius):
                col, row = col0 + dcol, row0 + drow
                if 0 <= col < cols and 0 <= row < rows and not mask[col, row]:
                    dist = math.hypot(dcol, drow)
                    if best is None or dist < best[0]:
                        best = (dist, col + 0.5, row + 0.5)
        for drow in range(-radius + 1, radius):
            for dcol in (-radius, radius):
                col, row = col0 + dcol, row0 + drow
                if 0 <= col < cols and 0 <= row < rows and not mask[col, row]:
                    dist = math.hypot(dcol, drow)
                    if best is None or dist < best[0]:
                        best = (dist, col + 0.5, row + 0.5)
        if best is not None:
            return (best[1], best[2], point[2])
    return None


def plan_search_route(request: SearchRouteRequest) -> SearchRoutePlan:
    """Build a complete obstacle-safe Dubins/SAR route from a state snapshot."""
    bbox = BBox(*request.bbox)
    mask = np.asarray(request.obstacle_mask, dtype=bool)
    planner = CoveragePlanner(sample_step=0.2)
    coverage = planner.plan(
        bbox,
        request.start_pose,
        request.swath_width,
        request.r_min,
        direction=request.direction,
        along_track_cells=request.along_track_cells,
        bounds=tuple(mask.shape),
    )
    fresh_mask = np.asarray(request.unscanned_mask, dtype=bool)
    swaths = [
        swath for swath in coverage.swaths
        if request.allow_revisit
        or any(fresh_mask[cell.col, cell.row] for cell in swath.footprint)
    ]
    if not swaths:
        return SearchRoutePlan(request.uav_id, (), 0, (), 0)

    avoider = ObstacleAvoider(max_iterations=1000, seed=request.seed)
    start = tuple(map(float, request.start_pose))
    if avoider._blocked(start, mask):
        # A drifting hazard can cover the airframe between assignment and
        # install.  Teleporting the route origin to the nearest free cell
        # keeps the task flyable instead of faulting the whole plan.
        projected = _nearest_free_point(start, mask)
        if projected is None:
            raise RuntimeError("no free cell exists to project the route start onto")
        start = projected
    path: list[Pose] = [start]
    scan_ranges: list[tuple[int, int, str]] = []
    transit_end_index = 0

    for index, swath in enumerate(swaths):
        entry = (swath.start[0], swath.start[1], swath.heading)
        scan_line = planner.sample_scan_line(swath)
        if not _path_in_bounds(scan_line, mask.shape):
            raise RuntimeError("SAR scan line exits planning bounds")
        if not avoider.is_path_safe(scan_line, mask):
            # A drifting hazard can cover part of a region between matching
            # and install.  Skip the blocked leg rather than vetoing the whole
            # route: the aircraft still covers every reachable cell, and the
            # missed cells are re-offered by the sweep channel once the hazard
            # moves on.
            continue
        direct = DubinsPath.compute(path[-1], entry, request.r_min, 0.2).waypoints
        if avoider.is_path_safe(direct, mask):
            connector = direct
        else:
            try:
                connector = avoider.plan_path(
                    path[-1], entry, mask, request.r_min,
                    allow_fallback=request.allow_fallback,
                )
            except RuntimeError:
                connector = ObstacleAvoider(
                    max_iterations=2400,
                    seed=request.seed + 31 + index * 101,
                ).plan_path(
                    path[-1], entry, mask, request.r_min,
                    allow_fallback=request.allow_fallback,
                )
        first_leg = not scan_ranges
        path.extend(connector[1:])
        if first_leg:
            transit_end_index = len(path) - 1
        scan_start = len(path) - 1
        path.extend(scan_line[1:])
        scan_ranges.append((scan_start, len(path) - 1, swath.look_direction))

    return SearchRoutePlan(
        request.uav_id,
        tuple(path),
        transit_end_index,
        tuple(scan_ranges),
        len(scan_ranges),
    )


__all__ = ["SearchRoutePlan", "SearchRouteRequest", "plan_search_route"]

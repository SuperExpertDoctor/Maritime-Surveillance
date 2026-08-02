"""Pure, process-safe search-route planning helpers.

The simulation main process owns task assignment and state.  This module has
no access to that state: it accepts immutable snapshots and returns a route,
which makes it safe to execute in a ``ProcessPoolExecutor``.
"""
from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class SearchRoutePlan:
    uav_id: str
    path: tuple[Pose, ...]
    transit_end_index: int
    scan_ranges: tuple[tuple[int, int, str], ...]
    scanned_swath_count: int


def plan_search_route(request: SearchRouteRequest) -> SearchRoutePlan:
    """Build a complete obstacle-safe Dubins/SAR route from a state snapshot."""
    bbox = BBox(*request.bbox)
    planner = CoveragePlanner(sample_step=0.2)
    coverage = planner.plan(
        bbox,
        request.start_pose,
        request.swath_width,
        request.r_min,
        direction=request.direction,
    )
    fresh_mask = np.asarray(request.unscanned_mask, dtype=bool)
    swaths = [
        swath for swath in coverage.swaths
        if request.allow_revisit
        or any(fresh_mask[cell.col, cell.row] for cell in swath.footprint)
    ]
    if not swaths:
        return SearchRoutePlan(request.uav_id, (), 0, (), 0)

    mask = np.asarray(request.obstacle_mask, dtype=bool)
    avoider = ObstacleAvoider(max_iterations=1000, seed=request.seed)
    path: list[Pose] = [tuple(map(float, request.start_pose))]
    scan_ranges: list[tuple[int, int, str]] = []
    transit_end_index = 0

    for index, swath in enumerate(swaths):
        entry = (swath.start[0], swath.start[1], swath.heading)
        direct = DubinsPath.compute(path[-1], entry, request.r_min, 0.2).waypoints
        if avoider.is_path_safe(direct, mask):
            connector = direct
        else:
            try:
                connector = avoider.plan_path(path[-1], entry, mask, request.r_min)
            except RuntimeError:
                connector = ObstacleAvoider(
                    max_iterations=2400,
                    seed=request.seed + 31 + index * 101,
                ).plan_path(path[-1], entry, mask, request.r_min)
        path.extend(connector[1:])
        if index == 0:
            transit_end_index = len(path) - 1
        scan_line = planner.sample_scan_line(swath)
        if not avoider.is_path_safe(scan_line, mask):
            raise RuntimeError("SAR scan line intersects a no-fly obstacle")
        scan_start = len(path) - 1
        path.extend(scan_line[1:])
        scan_ranges.append((scan_start, len(path) - 1, swath.look_direction))

    return SearchRoutePlan(
        request.uav_id,
        tuple(path),
        transit_end_index,
        tuple(scan_ranges),
        len(swaths),
    )


__all__ = ["SearchRoutePlan", "SearchRouteRequest", "plan_search_route"]

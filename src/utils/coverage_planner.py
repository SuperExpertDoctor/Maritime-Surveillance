"""Dubins-connected boustrophedon coverage for side-looking SAR."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Sequence

from src.env.dubins import DubinsPath, Pose
from src.schedule.datatypes import BBox, GridCoord


@dataclass(frozen=True)
class ScanSwath:
    start: tuple[float, float]
    end: tuple[float, float]
    look_direction: str
    footprint: tuple[GridCoord, ...]
    heading: float


@dataclass
class CoveragePath:
    swaths: list[ScanSwath] = field(default_factory=list)
    waypoints: list[Pose] = field(default_factory=list)
    total_length: float = 0.0
    scan_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def covered_cells(self) -> set[GridCoord]:
        return {cell for swath in self.swaths for cell in swath.footprint}


class CoveragePlanner:
    """Plan a complete side-looking scan of an axis-aligned rectangle.

    Bounding boxes are end-exclusive, matching ``BBox`` throughout the
    scheduling package.  Scan lines end on the rectangle boundary; the
    Dubins connector itself then completes each U-turn outside the region.
    """

    def __init__(self, sample_step: float = 0.25, near_range: float = 0.25):
        if sample_step <= 0:
            raise ValueError("sample_step must be positive")
        self.sample_step = sample_step
        self.near_range = max(0.01, near_range)

    def plan(
        self,
        bbox: BBox | Sequence[int],
        start_pose: Sequence[float],
        swath_width: float,
        R_min: float,
        direction: str | None = None,
    ) -> CoveragePath:
        box = bbox if isinstance(bbox, BBox) else BBox(*bbox)
        width = box.col_end - box.col_start
        height = box.row_end - box.row_start
        if width <= 0 or height <= 0:
            raise ValueError("bbox must have positive area")
        if swath_width <= 0:
            raise ValueError("swath_width must be positive")
        if direction not in (None, "horizontal", "vertical"):
            raise ValueError("direction must be horizontal, vertical, or None")

        orientation = direction or ("horizontal" if width >= height else "vertical")
        swaths = self._build_swaths(box, swath_width, R_min, orientation)
        if swaths:
            forward_entry = (
                swaths[0].start[0],
                swaths[0].start[1],
                swaths[0].heading,
            )
            reverse_entry = (
                swaths[0].end[0],
                swaths[0].end[1],
                _wrap_heading(swaths[0].heading + math.pi),
            )
            forward_length = DubinsPath.compute(
                start_pose, forward_entry, R_min, self.sample_step,
            ).total_length
            reverse_length = DubinsPath.compute(
                start_pose, reverse_entry, R_min, self.sample_step,
            ).total_length
            if reverse_length < forward_length:
                swaths = [
                    ScanSwath(
                        swath.end,
                        swath.start,
                        "left" if swath.look_direction == "right" else "right",
                        swath.footprint,
                        _wrap_heading(swath.heading + math.pi),
                    )
                    for swath in swaths
                ]
        result = CoveragePath(swaths=swaths)
        current = tuple(map(float, start_pose))

        for swath in swaths:
            entry = (swath.start[0], swath.start[1], swath.heading)
            connection = DubinsPath.compute(current, entry, R_min, self.sample_step)
            connection.waypoints[-1] = entry
            self._append_unique(result.waypoints, connection.waypoints)
            result.total_length += connection.total_length

            scan_start_index = len(result.waypoints) - 1
            straight = self._sample_line(swath.start, swath.end, swath.heading)
            self._append_unique(result.waypoints, straight)
            scan_end_index = len(result.waypoints) - 1
            result.scan_ranges.append((scan_start_index, scan_end_index))
            result.total_length += math.dist(swath.start, swath.end)
            current = (swath.end[0], swath.end[1], swath.heading)

        return result

    def _build_swaths(
        self, box: BBox, width: float, radius: float, orientation: str
    ) -> list[ScanSwath]:
        swaths: list[ScanSwath] = []
        if orientation == "horizontal":
            low_endpoint = float(box.col_start)
            high_endpoint = float(box.col_end)
            count = int(math.ceil((box.row_end - box.row_start) / width))
            for index in range(count):
                band_start = box.row_start + index * width
                band_end = min(box.row_end, band_start + width)
                track = max(0.0, band_start - self.near_range)
                footprint = tuple(
                    GridCoord(c, r)
                    for c in range(box.col_start, box.col_end)
                    for r in range(int(math.floor(band_start)), int(math.ceil(band_end)))
                    if box.row_start <= r < box.row_end
                )
                if index % 2 == 0:
                    start = (low_endpoint, track)
                    end = (high_endpoint, track)
                    heading, look = 0.0, "right"
                else:
                    start = (high_endpoint, track)
                    end = (low_endpoint, track)
                    heading, look = math.pi, "left"
                swaths.append(ScanSwath(start, end, look, footprint, heading))
        else:
            low_endpoint = float(box.row_start)
            high_endpoint = float(box.row_end)
            count = int(math.ceil((box.col_end - box.col_start) / width))
            for index in range(count):
                band_start = box.col_start + index * width
                band_end = min(box.col_end, band_start + width)
                track = max(0.0, band_start - self.near_range)
                footprint = tuple(
                    GridCoord(c, r)
                    for c in range(int(math.floor(band_start)), int(math.ceil(band_end)))
                    for r in range(box.row_start, box.row_end)
                    if box.col_start <= c < box.col_end
                )
                if index % 2 == 0:
                    start = (track, low_endpoint)
                    end = (track, high_endpoint)
                    heading, look = math.pi / 2.0, "left"
                else:
                    start = (track, high_endpoint)
                    end = (track, low_endpoint)
                    heading, look = -math.pi / 2.0, "right"
                swaths.append(ScanSwath(start, end, look, footprint, heading))
        return swaths

    def _sample_line(
        self, start: tuple[float, float], end: tuple[float, float], heading: float
    ) -> list[Pose]:
        length = math.dist(start, end)
        steps = max(1, int(math.ceil(length / self.sample_step)))
        return [
            (
                start[0] + (end[0] - start[0]) * index / steps,
                start[1] + (end[1] - start[1]) * index / steps,
                heading,
            )
            for index in range(steps + 1)
        ]

    def sample_scan_line(self, swath: ScanSwath) -> list[Pose]:
        return self._sample_line(swath.start, swath.end, swath.heading)

    def is_region_feasible(
        self,
        bbox: BBox | Sequence[int],
        swath_width: float,
        R_min: float,
        obstacle_mask,
    ) -> bool:
        """Check every scan line and inter-line Dubins turn against a mask."""
        box = bbox if isinstance(bbox, BBox) else BBox(*bbox)
        width = box.col_end - box.col_start
        height = box.row_end - box.row_start
        orientation = "horizontal" if width >= height else "vertical"
        swaths = self._build_swaths(box, swath_width, R_min, orientation)
        previous: ScanSwath | None = None
        for swath in swaths:
            if not self._poses_are_free(self.sample_scan_line(swath), obstacle_mask):
                return False
            if previous is not None:
                start = (previous.end[0], previous.end[1], previous.heading)
                goal = (swath.start[0], swath.start[1], swath.heading)
                turn = DubinsPath.compute(start, goal, R_min, self.sample_step)
                if not self._poses_are_free(turn.waypoints, obstacle_mask):
                    return False
            previous = swath
        return bool(swaths)

    @staticmethod
    def _poses_are_free(poses: Sequence[Sequence[float]], obstacle_mask) -> bool:
        cols, rows = obstacle_mask.shape
        for pose in poses:
            col, row = int(math.floor(pose[0])), int(math.floor(pose[1]))
            if col < 0 or row < 0 or col >= cols or row >= rows:
                return False
            if obstacle_mask[col, row]:
                return False
        return True

    @staticmethod
    def _append_unique(target: list[Pose], source: list[Pose]) -> None:
        for pose in source:
            if target and math.dist(target[-1][:2], pose[:2]) <= 1e-10:
                continue
            else:
                target.append(pose)


__all__ = ["CoveragePlanner", "CoveragePath", "ScanSwath"]


def _wrap_heading(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi

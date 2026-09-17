"""Continuous, local-window guidance for physical SAR coverage routes."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from typing import Sequence

from src.control.common.contracts import ActionSpec, Pose


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


@dataclass(frozen=True)
class CoverageGuidance:
    progress_cells: float
    next_index: int
    turn_rate_rad_min: float
    speed_cells_min: float
    cross_track_error_cells: float
    scan_segment_index: int | None
    is_complete: bool


class CoverageRouteFollower:
    """Follow an immutable route using monotone local arc-length projection."""

    def __init__(
        self,
        route: Sequence[Sequence[float]],
        *,
        scan_ranges: Sequence[tuple[int, int]] = (),
        r_min: float,
        along_track_cells: float = 0.8,
        progress_offset_cells: float = 0.0,
    ) -> None:
        if not route:
            raise ValueError("route must contain at least one pose")
        if not math.isfinite(r_min) or r_min <= 0.0:
            raise ValueError("r_min must be finite and positive")
        if not math.isfinite(along_track_cells) or along_track_cells <= 0.0:
            raise ValueError("along_track_cells must be finite and positive")
        if not math.isfinite(progress_offset_cells) or progress_offset_cells < 0.0:
            raise ValueError("progress_offset_cells must be finite and non-negative")
        poses = tuple(tuple(map(float, pose)) for pose in route)
        if any(len(pose) != 3 for pose in poses):
            raise ValueError("route poses must be (column, row, heading) triples")
        if any(not all(math.isfinite(value) for value in pose) for pose in poses):
            raise ValueError("route poses must be finite")
        ranges = tuple((int(start), int(end)) for start, end in scan_ranges)
        if any(
            start < 0 or end >= len(poses) or start >= end
            for start, end in ranges
        ):
            raise ValueError("scan ranges must be ordered inclusive route intervals")
        if any(previous[1] >= current[0] for previous, current in zip(ranges, ranges[1:])):
            raise ValueError("scan ranges must be disjoint and ordered")

        cumulative = [0.0]
        for start, end in zip(poses, poses[1:]):
            cumulative.append(cumulative[-1] + math.dist(start[:2], end[:2]))
        self._poses: tuple[Pose, ...] = poses  # type: ignore[assignment]
        self._scan_ranges = ranges
        self._cumulative = tuple(cumulative)
        self._length = cumulative[-1]
        self._r_min = float(r_min)
        self._along_track_cells = float(along_track_cells)
        self._progress_offset = float(progress_offset_cells)
        self._progress = 0.0
        self._index = 0
        self._complete = False
        self._scan_is_stable = False

    @property
    def poses(self) -> tuple[Pose, ...]:
        return self._poses

    @property
    def index(self) -> int:
        return self._index

    @property
    def is_complete(self) -> bool:
        return self._complete

    @property
    def progress_cells(self) -> float:
        return self._progress_offset + self._progress

    @property
    def scan_segment_index(self) -> int | None:
        return self._scan_segment_for_progress(self._progress)

    @property
    def scan_is_stable(self) -> bool:
        return self._scan_is_stable

    def update(
        self,
        *,
        position: Sequence[float],
        heading_rad: float,
        speed_cells_min: float,
        dt_min: float,
        action_spec: ActionSpec,
    ) -> CoverageGuidance:
        if len(position) != 2 or not all(math.isfinite(float(value)) for value in position):
            raise ValueError("position must contain two finite values")
        if not math.isfinite(heading_rad):
            raise ValueError("heading_rad must be finite")
        if not math.isfinite(dt_min) or dt_min <= 0.0:
            raise ValueError("dt_min must be finite and positive")
        if not math.isfinite(speed_cells_min) or speed_cells_min < 0.0:
            raise ValueError("speed_cells_min must be finite and non-negative")
        speed = min(
            max(float(speed_cells_min), action_spec.min_speed_cells_min),
            action_spec.max_speed_cells_min,
        )
        lower = max(0.0, self._progress - speed * dt_min)
        upper = min(self._length, self._progress + max(3.0 * speed * dt_min, self._r_min))
        projected_s, segment_index, cross_track = self._project_local(
            float(position[0]), float(position[1]), lower, upper
        )
        self._progress = max(self._progress, projected_s)
        self._index = self._index_for_progress(self._progress)

        scan_segment_index = self._scan_segment_for_progress(self._progress)
        stable_margin = max(speed * dt_min, self._along_track_cells / 2.0)
        self._scan_is_stable = (
            scan_segment_index is not None
            and self._scan_segment_for_progress(
                self._progress, margin=stable_margin
            )
            == scan_segment_index
        )
        if scan_segment_index is not None:
            scan_start, scan_end = self._scan_ranges[scan_segment_index]
            segment_index = min(max(segment_index, scan_start), scan_end - 1)
            tangent = self._segment_tangent(segment_index)
            cross_track = self._signed_cross_track(
                position, self._poses[segment_index], tangent
            )
        else:
            tangent = self._segment_tangent(segment_index)
            cross_track = self._signed_cross_track(
                position, self._poses[segment_index], tangent
            )

        lookahead = max(2.0 * speed * dt_min, self._r_min)
        look_s = min(self._length, self._progress + lookahead)
        target = self._interpolate(look_s)
        alpha = _wrap_pi(math.atan2(target[1] - position[1], target[0] - position[0]) - heading_rad)
        if math.hypot(target[0] - position[0], target[1] - position[1]) <= 1e-12:
            turn = _wrap_pi(target[2] - heading_rad) / dt_min
        else:
            turn = 2.0 * speed * math.sin(alpha) / lookahead
        if scan_segment_index is not None:
            desired = tangent - math.atan2(0.5 * cross_track, max(speed, 1e-9))
            turn = _wrap_pi(desired - heading_rad) / dt_min
        else:
            upcoming = self._upcoming_scan_index(self._progress)
            if upcoming is not None:
                scan_start, _ = self._scan_ranges[upcoming]
                if self._cumulative[scan_start] - self._progress <= 4.0 * self._r_min:
                    scan_tangent = self._segment_tangent(scan_start)
                    heading_error = _wrap_pi(scan_tangent - heading_rad)
                    # Pre-align only when it reinforces the route's local
                    # pursuit turn.  A connector may require a U-turn before
                    # the scan tangent is reachable; forcing that tangent
                    # early would freeze the aircraft on the connector.
                    if abs(heading_error) > 0.25 and turn * heading_error > 0.0:
                        turn = heading_error / dt_min
        turn = self._bounded_turn(turn, speed, self._r_min, action_spec)

        tolerance = max(speed * dt_min, 0.05)
        self._complete = (
            self._length - self._progress <= tolerance
            and math.dist(position, self._poses[-1][:2]) <= tolerance
        )
        next_index = len(self._poses) if self._complete else min(self._index + 1, len(self._poses))
        return CoverageGuidance(
            progress_cells=self.progress_cells,
            next_index=next_index,
            turn_rate_rad_min=turn,
            speed_cells_min=speed,
            cross_track_error_cells=abs(cross_track),
            scan_segment_index=scan_segment_index,
            is_complete=self._complete,
        )

    def _project_local(
        self, x: float, y: float, lower: float, upper: float
    ) -> tuple[float, int, float]:
        if self._length <= 1e-12:
            return 0.0, 0, math.dist((x, y), self._poses[0][:2])
        active_segment = self._active_segment_index(self._progress)
        first_segment, last_segment = self._route_interval(active_segment)
        candidates = []
        for index in range(first_segment, last_segment + 1):
            candidate = self._project_segment(index, x, y, lower, upper)
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            projected = min(max(self._progress, 0.0), self._length)
            index = active_segment
            point = self._interpolate(projected)
            return projected, index, math.dist((x, y), point[:2])
        distance, projected, index = min(candidates, key=lambda item: (item[0], item[1]))
        return projected, index, distance

    def _project_segment(
        self, index: int, x: float, y: float, lower: float, upper: float
    ) -> tuple[float, float, int] | None:
        start, end = self._poses[index], self._poses[index + 1]
        start_s, end_s = self._cumulative[index], self._cumulative[index + 1]
        if end_s <= lower + 1e-12 or start_s >= upper - 1e-12:
            return None
        dx, dy = end[0] - start[0], end[1] - start[1]
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-18:
            return None
        fraction = ((x - start[0]) * dx + (y - start[1]) * dy) / length_sq
        fraction = min(1.0, max(0.0, fraction))
        projected = start_s + fraction * math.sqrt(length_sq)
        if projected < lower - 1e-12 or projected > upper + 1e-12:
            return None
        px, py = start[0] + fraction * dx, start[1] + fraction * dy
        return math.dist((x, y), (px, py)), projected, index

    def _active_segment_index(self, progress: float) -> int:
        if len(self._poses) < 2:
            return 0
        index = bisect_right(self._cumulative, progress + 1e-12) - 1
        index = min(max(index, 0), len(self._poses) - 2)
        while (
            index < len(self._poses) - 1
            and self._segment_length(index) <= 1e-12
        ):
            index += 1
        if index >= len(self._poses) - 1:
            index = len(self._poses) - 2
            while index > 0 and self._segment_length(index) <= 1e-12:
                index -= 1
        return index

    def _route_interval(self, active_segment: int) -> tuple[int, int]:
        if len(self._poses) < 2:
            return 0, 0
        # Scan ranges are the route's semantic barriers.  Projection may use
        # every segment in the active straight line, or the connector interval
        # between two lines, but it must not use the next parallel line merely
        # because it is spatially close.
        for scan_index, (start, end) in enumerate(self._scan_ranges):
            if start <= active_segment < end:
                return start, end - 1
            if active_segment < start:
                previous_end = self._scan_ranges[scan_index - 1][1] if scan_index else 0
                return previous_end, start - 1
        previous_end = self._scan_ranges[-1][1] if self._scan_ranges else 0
        return previous_end, len(self._poses) - 2

    def _segment_length(self, index: int) -> float:
        return self._cumulative[index + 1] - self._cumulative[index]

    def _segment_heading(self, index: int) -> float:
        start, end = self._poses[index], self._poses[index + 1]
        if self._segment_length(index) <= 1e-12:
            return end[2]
        return math.atan2(end[1] - start[1], end[0] - start[0])

    def _index_for_progress(self, progress: float) -> int:
        index = bisect_right(self._cumulative, progress + 1e-12) - 1
        return min(max(index, 0), len(self._poses) - 1)

    def _interpolate(self, progress: float) -> tuple[float, float, float]:
        if progress >= self._length - 1e-12:
            return self._poses[-1]
        index = max(0, bisect_right(self._cumulative, progress) - 1)
        while index < len(self._poses) - 1 and self._cumulative[index + 1] <= self._cumulative[index] + 1e-12:
            index += 1
        start, end = self._poses[index], self._poses[index + 1]
        span = self._cumulative[index + 1] - self._cumulative[index]
        fraction = (progress - self._cumulative[index]) / span if span else 0.0
        return (
            start[0] + (end[0] - start[0]) * fraction,
            start[1] + (end[1] - start[1]) * fraction,
            _wrap_pi(start[2] + _wrap_pi(end[2] - start[2]) * fraction),
        )

    def _scan_segment_for_progress(
        self, progress: float, *, margin: float = 0.0
    ) -> int | None:
        for scan_index, (start, end) in enumerate(self._scan_ranges):
            if (
                self._cumulative[start] + margin
                < progress
                < self._cumulative[end] - margin
            ):
                return scan_index
        return None

    def _upcoming_scan_index(self, progress: float) -> int | None:
        for scan_index, (start, _) in enumerate(self._scan_ranges):
            if progress < self._cumulative[start]:
                return scan_index
        return None

    def _segment_tangent(self, index: int) -> float:
        if len(self._poses) < 2:
            return self._poses[0][2]
        index = min(max(index, 0), len(self._poses) - 2)
        start, end = self._poses[index], self._poses[index + 1]
        if math.dist(start[:2], end[:2]) <= 1e-12:
            return end[2]
        return math.atan2(end[1] - start[1], end[0] - start[0])

    @staticmethod
    def _signed_cross_track(
        position: Sequence[float], origin: Sequence[float], tangent: float
    ) -> float:
        x, y = float(position[0]) - float(origin[0]), float(position[1]) - float(origin[1])
        return -x * math.sin(tangent) + y * math.cos(tangent)

    @staticmethod
    def _bounded_turn(
        turn: float, speed: float, r_min: float, action_spec: ActionSpec
    ) -> float:
        limit = min(
            abs(action_spec.min_turn_rate_rad_min),
            action_spec.max_turn_rate_rad_min,
            speed / max(r_min, 1e-9),
        )
        lower = max(action_spec.min_turn_rate_rad_min, -limit)
        upper = min(action_spec.max_turn_rate_rad_min, limit)
        return min(upper, max(lower, turn))


__all__ = ["CoverageGuidance", "CoverageRouteFollower"]

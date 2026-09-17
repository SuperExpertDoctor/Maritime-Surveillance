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
    ) -> None:
        if not route:
            raise ValueError("route must contain at least one pose")
        if not math.isfinite(r_min) or r_min <= 0.0:
            raise ValueError("r_min must be finite and positive")
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
        self._progress = 0.0
        self._index = 0
        self._complete = False

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
        return self._progress

    @property
    def scan_segment_index(self) -> int | None:
        return self._scan_segment_for_progress(self._progress)

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
        turn = self._bounded_turn(turn, speed, self._r_min, action_spec)

        tolerance = max(speed * dt_min, 0.05)
        self._complete = (
            self._length - self._progress <= tolerance
            and math.dist(position, self._poses[-1][:2]) <= tolerance
        )
        next_index = len(self._poses) if self._complete else min(self._index + 1, len(self._poses))
        return CoverageGuidance(
            progress_cells=self._progress,
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
        candidates = []
        for index, (start, end) in enumerate(zip(self._poses, self._poses[1:])):
            start_s, end_s = self._cumulative[index], self._cumulative[index + 1]
            if end_s <= lower + 1e-12 or start_s >= upper - 1e-12:
                continue
            dx, dy = end[0] - start[0], end[1] - start[1]
            length_sq = dx * dx + dy * dy
            if length_sq <= 1e-18:
                continue
            fraction = ((x - start[0]) * dx + (y - start[1]) * dy) / length_sq
            fraction = min(1.0, max(0.0, fraction))
            projected = start_s + fraction * math.sqrt(length_sq)
            if projected < lower - 1e-12 or projected > upper + 1e-12:
                continue
            px, py = start[0] + fraction * dx, start[1] + fraction * dy
            candidates.append((math.dist((x, y), (px, py)), projected, index))
        if not candidates:
            projected = min(max(self._progress, 0.0), self._length)
            index = min(self._index, max(0, len(self._poses) - 2))
            point = self._interpolate(projected)
            return projected, index, math.dist((x, y), point[:2])
        distance, projected, index = min(candidates, key=lambda item: (item[0], item[1]))
        return projected, index, distance

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

    def _scan_segment_for_progress(self, progress: float) -> int | None:
        for scan_index, (start, end) in enumerate(self._scan_ranges):
            if self._cumulative[start] < progress < self._cumulative[end]:
                return scan_index
        return None

    def _segment_tangent(self, index: int) -> float:
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

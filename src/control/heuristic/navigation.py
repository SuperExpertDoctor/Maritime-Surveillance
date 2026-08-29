"""Deterministic curvature-constrained Hybrid A* path planning."""

from __future__ import annotations

import heapq
import itertools
import math
from collections.abc import Iterable, Sequence

import numpy as np

from src.control.common.contracts import Pose
from src.control.common.safety import SafetyEnvelope
from src.env.dubins import DubinsPath
from src.schedule.datatypes import BBox


NodeKey = tuple[int, int, int]
AnalyticAttempt = tuple[NodeKey, Pose]


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


class PathNotFoundError(RuntimeError):
    """Raised when no safe curvature-constrained path reaches any goal."""

    def __init__(
        self,
        start: Pose,
        goal_summary: str,
        planning_map_version: int,
        analytic_attempts: Iterable[AnalyticAttempt] = (),
    ) -> None:
        self.start = start
        self.goal_summary = goal_summary
        self.planning_map_version = planning_map_version
        self.analytic_attempts = tuple(analytic_attempts)
        self.attempt_count = len(self.analytic_attempts)
        super().__init__(
            "no safe Hybrid A* path: "
            f"start={start}, {goal_summary}, "
            f"planning_map_version={planning_map_version}, "
            f"analytic_attempts={self.attempt_count}"
        )


class AStarNavigator:
    """Search continuous fixed-wing poses with deterministic motion primitives."""

    def __init__(
        self,
        xy_resolution: float = 0.5,
        heading_bins: int = 72,
        candidate_limit: int = 32,
        primitive_length: float = 1.0,
        sample_step: float = 0.2,
    ) -> None:
        if not math.isfinite(xy_resolution) or xy_resolution <= 0.0:
            raise ValueError("xy_resolution must be a finite positive number")
        if heading_bins < 1:
            raise ValueError("heading_bins must be positive")
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be positive")
        if not math.isfinite(primitive_length) or primitive_length <= 0.0:
            raise ValueError("primitive_length must be a finite positive number")
        if not math.isfinite(sample_step) or sample_step <= 0.0:
            raise ValueError("sample_step must be a finite positive number")
        self.xy_resolution = float(xy_resolution)
        self.heading_bins = int(heading_bins)
        self.candidate_limit = int(candidate_limit)
        self.primitive_length = float(primitive_length)
        self.sample_step = float(sample_step)

    def plan_grid(
        self,
        start: Pose,
        goals: set[tuple[float, float]],
        obstacle_mask: np.ndarray,
        r_min: float,
        planning_map_version: int = 0,
    ) -> list[Pose]:
        start_pose = self._normalise_pose(start)
        normalised_goals = self._normalise_goals(goals)
        summary = f"grid goals={len(normalised_goals)}"
        return self._plan(
            start_pose,
            normalised_goals,
            obstacle_mask,
            r_min,
            planning_map_version,
            summary,
        )

    def plan_to_region(
        self,
        start_pose: Pose,
        bbox: BBox,
        obstacle_mask: np.ndarray,
        r_min: float,
        planning_map_version: int = 0,
    ) -> list[Pose]:
        start = self._normalise_pose(start_pose)
        mask = self._normalise_mask(obstacle_mask)
        boundary = self._bbox_boundary(bbox)
        goals = [goal for goal in boundary if not self._point_blocked(goal, mask)]
        goals.sort(key=lambda goal: (math.dist(start[:2], goal), goal[0], goal[1]))
        summary = f"region boundary={bbox!r}, candidates={len(goals)}"
        return self._plan(
            start,
            goals,
            mask,
            r_min,
            planning_map_version,
            summary,
        )

    def plan_to_standoff(
        self,
        start_pose: Pose,
        target: Sequence[float],
        radius: float,
        obstacle_mask: np.ndarray,
        r_min: float,
        planning_map_version: int = 0,
    ) -> list[Pose]:
        start = self._normalise_pose(start_pose)
        mask = self._normalise_mask(obstacle_mask)
        target_xy = self._normalise_point(target, "target")
        if not math.isfinite(radius) or radius <= 0.0:
            raise ValueError("radius must be a finite positive number")
        goals = self._standoff_annulus(target_xy, float(radius), mask)
        goals.sort(key=lambda goal: (math.dist(start[:2], goal), goal[0], goal[1]))
        summary = (
            f"standoff target={target_xy}, radius={float(radius)}, "
            f"candidates={len(goals)}"
        )
        return self._plan(
            start,
            goals,
            mask,
            r_min,
            planning_map_version,
            summary,
        )

    def _plan(
        self,
        start: Pose,
        goals: Sequence[tuple[float, float]],
        obstacle_mask: np.ndarray,
        r_min: float,
        planning_map_version: int,
        goal_summary: str,
    ) -> list[Pose]:
        mask = self._normalise_mask(obstacle_mask)
        if not math.isfinite(r_min) or r_min <= 0.0:
            raise ValueError("r_min must be a finite positive number")
        available_goals = tuple(
            goal for goal in goals if not self._point_blocked(goal, mask)
        )
        attempts: list[AnalyticAttempt] = []
        if self._point_blocked(start, mask) or not available_goals:
            raise PathNotFoundError(start, goal_summary, planning_map_version, attempts)
        for goal in available_goals:
            if math.dist(start[:2], goal) <= 1e-12:
                return [start]

        primitive_length = max(
            self.primitive_length,
            2.0 * math.pi * r_min / self.heading_bins,
        )
        analytic_distance = max(2.0 * primitive_length, 2.0 * r_min)
        curvatures = (-1.0 / r_min, 0.0, 1.0 / r_min)
        sequence = itertools.count()
        start_key = self._node_key(start)
        g_scores: dict[NodeKey, float] = {start_key: 0.0}
        poses: dict[NodeKey, Pose] = {start_key: start}
        came_from: dict[NodeKey, tuple[NodeKey, tuple[Pose, ...]]] = {}
        closed: set[NodeKey] = set()
        open_heap: list[tuple[float, float, NodeKey, int]] = []
        heapq.heappush(
            open_heap,
            (
                self._heuristic(start, available_goals),
                0.0,
                start_key,
                next(sequence),
            ),
        )
        attempted_pairs: set[AnalyticAttempt] = set()
        incumbent: tuple[float, tuple[float, float, NodeKey], list[Pose]] | None = None

        while open_heap:
            f_score, g_score, key, _ = heapq.heappop(open_heap)
            if g_score != g_scores.get(key) or key in closed:
                continue
            if incumbent is not None and f_score >= incumbent[0] - 1e-12:
                break
            closed.add(key)
            pose = poses[key]

            if len(attempts) < self.candidate_limit:
                ordered_goals = sorted(
                    available_goals,
                    key=lambda goal: (
                        g_score + math.dist(pose[:2], goal),
                        goal[0],
                        goal[1],
                    ),
                )
                for goal in ordered_goals:
                    distance = math.dist(pose[:2], goal)
                    if distance > analytic_distance + 1e-12:
                        break
                    goal_pose = self._goal_pose(pose, goal)
                    attempt = (key, goal_pose)
                    if attempt in attempted_pairs:
                        continue
                    attempted_pairs.add(attempt)
                    attempts.append(attempt)
                    analytic = DubinsPath.compute(
                        pose, goal_pose, r_min, step_size=self.sample_step
                    )
                    if self._path_is_safe(analytic.waypoints, mask):
                        path = self._reconstruct(start_key, key, came_from, start)
                        path.extend(tuple(sample) for sample in analytic.waypoints[1:])
                        rank = (analytic.total_length + g_score, goal[0], goal[1], key)
                        candidate = (rank[0], rank[1:], path)
                        if incumbent is None or candidate[:2] < incumbent[:2]:
                            incumbent = candidate
                    if len(attempts) >= self.candidate_limit:
                        break

            for curvature in curvatures:
                edge = self._sample_primitive(pose, curvature, primitive_length)
                if not self._path_is_safe((pose, *edge), mask):
                    continue
                successor = edge[-1]
                successor_key = self._node_key(successor)
                if successor_key in closed:
                    continue
                tentative_g = g_score + primitive_length
                if tentative_g + 1e-12 >= g_scores.get(successor_key, math.inf):
                    continue
                g_scores[successor_key] = tentative_g
                poses[successor_key] = successor
                came_from[successor_key] = (key, edge)
                successor_f = tentative_g + self._heuristic(successor, available_goals)
                heapq.heappush(
                    open_heap,
                    (
                        successor_f,
                        tentative_g,
                        successor_key,
                        next(sequence),
                    ),
                )

        if incumbent is not None:
            return list(incumbent[2])
        raise PathNotFoundError(start, goal_summary, planning_map_version, attempts)

    def _sample_primitive(
        self, pose: Pose, curvature: float, length: float
    ) -> tuple[Pose, ...]:
        steps = max(1, int(math.ceil(length / self.sample_step)))
        return tuple(
            self._integrate(pose, curvature, length * index / steps)
            for index in range(1, steps + 1)
        )

    @staticmethod
    def _integrate(pose: Pose, curvature: float, distance: float) -> Pose:
        col, row, heading = pose
        if abs(curvature) <= 1e-15:
            return (
                col + distance * math.cos(heading),
                row + distance * math.sin(heading),
                heading,
            )
        new_heading = heading + curvature * distance
        return (
            col + (math.sin(new_heading) - math.sin(heading)) / curvature,
            row + (-math.cos(new_heading) + math.cos(heading)) / curvature,
            _wrap_pi(new_heading),
        )

    def _node_key(self, pose: Pose) -> NodeKey:
        heading_step = 2.0 * math.pi / self.heading_bins
        heading_bin = round((pose[2] % (2.0 * math.pi)) / heading_step)
        return (
            round(pose[0] / self.xy_resolution),
            round(pose[1] / self.xy_resolution),
            heading_bin % self.heading_bins,
        )

    @staticmethod
    def _heuristic(pose: Pose, goals: Sequence[tuple[float, float]]) -> float:
        return min(math.dist(pose[:2], goal) for goal in goals)

    @staticmethod
    def _goal_pose(pose: Pose, goal: tuple[float, float]) -> Pose:
        delta_col = goal[0] - pose[0]
        delta_row = goal[1] - pose[1]
        heading = (
            math.atan2(delta_row, delta_col)
            if abs(delta_col) > 1e-12 or abs(delta_row) > 1e-12
            else pose[2]
        )
        return (goal[0], goal[1], _wrap_pi(heading))

    @staticmethod
    def _reconstruct(
        start_key: NodeKey,
        key: NodeKey,
        came_from: dict[NodeKey, tuple[NodeKey, tuple[Pose, ...]]],
        start: Pose,
    ) -> list[Pose]:
        edges: list[tuple[Pose, ...]] = []
        while key != start_key:
            parent, edge = came_from[key]
            edges.append(edge)
            key = parent
        path = [start]
        for edge in reversed(edges):
            path.extend(edge)
        return path

    @staticmethod
    def _normalise_mask(obstacle_mask: np.ndarray) -> np.ndarray:
        mask = np.asarray(obstacle_mask)
        if mask.ndim != 2:
            raise ValueError("obstacle_mask must be a two-dimensional array")
        return mask

    @staticmethod
    def _normalise_pose(pose: Sequence[float]) -> Pose:
        if len(pose) != 3:
            raise ValueError("pose must be an (x, y, heading) triple")
        col, row, heading = map(float, pose)
        if not all(math.isfinite(value) for value in (col, row, heading)):
            raise ValueError("pose values must be finite")
        return (col, row, _wrap_pi(heading))

    @staticmethod
    def _normalise_point(point: Sequence[float], name: str) -> tuple[float, float]:
        if len(point) < 2:
            raise ValueError(f"{name} must contain column and row")
        col, row = float(point[0]), float(point[1])
        if not all(math.isfinite(value) for value in (col, row)):
            raise ValueError(f"{name} values must be finite")
        return (col, row)

    @classmethod
    def _normalise_goals(
        cls, goals: Iterable[Sequence[float]]
    ) -> tuple[tuple[float, float], ...]:
        return tuple(sorted({cls._normalise_point(goal, "goal") for goal in goals}))

    @staticmethod
    def _bbox_boundary(bbox: BBox) -> list[tuple[float, float]]:
        if bbox.col_end <= bbox.col_start or bbox.row_end <= bbox.row_start:
            return []
        col_min, col_max = bbox.col_start, bbox.col_end - 1
        row_min, row_max = bbox.row_start, bbox.row_end - 1
        boundary = {
            (float(col), float(row))
            for col in range(col_min, col_max + 1)
            for row in range(row_min, row_max + 1)
            if col in (col_min, col_max) or row in (row_min, row_max)
        }
        return sorted(boundary)

    @classmethod
    def _standoff_annulus(
        cls,
        target: tuple[float, float],
        radius: float,
        mask: np.ndarray,
    ) -> list[tuple[float, float]]:
        col_start = max(0, math.floor(target[0] - radius - 0.5))
        col_end = min(mask.shape[0] - 1, math.ceil(target[0] + radius + 0.5))
        row_start = max(0, math.floor(target[1] - radius - 0.5))
        row_end = min(mask.shape[1] - 1, math.ceil(target[1] + radius + 0.5))
        candidates = []
        for col in range(col_start, col_end + 1):
            for row in range(row_start, row_end + 1):
                point = (float(col), float(row))
                if abs(math.dist(point, target) - radius) > 0.5:
                    continue
                if not cls._point_blocked(point, mask):
                    candidates.append(point)
        return candidates

    @staticmethod
    def _point_blocked(point: Sequence[float], mask: np.ndarray) -> bool:
        return SafetyEnvelope._point_blocked(float(point[0]), float(point[1]), mask)

    def _path_is_safe(self, path: Sequence[Sequence[float]], mask: np.ndarray) -> bool:
        if not path:
            return False
        previous = path[0]
        if self._point_blocked(previous, mask):
            return False
        for endpoint in path[1:]:
            segment_start = previous
            distance = math.dist(segment_start[:2], endpoint[:2])
            steps = max(1, int(math.ceil(distance / self.sample_step)))
            for index in range(1, steps + 1):
                ratio = index / steps
                sample = (
                    segment_start[0] + (endpoint[0] - segment_start[0]) * ratio,
                    segment_start[1] + (endpoint[1] - segment_start[1]) * ratio,
                )
                if self._point_blocked(sample, mask):
                    return False
                prior_cell = (
                    math.floor(previous[0]),
                    math.floor(previous[1]),
                )
                sample_cell = (math.floor(sample[0]), math.floor(sample[1]))
                if prior_cell[0] != sample_cell[0] and prior_cell[1] != sample_cell[1]:
                    corner_cells = (
                        (sample_cell[0], prior_cell[1]),
                        (prior_cell[0], sample_cell[1]),
                    )
                    if any(
                        SafetyEnvelope._cell_blocked(col, row, mask)
                        for col, row in corner_cells
                    ):
                        return False
                previous = sample
        return True


__all__ = ["AStarNavigator", "PathNotFoundError"]

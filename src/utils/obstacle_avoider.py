"""Curvature-constrained RRT* path planning around raster obstacles."""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
import time
from typing import Sequence

import numpy as np

from src.env.dubins import DubinsPath, Pose


@dataclass
class _Node:
    pose: Pose
    parent: int | None
    cost: float
    edge: list[Pose]


class _NodeSpatialIndex:
    """Uniform-grid index for exact nearest and radius node queries."""

    def __init__(self, bucket_size: float) -> None:
        self.bucket_size = max(float(bucket_size), 1.0)
        self._buckets: dict[tuple[int, int], list[int]] = {}

    def add(self, index: int, pose: Pose) -> None:
        self._buckets.setdefault(self._key(pose), []).append(index)

    def query_radius(
        self,
        point: Sequence[float],
        radius: float,
        nodes: Sequence[_Node],
    ) -> list[int]:
        """Return every indexed node within ``radius`` of ``point``."""
        if radius < 0.0:
            return []
        bucket = self.bucket_size
        min_x = math.floor((point[0] - radius) / bucket)
        max_x = math.floor((point[0] + radius) / bucket)
        min_y = math.floor((point[1] - radius) / bucket)
        max_y = math.floor((point[1] + radius) / bucket)
        radius_sq = radius * radius
        result: list[int] = []
        for cell_x in range(min_x, max_x + 1):
            for cell_y in range(min_y, max_y + 1):
                for index in self._buckets.get((cell_x, cell_y), ()):
                    node = nodes[index]
                    dx = node.pose[0] - point[0]
                    dy = node.pose[1] - point[1]
                    if dx * dx + dy * dy <= radius_sq + 1e-12:
                        result.append(index)
        return result

    def nearest(self, point: Sequence[float], nodes: Sequence[_Node]) -> int:
        """Find an exact nearest node while expanding nearby buckets."""
        if not nodes:
            raise ValueError("cannot query an empty node index")
        radius = self.bucket_size
        max_radius = max(
            self.bucket_size,
            max(math.dist(point[:2], node.pose[:2]) for node in nodes),
        )
        while radius < max_radius:
            candidates = self.query_radius(point, radius, nodes)
            if candidates:
                return min(
                    candidates,
                    key=lambda index: math.dist(nodes[index].pose[:2], point[:2]),
                )
            radius *= 2.0
        candidates = self.query_radius(point, max_radius + 1e-9, nodes)
        if candidates:
            return min(
                candidates,
                key=lambda index: math.dist(nodes[index].pose[:2], point[:2]),
            )
        return min(
            range(len(nodes)),
            key=lambda index: math.dist(nodes[index].pose[:2], point[:2]),
        )

    def _key(self, pose: Sequence[float]) -> tuple[int, int]:
        return (
            math.floor(pose[0] / self.bucket_size),
            math.floor(pose[1] / self.bucket_size),
        )


class ObstaclePlanningTimeout(RuntimeError):
    """Raised when bounded route planning reaches its deadline."""


class ObstacleAvoider:
    def __init__(
        self,
        max_iterations: int = 900,
        step_cells: float = 2.0,
        goal_bias: float = 0.12,
        sample_step: float = 0.2,
        seed: int = 17,
        planning_timeout_seconds: float | None = None,
        max_anchor_candidates: int = 96,
        parent_candidate_limit: int = 12,
    ):
        if max_iterations < 0:
            raise ValueError("max_iterations must be non-negative")
        if step_cells <= 0.0 or not math.isfinite(step_cells):
            raise ValueError("step_cells must be finite and positive")
        if not 0.0 <= goal_bias <= 1.0 or not math.isfinite(goal_bias):
            raise ValueError("goal_bias must be finite and between zero and one")
        if sample_step <= 0.0 or not math.isfinite(sample_step):
            raise ValueError("sample_step must be finite and positive")
        if planning_timeout_seconds is not None and (
            planning_timeout_seconds < 0.0
            or not math.isfinite(planning_timeout_seconds)
        ):
            raise ValueError("planning_timeout_seconds must be non-negative")
        if max_anchor_candidates < 1:
            raise ValueError("max_anchor_candidates must be positive")
        if parent_candidate_limit < 1:
            raise ValueError("parent_candidate_limit must be positive")
        self.max_iterations = int(max_iterations)
        self.step_cells = float(step_cells)
        self.goal_bias = float(goal_bias)
        self.sample_step = float(sample_step)
        self.seed = int(seed)
        self.planning_timeout_seconds = planning_timeout_seconds
        self.max_anchor_candidates = int(max_anchor_candidates)
        self.parent_candidate_limit = int(parent_candidate_limit)
        self.last_plan_stats: dict[str, float | int | str] = {}
        self._planning_started_at = 0.0
        self._planning_deadline: float | None = None

    def plan_path(
        self,
        start_pose: Sequence[float],
        goal_pose: Sequence[float],
        obstacle_mask: np.ndarray,
        R_min: float,
    ) -> list[Pose]:
        self._start_plan()
        start = tuple(map(float, start_pose))
        goal = tuple(map(float, goal_pose))
        self._validate_endpoint(start, obstacle_mask, "start")
        self._validate_endpoint(goal, obstacle_mask, "goal")
        self._check_timeout()

        direct = DubinsPath.compute(start, goal, R_min, self.sample_step)
        if self.is_path_safe(direct.waypoints, obstacle_mask):
            self._finish_plan("direct")
            return direct.waypoints
        local = self._plan_via_local_anchors(start, goal, obstacle_mask, R_min)
        if local:
            self._finish_plan("local_anchor")
            return local

        rng = random.Random(self.seed)
        nodes = [_Node(start, None, 0.0, [start])]
        node_index = _NodeSpatialIndex(max(self.step_cells, 1.0))
        node_index.add(0, start)
        cols, rows = obstacle_mask.shape
        goal_candidates: list[tuple[float, int, list[Pose]]] = []

        for iteration in range(self.max_iterations):
            self._check_timeout(iteration)
            if rng.random() < self.goal_bias:
                sample_xy = goal[:2]
            else:
                sample_xy = (rng.uniform(0.1, cols - 0.1), rng.uniform(0.1, rows - 0.1))
                if self._blocked(sample_xy, obstacle_mask):
                    continue

            nearest_index = node_index.nearest(sample_xy, nodes)
            nearest = nodes[nearest_index]
            desired_heading = math.atan2(sample_xy[1] - nearest.pose[1], sample_xy[0] - nearest.pose[0])
            exploratory = DubinsPath.compute(nearest.pose, (*sample_xy, desired_heading), R_min, self.sample_step)
            edge = self._truncate(exploratory.waypoints, self.step_cells)
            if len(edge) < 2 or not self.is_path_safe(edge, obstacle_mask):
                continue
            new_pose = edge[-1]

            radius = min(5.0, max(2.25, 10.0 * math.sqrt(math.log(len(nodes) + 1) / (len(nodes) + 1))))
            near_indices = node_index.query_radius(new_pose[:2], radius, nodes)
            near_indices = self._shortlist_indices(
                near_indices,
                nodes,
                new_pose[:2],
                self.parent_candidate_limit,
            )
            best_parent = nearest_index
            best_edge = edge
            best_cost = nearest.cost + self._length(edge)
            for idx in near_indices:
                candidate = DubinsPath.compute(nodes[idx].pose, new_pose, R_min, self.sample_step)
                cost = nodes[idx].cost + candidate.total_length
                if cost + 1e-9 < best_cost and self.is_path_safe(candidate.waypoints, obstacle_mask):
                    best_parent, best_edge, best_cost = idx, candidate.waypoints, cost

            new_index = len(nodes)
            nodes.append(_Node(new_pose, best_parent, best_cost, best_edge))
            node_index.add(new_index, new_pose)

            for idx in near_indices:
                if idx == best_parent or idx == 0:
                    continue
                candidate = DubinsPath.compute(new_pose, nodes[idx].pose, R_min, self.sample_step)
                rewired_cost = best_cost + candidate.total_length
                if rewired_cost + 1e-9 < nodes[idx].cost and self.is_path_safe(candidate.waypoints, obstacle_mask):
                    nodes[idx].parent = new_index
                    nodes[idx].cost = rewired_cost
                    nodes[idx].edge = candidate.waypoints

            if math.dist(new_pose[:2], goal[:2]) <= max(3.0, self.step_cells * 1.5):
                final = DubinsPath.compute(new_pose, goal, R_min, self.sample_step)
                if self.is_path_safe(final.waypoints, obstacle_mask):
                    goal_candidates.append((best_cost + final.total_length, new_index, final.waypoints))
                    # Continue briefly to retain RRT* optimisation, then stop.
                    if iteration > max(120, self.max_iterations // 3) and len(goal_candidates) >= 3:
                        break

        if not goal_candidates:
            fallback = self._plan_via_free_anchor(start, goal, obstacle_mask, R_min)
            if fallback:
                self._finish_plan("free_anchor")
                return fallback
            self._finish_plan("failure", reason="no_collision_free_path")
            raise RuntimeError("RRT* could not find a collision-free Dubins path")

        _, node_index, final_edge = min(goal_candidates, key=lambda item: item[0])
        edges = [final_edge]
        while node_index != 0:
            node = nodes[node_index]
            edges.append(node.edge)
            if node.parent is None:
                break
            node_index = node.parent
        edges.reverse()
        path: list[Pose] = [start]
        for edge in edges:
            path.extend(edge[1:] if path and edge else edge)
        if not self.is_path_safe(path, obstacle_mask):
            self._finish_plan("failure", reason="unsafe_generated_path")
            raise RuntimeError("planner generated an unsafe path")
        self._finish_plan("rrt_star")
        return path

    def _start_plan(self) -> None:
        self._planning_started_at = time.monotonic()
        self._planning_deadline = (
            None
            if self.planning_timeout_seconds is None
            else self._planning_started_at + self.planning_timeout_seconds
        )
        self.last_plan_stats = {
            "status": "running",
            "elapsed_seconds": 0.0,
            "iterations": 0,
            "anchor_candidates": 0,
            "anchor_evaluated": 0,
        }

    def _check_timeout(self, iteration: int | None = None) -> None:
        if iteration is not None:
            self.last_plan_stats["iterations"] = iteration
        if self._planning_deadline is None:
            return
        if time.monotonic() < self._planning_deadline:
            return
        self._finish_plan("timeout", reason="planning_timeout")
        raise ObstaclePlanningTimeout(
            "obstacle route planning exceeded its configured deadline"
        )

    def _finish_plan(self, status: str, **details: str | int | float) -> None:
        self.last_plan_stats.update(details)
        self.last_plan_stats["status"] = status
        self.last_plan_stats["elapsed_seconds"] = (
            time.monotonic() - self._planning_started_at
        )

    @staticmethod
    def _shortlist_indices(
        indices: Sequence[int],
        nodes: Sequence[_Node],
        point: Sequence[float],
        limit: int,
    ) -> list[int]:
        """Rank geometric candidates before doing any Dubins computations."""
        if len(indices) <= limit:
            return list(indices)
        distances = np.fromiter(
            (
                (nodes[index].pose[0] - point[0]) ** 2
                + (nodes[index].pose[1] - point[1]) ** 2
                for index in indices
            ),
            dtype=float,
            count=len(indices),
        )
        shortlist_positions = np.argpartition(distances, limit - 1)[:limit]
        return [indices[int(position)] for position in shortlist_positions]

    def _plan_via_local_anchors(
        self,
        start: Pose,
        goal: Pose,
        obstacle_mask: np.ndarray,
        R_min: float,
    ) -> list[Pose]:
        """Try a small deterministic set before invoking the full RRT* search."""
        dx = goal[0] - start[0]
        dy = goal[1] - start[1]
        distance = math.hypot(dx, dy)
        if distance <= 1e-9:
            return []
        ux, uy = dx / distance, dy / distance
        nx, ny = -uy, ux
        cols, rows = obstacle_mask.shape
        offsets = (1.5, 2.0, 2.5, 3.0, 4.0)
        fractions = (0.35, 0.5, 0.65)
        anchor_count = 0
        for fraction in fractions:
            base_x = start[0] + fraction * dx
            base_y = start[1] + fraction * dy
            for multiplier in offsets:
                for sign in (-1.0, 1.0):
                    self._check_timeout()
                    anchor_count += 1
                    self.last_plan_stats["anchor_candidates"] = anchor_count
                    if anchor_count > self.max_anchor_candidates:
                        return []
                    anchor_xy = (
                        base_x + sign * multiplier * R_min * nx,
                        base_y + sign * multiplier * R_min * ny,
                    )
                    if not (
                        0.0 <= anchor_xy[0] < cols
                        and 0.0 <= anchor_xy[1] < rows
                    ) or self._blocked(anchor_xy, obstacle_mask):
                        continue
                    self.last_plan_stats["anchor_evaluated"] = (
                        int(self.last_plan_stats["anchor_evaluated"]) + 1
                    )
                    heading = math.atan2(
                        goal[1] - anchor_xy[1], goal[0] - anchor_xy[0]
                    )
                    anchor = (*anchor_xy, heading)
                    first = DubinsPath.compute(
                        start, anchor, R_min, self.sample_step
                    ).waypoints
                    if not self.is_path_safe(first, obstacle_mask):
                        continue
                    second = DubinsPath.compute(
                        anchor, goal, R_min, self.sample_step
                    ).waypoints
                    if self.is_path_safe(second, obstacle_mask):
                        return [*first, *second[1:]]
        return []

    def _plan_via_free_anchor(
        self,
        start: Pose,
        goal: Pose,
        obstacle_mask: np.ndarray,
        R_min: float,
    ) -> list[Pose]:
        """Deterministic two-leg fallback for a transient RRT* miss.

        The environment contains only sparse, small square hazards.  A
        single free-water anchor is therefore sufficient in the cases where
        stochastic sampling misses a narrow Dubins corridor.  Enumerating
        cell centres keeps the fallback reproducible and preserves the same
        curvature and raster-safety checks as the primary planner.
        """
        cols, rows = obstacle_mask.shape
        if cols < 3 or rows < 3:
            return []
        free_cells = np.argwhere(~np.asarray(obstacle_mask[1:-1, 1:-1], dtype=bool))
        if free_cells.size == 0:
            return []
        remaining_budget = self.max_anchor_candidates - int(
            self.last_plan_stats["anchor_evaluated"]
        )
        if remaining_budget <= 0:
            return []
        free_cells = free_cells + np.array((1, 1), dtype=int)
        anchors_xy = free_cells.astype(float) + 0.5
        lower_bounds = (
            np.hypot(anchors_xy[:, 0] - start[0], anchors_xy[:, 1] - start[1])
            + np.hypot(anchors_xy[:, 0] - goal[0], anchors_xy[:, 1] - goal[1])
        )
        limit = min(remaining_budget, len(anchors_xy))
        if len(anchors_xy) > limit:
            shortlist = np.argpartition(lower_bounds, limit - 1)[:limit]
            shortlist = shortlist[np.argsort(lower_bounds[shortlist], kind="stable")]
        else:
            shortlist = np.argsort(lower_bounds, kind="stable")
        self.last_plan_stats["anchor_candidates"] = int(len(anchors_xy))
        candidates: list[tuple[float, list[Pose]]] = []
        for shortlist_index in shortlist:
            self._check_timeout()
            self.last_plan_stats["anchor_evaluated"] = (
                int(self.last_plan_stats["anchor_evaluated"]) + 1
            )
            anchor_xy = anchors_xy[int(shortlist_index)]
            heading = math.atan2(goal[1] - anchor_xy[1], goal[0] - anchor_xy[0])
            anchor = (float(anchor_xy[0]), float(anchor_xy[1]), heading)
            first = DubinsPath.compute(start, anchor, R_min, self.sample_step).waypoints
            if not self.is_path_safe(first, obstacle_mask):
                continue
            second = DubinsPath.compute(anchor, goal, R_min, self.sample_step).waypoints
            if self.is_path_safe(second, obstacle_mask):
                path = [*first, *second[1:]]
                candidates.append((self._length(path), path))
        if not candidates:
            return []
        return min(candidates, key=lambda item: item[0])[1]

    def is_path_safe(self, waypoints: Sequence[Sequence[float]], obstacle_mask: np.ndarray) -> bool:
        if not waypoints:
            return False
        return all(
            not self._blocked(point, obstacle_mask)
            for point in self._sample_segments(waypoints)
        )

    @staticmethod
    def path_conflicts(waypoints: Sequence[Sequence[float]], obstacle_mask: np.ndarray) -> bool:
        return any(
            ObstacleAvoider._blocked(point, obstacle_mask)
            for point in ObstacleAvoider._sample_segments(waypoints)
        )

    @staticmethod
    def _sample_segments(
        waypoints: Sequence[Sequence[float]],
        spacing: float = 0.2,
    ):
        yield waypoints[0]
        for start, end in zip(waypoints, waypoints[1:]):
            length = math.dist(start[:2], end[:2])
            steps = max(1, int(math.ceil(length / spacing)))
            for index in range(1, steps + 1):
                ratio = index / steps
                yield (
                    start[0] + (end[0] - start[0]) * ratio,
                    start[1] + (end[1] - start[1]) * ratio,
                )

    @staticmethod
    def _blocked(point: Sequence[float], mask: np.ndarray) -> bool:
        col, row = int(math.floor(point[0])), int(math.floor(point[1]))
        return col < 0 or row < 0 or col >= mask.shape[0] or row >= mask.shape[1] or bool(mask[col, row])

    @classmethod
    def _validate_endpoint(cls, pose: Pose, mask: np.ndarray, name: str) -> None:
        if cls._blocked(pose, mask):
            raise ValueError(f"{name} pose is outside the free configuration space")

    @staticmethod
    def _length(path: Sequence[Sequence[float]]) -> float:
        return sum(math.dist(a[:2], b[:2]) for a, b in zip(path, path[1:]))

    @classmethod
    def _truncate(cls, path: list[Pose], max_length: float) -> list[Pose]:
        result = [path[0]]
        travelled = 0.0
        for previous, current in zip(path, path[1:]):
            segment = math.dist(previous[:2], current[:2])
            if travelled + segment <= max_length + 1e-12:
                result.append(current)
                travelled += segment
                continue
            remaining = max_length - travelled
            if remaining > 1e-9 and segment > 1e-12:
                ratio = remaining / segment
                heading_delta = (current[2] - previous[2] + math.pi) % (2 * math.pi) - math.pi
                result.append((
                    previous[0] + (current[0] - previous[0]) * ratio,
                    previous[1] + (current[1] - previous[1]) * ratio,
                    previous[2] + heading_delta * ratio,
                ))
            break
        return result


__all__ = ["ObstacleAvoider", "ObstaclePlanningTimeout"]

"""Curvature-constrained RRT* path planning around raster obstacles."""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Sequence

import numpy as np

from src.env.dubins import DubinsPath, Pose


@dataclass
class _Node:
    pose: Pose
    parent: int | None
    cost: float
    edge: list[Pose]


class ObstacleAvoider:
    def __init__(
        self,
        max_iterations: int = 900,
        step_cells: float = 2.0,
        goal_bias: float = 0.12,
        sample_step: float = 0.2,
        seed: int = 17,
    ):
        self.max_iterations = max_iterations
        self.step_cells = step_cells
        self.goal_bias = goal_bias
        self.sample_step = sample_step
        self.seed = seed

    def plan_path(
        self,
        start_pose: Sequence[float],
        goal_pose: Sequence[float],
        obstacle_mask: np.ndarray,
        R_min: float,
    ) -> list[Pose]:
        start = tuple(map(float, start_pose))
        goal = tuple(map(float, goal_pose))
        self._validate_endpoint(start, obstacle_mask, "start")
        self._validate_endpoint(goal, obstacle_mask, "goal")

        direct = DubinsPath.compute(start, goal, R_min, self.sample_step)
        if self.is_path_safe(direct.waypoints, obstacle_mask):
            return direct.waypoints

        rng = random.Random(self.seed)
        nodes = [_Node(start, None, 0.0, [start])]
        cols, rows = obstacle_mask.shape
        goal_candidates: list[tuple[float, int, list[Pose]]] = []

        for iteration in range(self.max_iterations):
            if rng.random() < self.goal_bias:
                sample_xy = goal[:2]
            else:
                sample_xy = (rng.uniform(0.1, cols - 0.1), rng.uniform(0.1, rows - 0.1))
                if self._blocked(sample_xy, obstacle_mask):
                    continue

            nearest_index = min(
                range(len(nodes)), key=lambda idx: math.dist(nodes[idx].pose[:2], sample_xy)
            )
            nearest = nodes[nearest_index]
            desired_heading = math.atan2(sample_xy[1] - nearest.pose[1], sample_xy[0] - nearest.pose[0])
            exploratory = DubinsPath.compute(nearest.pose, (*sample_xy, desired_heading), R_min, self.sample_step)
            edge = self._truncate(exploratory.waypoints, self.step_cells)
            if len(edge) < 2 or not self.is_path_safe(edge, obstacle_mask):
                continue
            new_pose = edge[-1]

            radius = min(5.0, max(2.25, 10.0 * math.sqrt(math.log(len(nodes) + 1) / (len(nodes) + 1))))
            near_indices = [
                idx for idx, node in enumerate(nodes)
                if math.dist(node.pose[:2], new_pose[:2]) <= radius
            ]
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
            raise RuntimeError("planner generated an unsafe path")
        return path

    def is_path_safe(self, waypoints: Sequence[Sequence[float]], obstacle_mask: np.ndarray) -> bool:
        return bool(waypoints) and all(not self._blocked(pose, obstacle_mask) for pose in waypoints)

    @staticmethod
    def path_conflicts(waypoints: Sequence[Sequence[float]], obstacle_mask: np.ndarray) -> bool:
        return any(ObstacleAvoider._blocked(pose, obstacle_mask) for pose in waypoints)

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


__all__ = ["ObstacleAvoider"]

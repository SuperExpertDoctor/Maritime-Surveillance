"""Dynamic thunderstorms, static islands, and occupancy rasterisation."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import random
from typing import Iterable, Sequence

import numpy as np


Point = tuple[float, float]


@dataclass
class Thunderstorm:
    center: Point
    radius: float
    move_vector: Point = (0.0, 0.0)
    lifetime: float = -1.0
    id: str = "storm"

    def __post_init__(self) -> None:
        self.center = (float(self.center[0]), float(self.center[1]))
        if self.radius <= 0:
            raise ValueError("storm radius must be positive")

    def step(self, dt_min: float, bounds: tuple[int, int] = (30, 30)) -> bool:
        """Move the storm and return whether it remains active."""
        if dt_min < 0:
            raise ValueError("dt_min cannot be negative")
        x = self.center[0] + self.move_vector[0] * dt_min
        y = self.center[1] + self.move_vector[1] * dt_min
        vx, vy = self.move_vector
        if x - self.radius < 0 or x + self.radius > bounds[0]:
            vx = -vx
            x = min(max(x, self.radius), bounds[0] - self.radius)
        if y - self.radius < 0 or y + self.radius > bounds[1]:
            vy = -vy
            y = min(max(y, self.radius), bounds[1] - self.radius)
        self.center = (x, y)
        self.move_vector = (vx, vy)
        if self.lifetime >= 0:
            self.lifetime -= dt_min
        return self.lifetime < 0 or self.lifetime > 0

    def contains(self, point: Sequence[float], safety_margin: float = 0.0) -> bool:
        return math.dist(self.center, (float(point[0]), float(point[1]))) <= self.radius + safety_margin


@dataclass
class Island:
    vertices: list[Point]
    id: str = "island"

    def __post_init__(self) -> None:
        self.vertices = [(float(x), float(y)) for x, y in self.vertices]
        if len(self.vertices) < 3:
            raise ValueError("an island needs at least three vertices")

    @classmethod
    def random_polygon(
        cls,
        center: Point,
        base_radius: float,
        vertex_count: int = 7,
        irregularity: float = 0.3,
        seed: int | None = None,
        island_id: str = "island",
    ) -> "Island":
        rng = random.Random(seed)
        vertices = []
        for index in range(max(5, min(10, vertex_count))):
            angle = 2.0 * math.pi * index / vertex_count + rng.uniform(-0.12, 0.12)
            radius = base_radius * (1.0 + rng.uniform(-irregularity, irregularity))
            vertices.append((
                min(29.0, max(0.0, center[0] + radius * math.cos(angle))),
                min(29.0, max(0.0, center[1] + radius * math.sin(angle))),
            ))
        return cls(vertices, island_id)

    def contains(self, point: Sequence[float]) -> bool:
        x, y = float(point[0]), float(point[1])
        inside = False
        j = len(self.vertices) - 1
        for i, (xi, yi) in enumerate(self.vertices):
            xj, yj = self.vertices[j]
            if (yi > y) != (yj > y):
                crossing_x = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < crossing_x:
                    inside = not inside
            j = i
        return inside

    def distance_to_boundary(self, point: Sequence[float]) -> float:
        px, py = float(point[0]), float(point[1])
        best = float("inf")
        for index, start in enumerate(self.vertices):
            end = self.vertices[(index + 1) % len(self.vertices)]
            best = min(best, _point_segment_distance((px, py), start, end))
        return best


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    vx, vy = end[0] - start[0], end[1] - start[1]
    length2 = vx * vx + vy * vy
    if length2 <= 1e-12:
        return math.dist(point, start)
    t = max(0.0, min(1.0, ((point[0] - start[0]) * vx + (point[1] - start[1]) * vy) / length2))
    projection = (start[0] + t * vx, start[1] + t * vy)
    return math.dist(point, projection)


def obstacle_grid_mask(
    obstacles: Iterable[Thunderstorm | Island],
    resolution: tuple[int, int] = (30, 30),
    storm_safety_margin: float = 2.0,
    island_safety_margin: float = 1.0,
) -> np.ndarray:
    """Rasterise obstacles into the repository's ``mask[col, row]`` form."""
    cols, rows = resolution
    mask = np.zeros((cols, rows), dtype=bool)
    obstacle_list = list(obstacles)
    for col in range(cols):
        for row in range(rows):
            center = (col + 0.5, row + 0.5)
            for obstacle in obstacle_list:
                if isinstance(obstacle, Thunderstorm):
                    # Half-cell padding makes continuous points in an allowed
                    # cell respect the requested radius+margin clearance.
                    blocked = obstacle.contains(center, storm_safety_margin + math.sqrt(0.5))
                else:
                    blocked = obstacle.contains(center) or obstacle.distance_to_boundary(center) <= island_safety_margin + math.sqrt(0.5)
                if blocked:
                    mask[col, row] = True
                    break
    return mask


def default_obstacles(seed: int = 42) -> list[Thunderstorm | Island]:
    """Deterministic environment used by the eight-hour demonstration."""
    return [
        Thunderstorm((8.0, 11.0), 2.3, (0.008, 0.004), -1, "storm-1"),
        Thunderstorm((22.0, 9.0), 2.0, (-0.005, 0.006), -1, "storm-2"),
        Island.random_polygon((15.0, 16.0), 1.7, 7, seed=seed, island_id="island-1"),
        Island.random_polygon((25.0, 22.0), 1.25, 6, seed=seed + 1, island_id="island-2"),
    ]


__all__ = ["Thunderstorm", "Island", "obstacle_grid_mask", "default_obstacles"]

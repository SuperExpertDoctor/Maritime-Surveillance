"""GOAL2 square island and thunderstorm environment models."""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Iterable, Sequence

import numpy as np


Point = tuple[float, float]


@dataclass
class Island:
    """Static square island, expressed in 10 km grid cells."""

    center: Point
    size: int
    id: str = "island-1"
    label: str | None = None

    def __post_init__(self) -> None:
        self.center = (float(self.center[0]), float(self.center[1]))
        self.size = int(self.size)
        if not 1 <= self.size <= 3:
            raise ValueError("island size must be between 1 and 3 cells")
        if self.label is None:
            suffix = self.id.rsplit("-", 1)[-1]
            self.label = f"岛屿-{suffix}"

    @property
    def half_extent(self) -> float:
        return self.size / 2.0

    @property
    def vertices(self) -> list[Point]:
        x, y = self.center
        h = self.half_extent
        return [(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        x, y = self.center
        h = self.half_extent
        return x - h, y - h, x + h, y + h

    def contains(self, point: Sequence[float]) -> bool:
        min_x, min_y, max_x, max_y = self.bounds
        x, y = float(point[0]), float(point[1])
        return min_x <= x <= max_x and min_y <= y <= max_y

    def distance_to_boundary(self, point: Sequence[float]) -> float:
        x, y = float(point[0]), float(point[1])
        min_x, min_y, max_x, max_y = self.bounds
        if self.contains((x, y)):
            return 0.0
        dx = max(min_x - x, 0.0, x - max_x)
        dy = max(min_y - y, 0.0, y - max_y)
        return math.hypot(dx, dy)

    def intersects_segment(self, start: Sequence[float], end: Sequence[float]) -> bool:
        """Exact line-segment vs axis-aligned-square collision check."""
        x0, y0 = float(start[0]), float(start[1])
        x1, y1 = float(end[0]), float(end[1])
        min_x, min_y, max_x, max_y = self.bounds
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for p, q in (
            (-dx, x0 - min_x), (dx, max_x - x0),
            (-dy, y0 - min_y), (dy, max_y - y0),
        ):
            if abs(p) <= 1e-12:
                if q < 0:
                    return False
                continue
            ratio = q / p
            if p < 0:
                if ratio > t1:
                    return False
                t0 = max(t0, ratio)
            else:
                if ratio < t0:
                    return False
                t1 = min(t1, ratio)
        return True


@dataclass
class Thunderstorm:
    """Moving square thunderstorm.  Its center supports sub-cell movement."""

    center: Point
    size: int
    move_vector: Point = (0.0, 0.0)
    lifetime: float = -1.0
    intensity: float = 0.5
    id: str = "storm-1"

    def __post_init__(self) -> None:
        self.center = (float(self.center[0]), float(self.center[1]))
        self.size = int(self.size)
        self.move_vector = (float(self.move_vector[0]), float(self.move_vector[1]))
        self.intensity = float(self.intensity)
        if not 1 <= self.size <= 4:
            raise ValueError("thunderstorm size must be between 1 and 4 cells")
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError("thunderstorm intensity must be between zero and one")

    @property
    def half_extent(self) -> float:
        return self.size / 2.0

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        x, y = self.center
        h = self.half_extent
        return x - h, y - h, x + h, y + h

    @property
    def vertices(self) -> list[Point]:
        min_x, min_y, max_x, max_y = self.bounds
        return [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]

    def contains(self, point: Sequence[float], safety_margin: float = 0.0) -> bool:
        x, y = float(point[0]), float(point[1])
        h = self.half_extent + max(0.0, safety_margin)
        return abs(x - self.center[0]) <= h and abs(y - self.center[1]) <= h

    def distance_to_boundary(self, point: Sequence[float]) -> float:
        x, y = float(point[0]), float(point[1])
        dx = max(abs(x - self.center[0]) - self.half_extent, 0.0)
        dy = max(abs(y - self.center[1]) - self.half_extent, 0.0)
        return math.hypot(dx, dy)

    def step(self, dt_min: float, bounds: tuple[int, int] = (30, 30)) -> bool:
        """Advance, reflect at map boundaries, and report whether still live."""
        if dt_min < 0:
            raise ValueError("dt_min cannot be negative")
        x = self.center[0] + self.move_vector[0] * dt_min
        y = self.center[1] + self.move_vector[1] * dt_min
        vx, vy = self.move_vector
        h = self.half_extent
        if x - h < 0.0 or x + h > bounds[0]:
            vx = -vx
            x = min(max(x, h), bounds[0] - h)
        if y - h < 0.0 or y + h > bounds[1]:
            vy = -vy
            y = min(max(y, h), bounds[1] - h)
        self.center = (x, y)
        self.move_vector = (vx, vy)
        if self.lifetime > 0:
            self.lifetime = max(0.0, self.lifetime - dt_min)
            return self.lifetime > 0
        return True


def obstacle_grid_mask(
    obstacles: Iterable[Thunderstorm | Island],
    resolution: tuple[int, int] = (30, 30),
    storm_safety_margin: float = 1.0,
    include_islands: bool = False,
) -> np.ndarray:
    """Rasterise no-fly hazards, optionally including static islands."""
    cols, rows = resolution
    mask = np.zeros((cols, rows), dtype=bool)
    for obstacle in obstacles:
        if isinstance(obstacle, Island) and not include_islands:
            continue
        for col in range(cols):
            for row in range(rows):
                if isinstance(obstacle, Thunderstorm):
                    blocked = obstacle.contains((col + 0.5, row + 0.5), storm_safety_margin)
                else:
                    blocked = obstacle.contains((col + 0.5, row + 0.5))
                if blocked:
                    mask[col, row] = True
    return mask


def default_obstacles(
    seed: int = 42,
    *,
    base_positions: Iterable[Sequence[float]] = (),
    island_count: int | None = None,
    thunderstorm_count: int | None = None,
    base_clearance_cells: float = 4.0,
    resolution: tuple[int, int] = (30, 30),
) -> list[Thunderstorm | Island]:
    """Generate a deterministic sparse open-water state for one reset."""
    rng = random.Random(seed)
    cols, rows = resolution
    bases = [tuple(map(float, position[:2])) for position in base_positions]
    islands: list[Island] = []
    requested_islands = island_count if island_count is not None else rng.randint(0, 2)
    for index in range(requested_islands):
        for _ in range(300):
            size = rng.randint(1, 2)
            h = size / 2.0
            center = (rng.uniform(2.0 + h, cols - 2.0 - h), rng.uniform(2.0 + h, rows - 2.0 - h))
            candidate = Island(center, size, f"island-{index + 1}")
            if any(
                candidate.distance_to_boundary((base[0] + 0.5, base[1] + 0.5))
                < base_clearance_cells
                for base in bases
            ):
                continue
            if any(candidate.distance_to_boundary(other.center) < 1.0 + other.half_extent for other in islands):
                continue
            islands.append(candidate)
            break
    storms: list[Thunderstorm] = []
    requested_storms = thunderstorm_count if thunderstorm_count is not None else rng.randint(2, 3)
    for index in range(requested_storms):
        for _ in range(200):
            size = rng.randint(1, 2)
            h = size / 2.0
            # Keep the one-cell airborne safety margin clear of the coastal
            # launch/recovery strip, including every generated base.
            center = (
                rng.uniform(h + 1.0, cols - h - 1.0),
                rng.uniform(h + 1.0, rows - h - 1.0),
            )
            candidate = Thunderstorm(center, size)
            if any(
                candidate.distance_to_boundary((base[0] + 0.5, base[1] + 0.5))
                < base_clearance_cells
                for base in bases
            ):
                continue
            storms.append(Thunderstorm(
                center,
                size,
                (rng.uniform(-0.05, 0.05), rng.uniform(-0.05, 0.05)),
                rng.choice([-1.0, rng.uniform(90.0, 240.0)]),
                rng.uniform(0.3, 1.0),
                f"storm-{index + 1}",
            ))
            break
    return [*storms, *islands]


__all__ = ["Thunderstorm", "Island", "obstacle_grid_mask", "default_obstacles"]

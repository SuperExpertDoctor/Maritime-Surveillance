import math

import numpy as np
import pytest

from src.env.dubins import DubinsPath
from src.env.obstacle import Thunderstorm, obstacle_grid_mask
from src.utils.obstacle_avoider import ObstacleAvoider


def path_length(path):
    return sum(math.dist(a[:2], b[:2]) for a, b in zip(path, path[1:]))


def test_no_obstacle_returns_direct_shortest_dubins_path():
    mask = np.zeros((20, 20), dtype=bool)
    planner = ObstacleAvoider()
    path = planner.plan_path((1, 2, 0), (15, 12, math.pi / 2), mask, 1)
    expected = DubinsPath.compute((1, 2, 0), (15, 12, math.pi / 2), 1, planner.sample_step)
    # Sampled arc chords converge to the analytic length from below.
    assert path_length(path) == pytest.approx(expected.total_length, rel=2e-4)


def test_rrt_star_dubins_keeps_storm_clearance():
    storm = Thunderstorm((8, 5), radius=1)
    mask = obstacle_grid_mask([storm], resolution=(20, 12))
    planner = ObstacleAvoider(max_iterations=1200, seed=2)
    path = planner.plan_path((1, 5, 0), (16, 5, 0), mask, 1)
    assert planner.is_path_safe(path, mask)
    assert min(math.dist(pose[:2], storm.center) for pose in path) >= storm.radius + 2

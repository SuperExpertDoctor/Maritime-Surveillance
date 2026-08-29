import math

import numpy as np
import pytest

from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.env.dubins import DubinsPath, DubinsResult
from src.schedule.datatypes import BBox, GridCoord
from src.utils.obstacle_avoider import ObstacleAvoider


def _on_bbox_boundary(point, bbox):
    col, row = point
    col_max = bbox.col_end - 1
    row_max = bbox.row_end - 1
    within_cols = bbox.col_start <= col <= col_max
    within_rows = bbox.row_start <= row <= row_max
    return (
        within_cols
        and within_rows
        and (col in (bbox.col_start, col_max) or row in (bbox.row_start, row_max))
    )


def _all_samples_respect_curvature(path, r_min):
    for first, second in zip(path, path[1:]):
        distance = math.dist(first[:2], second[:2])
        if distance <= 1e-9:
            continue
        heading_change = abs(
            (second[2] - first[2] + math.pi) % (2.0 * math.pi) - math.pi
        )
        if heading_change / distance > 1.02 / r_min:
            return False
    return True


def test_astar_returns_a_deterministic_direct_curvature_safe_path():
    mask = np.zeros((24, 12), dtype=bool)
    navigator = AStarNavigator()

    path = navigator.plan_grid((2.0, 5.0, 0.0), {(18.0, 5.0)}, mask, r_min=1.0)
    repeated = navigator.plan_grid((2.0, 5.0, 0.0), {(18.0, 5.0)}, mask, r_min=1.0)

    assert path == repeated
    assert path[0] == (2.0, 5.0, 0.0)
    assert path[-1][:2] == pytest.approx((18.0, 5.0))
    assert all(isinstance(pose, tuple) for pose in path)
    assert ObstacleAvoider().is_path_safe(path, mask)
    assert _all_samples_respect_curvature(path, r_min=1.0)


def test_astar_detours_around_a_wall_instead_of_using_line_of_sight():
    mask = np.zeros((24, 16), dtype=bool)
    mask[11, :12] = True

    path = AStarNavigator().plan_grid((3.0, 5.0, 0.0), {(20.0, 5.0)}, mask, r_min=1.0)

    assert path[-1][:2] == pytest.approx((20.0, 5.0))
    assert max(pose[1] for pose in path) >= 12.0
    assert ObstacleAvoider().is_path_safe(path, mask)
    assert _all_samples_respect_curvature(path, r_min=1.0)


def test_astar_reports_unreachable_goal_with_snapshot_diagnostics():
    mask = np.zeros((10, 10), dtype=bool)
    mask[5, :] = True
    navigator = AStarNavigator()

    with pytest.raises(PathNotFoundError) as caught:
        navigator.plan_grid(
            (2.0, 4.0, 0.0),
            {(8.0, 4.0)},
            mask,
            r_min=1.0,
            planning_map_version=19,
        )

    error = caught.value
    assert error.start == (2.0, 4.0, 0.0)
    assert error.planning_map_version == 19
    assert error.attempt_count >= 0
    assert "grid goals=1" in error.goal_summary
    assert "planning_map_version=19" in str(error)
    assert "analytic_attempts=" in str(error)


def test_astar_does_not_cut_a_blocked_diagonal_corner():
    mask = np.ones((4, 4), dtype=bool)
    mask[1, 1] = False
    mask[2, 2] = False
    mask[2, 1] = True
    mask[1, 2] = True
    navigator = AStarNavigator(sample_step=0.2)

    with pytest.raises(PathNotFoundError):
        navigator.plan_grid((1.0, 1.0, 0.0), {(2.0, 2.0)}, mask, r_min=1.0)


def test_region_path_ends_on_an_unblocked_inner_boundary_and_is_safe():
    mask = np.zeros((30, 30), dtype=bool)
    bbox = BBox(10, 10, 15, 15)
    mask[10, 10] = True

    path = AStarNavigator().plan_to_region((2.0, 2.0, 0.0), bbox, mask, r_min=1.0)

    assert _on_bbox_boundary(path[-1][:2], bbox)
    assert not mask[math.floor(path[-1][0]), math.floor(path[-1][1])]
    assert ObstacleAvoider().is_path_safe(path, mask)


def test_standoff_path_ends_on_the_requested_annulus():
    mask = np.zeros((32, 32), dtype=bool)

    path = AStarNavigator().plan_to_standoff(
        (3.0, 15.0, 0.0),
        GridCoord(20, 15),
        radius=5.0,
        obstacle_mask=mask,
        r_min=1.0,
        planning_map_version=7,
    )

    assert math.dist(path[-1][:2], (20.0, 15.0)) == pytest.approx(5.0)
    assert ObstacleAvoider().is_path_safe(path, mask)


def test_standoff_path_preserves_a_non_integer_safe_radius():
    mask = np.zeros((32, 32), dtype=bool)
    navigator = AStarNavigator()
    arguments = (
        (3.0, 15.0, 0.0),
        GridCoord(20, 15),
    )
    path = navigator.plan_to_standoff(
        *arguments, radius=1.8, obstacle_mask=mask, r_min=1.0
    )
    repeated = navigator.plan_to_standoff(
        *arguments, radius=1.8, obstacle_mask=mask, r_min=1.0
    )

    assert path == repeated
    assert math.dist(path[-1][:2], (20.0, 15.0)) == pytest.approx(1.8)
    assert ObstacleAvoider().is_path_safe(path, mask)


def test_large_turn_radius_can_still_turn_in_open_space():
    mask = np.zeros((80, 80), dtype=bool)
    path = AStarNavigator(heading_bins=72).plan_grid(
        (10.0, 10.0, 0.0), {(10.0, 40.0)}, mask, r_min=8.0
    )

    assert path[-1][1] == pytest.approx(40.0, abs=1.0)
    assert _all_samples_respect_curvature(path, r_min=8.0)


def test_unsafe_dubins_final_connection_is_rejected(monkeypatch):
    mask = np.zeros((20, 12), dtype=bool)
    mask[10, 6] = True
    original_compute = DubinsPath.compute
    call_count = 0

    def unsafe_once(cls, start_pose, end_pose, R_min, step_size=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            start = tuple(map(float, start_pose))
            end = tuple(map(float, end_pose))
            blocked = (10.2, 6.2, 0.0)
            return DubinsResult(
                "LSL",
                math.dist(start[:2], blocked[:2]) + math.dist(blocked[:2], end[:2]),
                [start, blocked, end],
                (0.0, 0.0, 0.0),
            )
        return original_compute(start_pose, end_pose, R_min, step_size)

    monkeypatch.setattr(DubinsPath, "compute", classmethod(unsafe_once))

    path = AStarNavigator().plan_grid((2.0, 6.0, 0.0), {(16.0, 6.0)}, mask, r_min=1.0)

    assert call_count >= 2
    assert path[-1][:2] == pytest.approx((16.0, 6.0))
    assert ObstacleAvoider().is_path_safe(path, mask)


def test_astar_reaches_grid_goal_after_unsafe_analytic_attempt_limit(monkeypatch):
    mask = np.ones((45, 35), dtype=bool)
    mask[:, 17] = False
    navigator = AStarNavigator(heading_bins=144)
    call_count = 0

    def always_unsafe(cls, start_pose, end_pose, R_min, step_size=None):
        nonlocal call_count
        call_count += 1
        start = tuple(map(float, start_pose))
        end = tuple(map(float, end_pose))
        blocked = (0.2, 0.2, 0.0)
        return DubinsResult(
            "LSL",
            math.dist(start[:2], blocked[:2]) + math.dist(blocked[:2], end[:2]),
            [start, blocked, end],
            (0.0, 0.0, 0.0),
        )

    monkeypatch.setattr(DubinsPath, "compute", classmethod(always_unsafe))

    path = navigator.plan_grid((4.0, 17.0, 0.0), {(40.0, 17.0)}, mask, r_min=16.0)

    assert call_count == navigator.candidate_limit == 32
    assert path[-1][:2] == pytest.approx((40.0, 17.0))
    assert ObstacleAvoider().is_path_safe(path, mask)

import math

import pytest

from src.env.dubins import DubinsPath


def test_straight_path_has_exact_length():
    result = DubinsPath.compute((0, 0, 0), (10, 0, 0), 1.0, step_size=0.1)
    assert result.total_length == pytest.approx(10.0, abs=1e-9)
    assert result.waypoints[-1] == pytest.approx((10.0, 0.0, 0.0))


def test_screen_clockwise_quarter_turn_is_rsr():
    result = DubinsPath.compute((0, 0, 0), (10, 10, math.pi / 2), 1.0, step_size=0.05)
    assert result.path_type == "RSR"
    assert result.waypoints[-1] == pytest.approx((10.0, 10.0, math.pi / 2))


def test_u_turn_is_smooth_and_curvature_bounded():
    result = DubinsPath.compute((0, 0, 0), (0, 10, math.pi), 1.0, step_size=0.05)
    assert result.path_type in {"LSL", "LSR", "RSL", "RSR", "LRL", "RLR"}
    for first, second in zip(result.waypoints, result.waypoints[1:]):
        distance = math.dist(first[:2], second[:2])
        if distance <= 1e-8:
            continue
        heading_change = abs((second[2] - first[2] + math.pi) % (2 * math.pi) - math.pi)
        assert heading_change / distance <= 1.01


def test_all_extreme_heading_inputs_produce_finite_paths():
    for degrees in (1, 89, 179, 181, 271, 359):
        result = DubinsPath.compute((0, 0, 0), (0, 10, math.radians(degrees)), 1.0)
        assert math.isfinite(result.total_length)
        assert result.path_type in {"LSL", "LSR", "RSL", "RSR", "LRL", "RLR"}


@pytest.mark.parametrize(
    "start,end,radius",
    [
        ((1, 1, 0), (8, 3, math.pi / 4), 1.0),
        ((8, 3, math.pi / 4), (2, 9, math.pi), 1.0),
        ((4, 12, -math.pi / 2), (14, 2, 0), 1.5),
        ((20, 20, math.pi), (6, 18, -math.pi / 3), 2.0),
        ((2, 25, math.pi / 2), (25, 4, -math.pi / 2), 0.75),
        ((15, 15, 0), (15, 15, math.pi), 1.0),
        ((3, 3, math.radians(179)), (3, 18, math.radians(1)), 1.0),
        ((27, 2, math.pi), (5, 27, math.pi / 2), 1.25),
        ((9, 4, -2.4), (18, 23, 2.2), 1.0),
        ((12, 26, 0.3), (2, 6, -2.7), 1.8),
    ],
)
def test_known_pose_suite_has_exact_endpoints_and_bounded_curvature(start, end, radius):
    result = DubinsPath.compute(start, end, radius, step_size=0.04)
    assert result.waypoints[0][:2] == pytest.approx(start[:2])
    assert result.waypoints[-1][:2] == pytest.approx(end[:2])
    assert abs(
        (result.waypoints[0][2] - start[2] + math.pi) % (2 * math.pi) - math.pi
    ) < 1e-9
    assert abs(
        (result.waypoints[-1][2] - end[2] + math.pi) % (2 * math.pi) - math.pi
    ) < 1e-9
    assert result.total_length >= math.dist(start[:2], end[:2])
    assert result.path_type in {"LSL", "LSR", "RSL", "RSR", "LRL", "RLR"}
    for first, second in zip(result.waypoints, result.waypoints[1:]):
        distance = math.dist(first[:2], second[:2])
        if distance <= 1e-8:
            continue
        heading_change = abs(
            (second[2] - first[2] + math.pi) % (2 * math.pi) - math.pi
        )
        assert heading_change / distance <= 1.02 / radius

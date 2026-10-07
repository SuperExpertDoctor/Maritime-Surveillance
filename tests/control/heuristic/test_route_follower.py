"""RouteFollower overshot-waypoint recovery.

A waypoint that sits behind the nose inside the tightest turn circle is
unreachable: pure pursuit commands a hairpin the airframe cannot complete,
which orbits the follower in place forever.  The follower must skip it and
pursue the next point on the same route.
"""

import math

import numpy as np

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ControlObservation,
    ControlMode,
    ControlOwner,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.control.heuristic.base import RouteFollower


ACTION_SPEC = ActionSpec(-1.0, 1.0, 0.5, 1.0)
DT_MIN = 1.0


def make_observation(
    position: tuple[float, float],
    heading_rad: float,
    *,
    speed: float = 1.0,
) -> ControlObservation:
    arrays = np.zeros((30, 30), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=DT_MIN,
        self_state=UAVObservation(
            uav_id="uav-1",
            position=position,
            heading_rad=heading_rad,
            speed_cells_min=speed,
            remaining_range_cells=100.0,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.SYSTEM,
            operation_mode=OperationMode.RETURN,
            sensor_mode=SensorMode.OFF,
            safety_intervened=False,
        ),
        local_info=arrays,
        local_value=arrays,
        obstacle_mask=arrays,
        searchable_mask=np.ones((30, 30), dtype=bool),
        planning_obstacle_mask=arrays,
        planning_map_version=1,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.SAR),
            (OperationMode.RETURN, OperationMode.HOLDING),
            (),
        ),
    )


def step_pose(position, heading, command, dt=DT_MIN):
    heading += command.turn_rate_rad_min * dt
    heading = (heading + math.pi) % (2.0 * math.pi) - math.pi
    return (
        position[0] + command.speed_cells_min * math.cos(heading) * dt,
        position[1] + command.speed_cells_min * math.sin(heading) * dt,
    ), heading


# Mirrors the live 6h soak deadlock: a hairpin entry segment the airframe
# already overshot, followed by a straight westbound descent to base.
HAIRPIN_ROUTE = (
    (5.13, 24.36, -2.06),
    (5.80, 24.49, -2.87),
    (5.61, 24.46, -3.07),
    (5.41, 24.46, 3.01),
    (5.21, 24.51, 2.81),
    (4.85, 24.68, 2.81),
    (4.46, 24.73, -3.07),
    (4.07, 24.63, -2.67),
    (3.49, 24.53, 3.01),
    (2.94, 24.76, 2.61),
    (2.43, 25.07, 2.61),
    (2.07, 25.25, 2.81),
    (1.68, 25.30, -3.07),
    (1.30, 25.20, -2.67),
    (1.04, 25.02, -2.51),
    (1.0, 25.0, -2.55),
)
BASE = HAIRPIN_ROUTE[-1][:2]


def test_follower_skips_overshot_waypoints_and_reaches_base():
    follower = RouteFollower(HAIRPIN_ROUTE)
    position, heading = (5.5, 24.0), -2.06

    for _ in range(120):
        observation = make_observation(position, heading)
        command = follower.next_command(
            observation, ACTION_SPEC, SensorMode.OFF, OperationMode.RETURN
        )
        position, heading = step_pose(position, heading, command)
        if math.dist(position, BASE) <= 1.0:
            break

    assert follower.index > 1, "follower never advanced past the hairpin"
    assert math.dist(position, BASE) <= 1.0


def test_follower_does_not_skip_a_waypoint_that_is_ahead():
    follower = RouteFollower(HAIRPIN_ROUTE)
    # Heading almost exactly toward waypoint 1 from inside the overshot
    # radius: it must be pursued, not skipped.
    bearing = math.atan2(24.49 - 24.6, 5.80 - 7.5)
    observation = make_observation((7.5, 24.6), bearing)
    follower.next_command(
        observation, ACTION_SPEC, SensorMode.OFF, OperationMode.RETURN
    )
    assert follower.index == 0


def test_follower_pursues_a_waypoint_far_behind_the_nose():
    # A waypoint beyond the turn-circle zone stays the pursuit target: the
    # airframe turns around and re-acquires it instead of corner-cutting.
    route = (
        (0.0, 0.0, 0.0),
        (-8.0, 0.0, 0.0),
        (-9.0, 0.0, 0.0),
    )
    follower = RouteFollower(route)
    observation = make_observation((0.0, 0.0), 0.0)
    command = follower.next_command(
        observation, ACTION_SPEC, SensorMode.OFF, OperationMode.RETURN
    )
    assert follower.index == 0
    # It must turn back toward the waypoint, not run past it.
    assert abs(command.turn_rate_rad_min) > 0.5

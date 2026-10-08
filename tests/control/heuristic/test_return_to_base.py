"""En-route SAR imaging on recovery legs."""

import math

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ControlObservation,
    ControlTask,
    ControlOwner,
    ControlMode,
    OperationMode,
    RecoveryPlan,
    SensorMode,
    UAVObservation,
)
from src.control.heuristic.return_to_base import (
    ReturnToBaseController,
    SystemHoldingController,
)


def make_observation(
    *,
    heading_rad: float = 0.0,
    position: tuple[float, float] = (2.0, 10.0),
    obstacle_mask: np.ndarray | None = None,
    operation_mode: OperationMode = OperationMode.RETURN,
    allowed_modes: tuple[OperationMode, ...] = (
        OperationMode.RETURN,
        OperationMode.HOLDING,
    ),
) -> ControlObservation:
    arrays = np.zeros((30, 30), dtype=bool)
    if obstacle_mask is None:
        obstacle_mask = arrays
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="uav-1",
            position=position,
            heading_rad=heading_rad,
            speed_cells_min=1.0,
            remaining_range_cells=100.0,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.SYSTEM,
            operation_mode=operation_mode,
            sensor_mode=SensorMode.OFF,
            safety_intervened=False,
        ),
        local_info=arrays,
        local_value=arrays,
        obstacle_mask=arrays,
        searchable_mask=np.ones((30, 30), dtype=bool),
        planning_obstacle_mask=obstacle_mask,
        planning_map_version=1,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.SAR),
            allowed_modes,
            (),
        ),
    )


@pytest.fixture
def action_spec():
    return ActionSpec(-2.0, 2.0, 0.5, 1.0)


@pytest.fixture
def controller(action_spec):
    return ReturnToBaseController(
        observation_spec=__import__(
            "src.control.common.contracts", fromlist=["ObservationSpec"]
        ).ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
    )


def straight_plan() -> RecoveryPlan:
    return RecoveryPlan(
        base_id="B1",
        base_position=(8.0, 10.0),
        reservation_id="R1",
        path=((2.0, 10.0, 0.0), (8.0, 10.0, 0.0)),
        path_length_cells=6.0,
        reserve_cells=0.5,
        planning_map_version=1,
    )


def test_return_straight_leg_commands_sar(controller):
    observation = make_observation(heading_rad=0.0)
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=straight_plan()),
        observation,
    )

    decision = controller.act(observation)

    assert decision.command.operation_mode is OperationMode.RETURN
    assert decision.command.sensor_mode is SensorMode.SAR
    assert decision.command.sar_look_direction == "left"
    assert decision.command.sar_scan_heading_rad == pytest.approx(0.0)
    assert decision.command.sar_scan_origin == pytest.approx((2.0, 10.0))


def test_return_leg_turning_stays_sensor_off(controller):
    # Heading 90° away from the straight eastbound leg: the follower
    # commands a hard turn, so en-route imaging must stay off.
    observation = make_observation(heading_rad=-math.pi / 2)
    plan = RecoveryPlan(
        base_id="B1",
        base_position=(8.0, 10.0),
        reservation_id="R1",
        path=((2.0, 10.0, -math.pi / 2), (8.0, 10.0, 0.0)),
        path_length_cells=6.0,
        reserve_cells=0.5,
        planning_map_version=1,
    )
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=plan),
        observation,
    )

    decision = controller.act(observation)

    assert abs(decision.command.turn_rate_rad_min) > 0.1
    assert decision.command.sensor_mode is SensorMode.OFF


@pytest.fixture
def holding(action_spec):
    return SystemHoldingController(
        observation_spec=__import__(
            "src.control.common.contracts", fromlist=["ObservationSpec"]
        ).ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        orbit_radius_cells=2.0,
    )


def test_system_holding_sweeps_a_local_box_instead_of_orbiting(holding):
    # A parked airframe must survey a small local patch — circling an
    # empty point is forbidden airspace behaviour.
    mask = np.zeros((30, 30), dtype=bool)
    observation = make_observation(
        position=(14.5, 13.5),
        heading_rad=0.0,
        obstacle_mask=mask,
        operation_mode=OperationMode.HOLDING,
        allowed_modes=(OperationMode.HOLDING, OperationMode.RETURN),
    )
    holding.start_task(ControlTask("H1", OperationMode.HOLDING), observation)

    route = holding.route_snapshot().route
    assert len(route) >= 4
    leg_target = route[1]
    on_leg = make_observation(
        position=(leg_target[0] - 2.0, leg_target[1]),
        heading_rad=leg_target[2],
        obstacle_mask=mask,
        operation_mode=OperationMode.HOLDING,
        allowed_modes=(OperationMode.HOLDING, OperationMode.RETURN),
    )
    decision = holding.act(on_leg)

    # On a sweep leg the command is essentially straight — an orbit
    # would emit a constant ~speed/radius turn every tick.
    assert decision.command.operation_mode is OperationMode.HOLDING
    assert abs(decision.command.turn_rate_rad_min) < 0.1
    assert decision.command.sensor_mode is SensorMode.OFF


def test_system_holding_no_sweep_holds_straight_or_parks_never_orbits(holding):
    # A pocketed airframe must not orbit: with no sweepable patch it
    # holds a straight minimum-speed leg, and if every first step is
    # illegal the safety envelope parks it — never a circle.
    mask = np.ones((30, 30), dtype=bool)
    mask[15, 15] = False  # only the airframe's own cell is free
    observation = make_observation(
        position=(15.5, 15.5),
        obstacle_mask=mask,
        operation_mode=OperationMode.HOLDING,
        allowed_modes=(OperationMode.HOLDING, OperationMode.RETURN),
    )
    holding.start_task(ControlTask("H1", OperationMode.HOLDING), observation)

    assert holding.route_snapshot().route == ()
    from src.control.common.safety import UnsafeControlState

    with pytest.raises(UnsafeControlState):
        holding.act(observation)

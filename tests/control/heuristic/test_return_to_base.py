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
from src.control.heuristic.return_to_base import ReturnToBaseController


def make_observation(*, heading_rad: float = 0.0) -> ControlObservation:
    arrays = np.zeros((30, 30), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="uav-1",
            position=(2.0, 10.0),
            heading_rad=heading_rad,
            speed_cells_min=1.0,
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

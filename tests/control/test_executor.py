import math

import pytest

from src.control.common.contracts import (
    ActionSpec,
    ControlCommand,
    OperationMode,
    SensorMode,
)
from src.control.common.executor import UAVDynamicsExecutor
from src.control.common.safety import SafetyEnvelope
from src.env.uav_entity import UAVEntity
from src.schedule.datatypes import GridCoord


@pytest.fixture
def uav():
    entity = UAVEntity(
        "UAV-1", GridCoord(10, 10), endurance_h=8.0, cruise_speed_kmh=160.0
    )
    entity.heading_rad = 0.0
    return entity


def test_executor_integrates_midpoint_motion_and_consumes_actual_range(uav):
    executor = UAVDynamicsExecutor()
    command = ControlCommand(
        turn_rate_rad_min=0.2,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    before = uav.remaining_range_cells
    result = executor.execute(uav, command, dt_min=1.0)

    assert result.distance_cells == pytest.approx(0.25)
    assert uav.remaining_range_cells == pytest.approx(before - 0.25)
    assert uav.heading_rad == pytest.approx(0.2)
    assert uav.float_position == pytest.approx(
        (10.0 + 0.25 * math.cos(0.1), 10.0 + 0.25 * math.sin(0.1))
    )


def test_executor_updates_legacy_state_trail_and_command_audit_fields(uav):
    executor = UAVDynamicsExecutor()
    command = ControlCommand(
        turn_rate_rad_min=0.0,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.EO,
        operation_mode=OperationMode.TRACK,
        target_contact_id="G1",
    )

    result = executor.execute(uav, command, dt_min=1.0)

    assert result.command is command
    assert uav.status == "tracking"
    assert uav.sensor_mode == "eo"
    assert uav.trail[-1] == pytest.approx(uav.float_position)
    assert uav.last_requested_command is command
    assert uav.last_applied_command is command


def test_executor_preserves_distinct_requested_and_safety_applied_commands(uav):
    requested = ControlCommand(
        turn_rate_rad_min=1.0,
        speed_cells_min=1.0,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.TRANSIT,
    )
    observation = make_observation_for_executor(uav)
    safety = SafetyEnvelope(
        ActionSpec(-0.2, 0.2, 0.1, 0.5)
    ).apply(requested, observation, dt_min=1.0)

    result = UAVDynamicsExecutor().execute(uav, safety, dt_min=1.0)

    assert result.requested_command is requested
    assert result.applied_command is safety.applied_command
    assert result.command is safety.applied_command
    assert result.requested_command != result.applied_command
    assert uav.last_requested_command is requested
    assert uav.last_applied_command is safety.applied_command


def make_observation_for_executor(uav):
    from src.control.common.contracts import (
        ActionMask,
        ControlMode,
        ControlObservation,
        ControlOwner,
        UAVObservation,
    )
    import numpy as np

    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id=uav.id,
            position=uav.float_position,
            heading_rad=uav.heading_rad,
            speed_cells_min=0.0,
            remaining_range_cells=uav.remaining_range_cells,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.HEURISTIC,
            operation_mode=OperationMode.TRANSIT,
            sensor_mode=SensorMode.OFF,
            safety_intervened=False,
        ),
        local_info=np.zeros((1, 1), dtype=np.float32),
        local_value=np.zeros((1, 1), dtype=np.float32),
        obstacle_mask=np.zeros((1, 1), dtype=bool),
        searchable_mask=np.ones((1, 1), dtype=bool),
        planning_obstacle_mask=np.zeros((30, 30), dtype=bool),
        planning_map_version=1,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.SAR, SensorMode.EO),
            (OperationMode.TRANSIT,),
            (),
        ),
    )

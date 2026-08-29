import math

import pytest

from src.control.common.contracts import ControlCommand, OperationMode, SensorMode
from src.control.common.executor import UAVDynamicsExecutor
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

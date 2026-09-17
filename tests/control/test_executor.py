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


def test_executor_installs_sar_geometry_and_gates_imaging_on_heading_error(uav):
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.SAR,
        OperationMode.COVERAGE,
        sar_look_direction="right",
        sar_scan_heading_rad=0.0,
        sar_scan_origin=(10.0, 10.0),
    )

    result = UAVDynamicsExecutor().execute(uav, command, dt_min=1.0)

    assert result.applied_command is command
    assert uav.sar_look_direction == "right"
    assert uav.sar_scan_heading_rad == pytest.approx(0.0)
    assert uav.sar_scan_origin == pytest.approx((10.0, 10.0))
    assert uav.sar_heading_error_deg == pytest.approx(0.0)
    assert uav.sar_imaging


def test_executor_turning_off_sar_clears_acquisition_geometry(uav):
    uav.sar_look_direction = "left"
    uav.sar_scan_origin = (9.0, 10.0)
    command = ControlCommand(0.0, 0.25, SensorMode.OFF, OperationMode.TRANSIT)

    UAVDynamicsExecutor().execute(uav, command, dt_min=1.0)

    assert not uav.sar_imaging
    assert uav.sar_look_direction is None
    assert uav.sar_scan_heading_rad is None
    assert uav.sar_scan_origin is None


def test_executor_reports_real_heading_error_and_suppresses_imaging(uav):
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.SAR,
        OperationMode.COVERAGE,
        sar_look_direction="right",
        sar_scan_heading_rad=math.pi / 2.0,
        sar_scan_origin=uav.float_position,
    )

    UAVDynamicsExecutor().execute(uav, command, dt_min=1.0)

    assert uav.sar_heading_error_deg == pytest.approx(90.0)
    assert not uav.sar_imaging


def test_executor_rejects_parallel_sar_offset_and_off_clears_prior_footprint(uav):
    uav.sar_footprint = [GridCoord(10, 10)]
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.SAR,
        OperationMode.COVERAGE,
        sar_look_direction="right",
        sar_scan_heading_rad=0.0,
        sar_scan_origin=(10.0, 10.5),
    )

    UAVDynamicsExecutor().execute(uav, command, dt_min=1.0)

    assert uav.sar_cross_track_error_cells > 0.2
    assert not uav.sar_imaging

    UAVDynamicsExecutor().execute(
        uav,
        ControlCommand(0.0, 0.25, SensorMode.OFF, OperationMode.TRANSIT),
        dt_min=1.0,
    )

    assert uav.sar_footprint == []


def test_uav_clear_sar_acquisition_clears_view_and_all_geometry(uav):
    uav.sar_look_direction = "left"
    uav.sar_scan_heading_rad = math.pi
    uav.sar_scan_origin = (9.0, 10.0)
    uav.sar_heading_error_deg = 12.0
    uav.sar_cross_track_error_cells = 0.5
    uav.sar_aperture_track = [(9.0, 10.0), (10.0, 10.0)]
    uav.sar_footprint = [GridCoord(9, 10)]
    uav.sar_imaging = True

    uav._clear_sar_acquisition()

    assert uav.sar_look_direction is None
    assert uav.sar_scan_heading_rad is None
    assert uav.sar_scan_origin is None
    assert uav.sar_heading_error_deg == 0.0
    assert uav.sar_cross_track_error_cells == 0.0
    assert uav.sar_aperture_track == []
    assert uav.sar_footprint == []
    assert not uav.sar_imaging


def test_executor_maps_probe_operation_to_tracking_status(uav):
    command = ControlCommand(
        turn_rate_rad_min=0.0,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.PROBE,
        target_contact_id="C1",
    )

    result = UAVDynamicsExecutor().execute(uav, command, dt_min=1.0)

    assert result.command is command
    assert uav.status == "tracking"


def test_executor_preserves_distinct_requested_and_safety_applied_commands(uav):
    requested = ControlCommand(
        turn_rate_rad_min=1.0,
        speed_cells_min=1.0,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.TRANSIT,
        sar_look_direction="right",
        sar_scan_heading_rad=0.0,
        sar_scan_origin=uav.float_position,
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

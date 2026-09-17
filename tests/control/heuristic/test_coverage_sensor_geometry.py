import math
from dataclasses import replace

import numpy as np

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControlTask,
    CoverageExecutionConfig,
    ObservationSpec,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.control.heuristic.coverage import CoverageController
from src.env.sar_sensor import SARSensor
from src.schedule.datatypes import BBox, GridCoord
from src.utils.coverage_planner import CoveragePlanner
import pytest


def test_alternating_scan_direction_covers_same_side_of_world():
    sensor = SARSensor(swath_width_cells=1.5, near_range_cells=.25)
    east = set(sensor.compute_swath_footprint((10.5, 9.75), 0.0, 'right', .8))
    west = set(sensor.compute_swath_footprint((10.5, 9.75), math.pi, 'left', .8))
    assert east == west
    assert GridCoord(10, 10) in east
    wrong = set(sensor.compute_swath_footprint((10.5, 9.75), math.pi, 'right', .8))
    assert GridCoord(10, 10) not in wrong


def _observation(position=(2.0, 10.0), heading=0.0, version=1):
    empty = np.zeros((30, 30), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="UAV-1",
            position=position,
            heading_rad=heading,
            speed_cells_min=1.0,
            remaining_range_cells=100.0,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.HEURISTIC,
            operation_mode=OperationMode.TRANSIT,
            sensor_mode=SensorMode.OFF,
            safety_intervened=False,
        ),
        local_info=empty,
        local_value=empty,
        obstacle_mask=empty,
        searchable_mask=np.ones((30, 30), dtype=bool),
        planning_obstacle_mask=empty,
        planning_map_version=version,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.SAR),
            (OperationMode.TRANSIT, OperationMode.COVERAGE),
            (),
        ),
    )


def test_controller_emits_scan_direction_heading_and_origin_for_each_swath():
    controller = CoverageController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=ActionSpec(-2.0, 2.0, .5, 1.0),
        planner=CoveragePlanner(sample_step=.5),
        swath_width=1.5,
        r_min=1.0,
        sar_along_track_cells=.8,
        sar_heading_tolerance_rad=math.radians(2.0),
        cross_track_tolerance_cells=.2,
    )
    observation = _observation()
    controller.start_task(
        ControlTask("S1", OperationMode.COVERAGE, region_bbox=BBox(10, 10, 15, 15)),
        observation,
    )

    scan_start, scan_end = controller.scan_ranges[0]
    stable_index = scan_start + 5
    assert stable_index < scan_end
    for pose in controller.route[1 : stable_index + 1]:
        scan_observation = replace(
            observation,
            self_state=replace(
                observation.self_state,
                position=pose[:2],
                heading_rad=pose[2],
            ),
        )
        controller.act(scan_observation)
    scan_observation = replace(
        observation,
        self_state=replace(
            observation.self_state,
            position=controller.route[stable_index][:2],
            heading_rad=controller.route[stable_index][2],
        ),
    )

    command = controller.act(scan_observation).command

    swath = controller.scan_swaths[0]
    assert command.sensor_mode is SensorMode.SAR
    assert command.sar_look_direction == swath.look_direction
    assert command.sar_scan_heading_rad == swath.heading
    assert command.sar_scan_origin == swath.start


def test_controller_rejects_planner_with_inconsistent_near_range():
    execution = CoverageExecutionConfig(
        swath_width_cells=1.5,
        near_range_cells=0.25,
        min_turn_radius_cells=1.0,
        along_track_cells=.8,
        heading_tolerance_rad=math.radians(2.0),
        cross_track_tolerance_cells=.2,
    )

    with pytest.raises(ValueError, match="near_range"):
        CoverageController(
            observation_spec=ObservationSpec("control-observation/v1", 11),
            action_spec=ActionSpec(-2.0, 2.0, .5, 1.0),
            planner=CoveragePlanner(sample_step=.5, near_range=.5),
            coverage_execution=execution,
        )

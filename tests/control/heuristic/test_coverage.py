import math
from dataclasses import replace

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControlTask,
    ObservationSpec,
    OperationMode,
    SensorMode,
    StopReason,
    UAVObservation,
)
from src.control.common.safety import InvalidControlCommand
from src.control.heuristic.coverage import (
    CoverageController,
    CoveragePhase,
    CoverageRouteBlockedError,
)
from src.control.scan_pattern import generate_scan_waypoints
from src.control.waypoint import navigate_to_region
from src.schedule.datatypes import BBox, GridCoord
from src.utils.coverage_planner import CoveragePlanner


class NavigatorSpy:
    def __init__(self):
        self.plan_calls = 0
        self.plan_arguments = []

    def plan_grid(
        self, start, goals, obstacle_mask, r_min, planning_map_version=0
    ):
        self.plan_calls += 1
        self.plan_arguments.append((start, goals, obstacle_mask.copy(), r_min, planning_map_version))
        goal = min(goals)
        heading = math.atan2(goal[1] - start[1], goal[0] - start[0])
        return [tuple(start), (goal[0], goal[1], heading)]


class DetourNavigator(NavigatorSpy):
    def plan_grid(
        self, start, goals, obstacle_mask, r_min, planning_map_version=0
    ):
        direct = super().plan_grid(
            start, goals, obstacle_mask, r_min, planning_map_version
        )
        if self.plan_calls == 1:
            return direct
        goal = min(goals)
        detour_col = max(start[0], goal[0]) + 1.0
        return [
            tuple(start),
            (detour_col, start[1], 0.0),
            (detour_col, goal[1], math.pi / 2.0),
            (goal[0], goal[1], math.pi),
        ]


@pytest.fixture
def action_spec():
    return ActionSpec(-2.0, 2.0, 0.5, 1.0)


@pytest.fixture
def observation():
    arrays = np.zeros((30, 30), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="uav-1",
            position=(2.0, 10.0),
            heading_rad=0.0,
            speed_cells_min=1.0,
            remaining_range_cells=100.0,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.HEURISTIC,
            operation_mode=OperationMode.TRANSIT,
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
            (OperationMode.TRANSIT, OperationMode.COVERAGE),
            (),
        ),
    )


@pytest.fixture
def controller(action_spec):
    return CoverageController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=NavigatorSpy(),
        planner=CoveragePlanner(sample_step=0.5),
        swath_width=2.0,
        r_min=1.0,
        sar_heading_tolerance_rad=math.radians(2.0),
    )


@pytest.fixture
def started_controller(controller, observation):
    controller.start_task(
        ControlTask("S1", OperationMode.COVERAGE, region_bbox=BBox(10, 10, 15, 15)),
        observation,
    )
    return controller


def _with_pose(observation, pose, *, planning_map_version=None, obstacle_mask=None):
    return replace(
        observation,
        planning_map_version=(
            observation.planning_map_version
            if planning_map_version is None
            else planning_map_version
        ),
        planning_obstacle_mask=(
            observation.planning_obstacle_mask
            if obstacle_mask is None
            else obstacle_mask
        ),
        self_state=replace(
            observation.self_state,
            position=pose[:2],
            heading_rad=pose[2],
        ),
    )


def test_coverage_uses_astar_before_enabling_sar(controller, observation):
    controller.start_task(
        ControlTask("S1", OperationMode.COVERAGE, region_bbox=BBox(10, 10, 15, 15)),
        observation,
    )

    decision = controller.act(observation)

    assert controller.navigator.plan_calls == 1
    assert controller.phase is CoveragePhase.TRANSIT_ASTAR
    assert decision.command.sensor_mode is SensorMode.OFF
    assert decision.command.operation_mode is OperationMode.TRANSIT


def test_coverage_enables_sar_only_on_stable_scan_leg(started_controller, observation):
    scan_start, scan_end = started_controller.scan_ranges[0]
    interior_index = scan_start + 1
    assert interior_index < scan_end
    scan_observation = _with_pose(
        observation, started_controller.route[interior_index]
    )

    decision = started_controller.act(scan_observation)

    assert started_controller.phase is CoveragePhase.SCANNING
    assert decision.command.sensor_mode is SensorMode.SAR
    assert decision.command.operation_mode is OperationMode.COVERAGE


def test_coverage_keeps_sar_off_until_heading_is_stable(started_controller, observation):
    scan_start, _ = started_controller.scan_ranges[0]
    interior_pose = started_controller.route[scan_start + 1]
    unstable_observation = _with_pose(
        observation,
        (interior_pose[0], interior_pose[1], interior_pose[2] + math.radians(3.0)),
    )

    decision = started_controller.act(unstable_observation)

    assert started_controller.phase is CoveragePhase.ALIGN_SCAN
    assert decision.command.sensor_mode is SensorMode.OFF


def test_coverage_emits_search_complete_once_after_final_scan_pose(
    started_controller, observation
):
    decisions = []
    for pose in started_controller.route[1:-1]:
        decisions.append(started_controller.act(_with_pose(observation, pose)))
    final_observation = _with_pose(observation, started_controller.route[-1])

    decision = started_controller.act(final_observation)
    repeated = started_controller.act(final_observation)
    decisions.append(decision)

    assert started_controller.is_complete(final_observation)
    assert started_controller.phase is CoveragePhase.COMPLETED
    completion_events = [
        event for result in decisions for event in result.events
    ]
    assert [(event.event_type, dict(event.payload)) for event in completion_events] == [
        ("search_complete", {"task_id": "S1"})
    ]
    assert repeated.events == ()


def test_coverage_updates_the_map_version_when_unflown_route_is_still_safe(
    started_controller, observation
):
    unchanged_route = _with_pose(observation, started_controller.route[0])
    started_controller.act(unchanged_route)
    updated_route = _with_pose(
        observation,
        started_controller.route[0],
        planning_map_version=2,
    )

    started_controller.act(updated_route)

    assert started_controller.navigator.plan_calls == 1
    assert started_controller.planning_map_version == 2


def test_coverage_rejects_a_continuous_route_segment_that_crosses_a_blocked_cell(
    controller, observation
):
    blocked_mask = np.array(observation.planning_obstacle_mask, copy=True)
    # The direct A* entry segment is (2, 10) -> (10, 9.75).  This cell is
    # crossed by the segment, but neither endpoint is inside it.
    blocked_mask[4, 9] = True
    blocked_observation = replace(observation, planning_obstacle_mask=blocked_mask)

    with pytest.raises(CoverageRouteBlockedError, match="coverage route blocked"):
        controller.start_task(
            ControlTask(
                "S1", OperationMode.COVERAGE, region_bbox=BBox(10, 10, 15, 15)
            ),
            blocked_observation,
        )


def test_coverage_rejects_a_new_obstacle_on_an_unflown_scan_leg(
    started_controller, observation
):
    scan_start, scan_end = started_controller.scan_ranges[0]
    current_observation = _with_pose(
        observation, started_controller.route[scan_start]
    )
    started_controller.act(current_observation)
    current_index = started_controller.follower.index
    assert scan_start <= current_index < scan_end - 1
    blocked_mask = np.array(observation.planning_obstacle_mask, copy=True)
    blocked_pose = started_controller.route[current_index + 1]
    blocked_mask[math.floor(blocked_pose[0]), math.floor(blocked_pose[1])] = True
    blocked_observation = _with_pose(
        observation,
        started_controller.route[current_index],
        planning_map_version=2,
        obstacle_mask=blocked_mask,
    )

    with pytest.raises(CoverageRouteBlockedError, match="coverage route blocked"):
        started_controller.act(blocked_observation)


def test_coverage_replan_starts_at_next_unconsumed_scan_band(
    controller, observation
):
    controller.navigator = DetourNavigator()
    controller.r_min = 1.5
    controller.start_task(
        ControlTask("S1", OperationMode.COVERAGE, region_bbox=BBox(10, 10, 15, 15)),
        observation,
    )
    first_scan_end = controller.scan_ranges[0][1]
    for pose in controller.route[1 : first_scan_end + 1]:
        controller.act(_with_pose(observation, pose))
    previous_index = controller.follower.index
    next_scan_start = next(
        start for start, end in controller.scan_ranges if end > previous_index
    )
    next_scan_entry = controller.route[next_scan_start]
    blocked_mask = np.array(observation.planning_obstacle_mask, copy=True)
    # The prior connector swings through this cell, while the detour to the
    # second band remains free.  It must not restart at the first scan entry.
    blocked_mask[17, 10] = True
    replan_observation = _with_pose(
        observation,
        controller.route[previous_index],
        planning_map_version=2,
        obstacle_mask=blocked_mask,
    )

    controller.act(replan_observation)

    assert controller.navigator.plan_arguments[-1][1] == {next_scan_entry[:2]}
    assert controller.route[3][:2] == next_scan_entry[:2]
    assert controller.route[3][:2] != observation.self_state.position


def test_coverage_stop_before_final_pose_is_not_complete_and_is_idempotent(
    started_controller, observation
):
    started_controller.stop_task(StopReason.CANCELLED)
    started_controller.stop_task(StopReason.CANCELLED)

    assert started_controller.phase is CoveragePhase.TRANSIT_ASTAR
    assert not started_controller.is_complete(observation)
    assert not started_controller.follower.is_complete


def test_coverage_rejects_disallowed_operation_mode(started_controller, observation):
    scan_start, _ = started_controller.scan_ranges[0]
    restricted_observation = replace(
        _with_pose(observation, started_controller.route[scan_start + 1]),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.SAR),
            (OperationMode.TRANSIT,),
            (),
        ),
    )

    with pytest.raises(InvalidControlCommand, match="operation mode is absent"):
        started_controller.act(restricted_observation)


def test_legacy_navigation_wrapper_warns_and_returns_grid_coordinates():
    with pytest.warns(DeprecationWarning):
        waypoints = navigate_to_region(GridCoord(1, 1), BBox(10, 10, 15, 15))

    assert waypoints
    assert all(isinstance(point, GridCoord) for point in waypoints)


def test_legacy_scan_wrapper_warns_and_returns_coarse_grid_endpoints():
    with pytest.warns(DeprecationWarning):
        waypoints = generate_scan_waypoints(BBox(2, 2, 8, 8), swath_cells=2)

    assert waypoints
    assert all(isinstance(point, GridCoord) for point in waypoints)

from __future__ import annotations

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
    UAVObservation,
)
from src.control.heuristic.coverage import CoverageController, CoveragePhase
from src.schedule.datatypes import BBox
from src.utils.coverage_planner import CoveragePlanner


class NavigatorSpy:
    def __init__(self) -> None:
        self.plan_calls = 0

    def plan_grid(
        self, start, goals, obstacle_mask, r_min, planning_map_version=0
    ):
        del obstacle_mask, r_min, planning_map_version
        self.plan_calls += 1
        goal = min(goals)
        heading = math.atan2(goal[1] - start[1], goal[0] - start[0])
        return [tuple(start), (goal[0], goal[1], heading)]


@pytest.fixture
def observation() -> ControlObservation:
    obstacle_mask = np.zeros((40, 40), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="UAV-1",
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
        local_info=obstacle_mask,
        local_value=obstacle_mask,
        obstacle_mask=obstacle_mask,
        searchable_mask=np.ones((40, 40), dtype=bool),
        planning_obstacle_mask=obstacle_mask,
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
def controller() -> CoverageController:
    return CoverageController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0),
        navigator=NavigatorSpy(),
        planner=CoveragePlanner(sample_step=0.5),
        swath_width=2.0,
        r_min=1.0,
        progress_timeout_min=10.0,
        align_timeout_min=8.0,
        max_stall_replans=2,
    )


def _start(controller: CoverageController, observation: ControlObservation) -> None:
    controller.start_task(
        ControlTask("S1", OperationMode.COVERAGE, region_bbox=BBox(10, 10, 15, 15)),
        observation,
    )


def _at(
    observation: ControlObservation,
    timestamp: float,
    *,
    pose: tuple[float, float, float] | None = None,
    planning_map_version: int | None = None,
    dt_min: float | None = None,
) -> ControlObservation:
    return replace(
        observation,
        timestamp_min=timestamp,
        dt_min=observation.dt_min if dt_min is None else dt_min,
        planning_map_version=(
            observation.planning_map_version
            if planning_map_version is None
            else planning_map_version
        ),
        self_state=replace(
            observation.self_state,
            position=(
                observation.self_state.position
                if pose is None
                else pose[:2]
            ),
            heading_rad=(
                observation.self_state.heading_rad
                if pose is None
                else pose[2]
            ),
        ),
    )


def test_watchdog_replans_twice_then_emits_one_terminal_failure(
    controller, observation
):
    _start(controller, observation)

    results = [
        controller.act(_at(observation, timestamp))
        for timestamp in (0, 10, 20, 30)
    ]

    assert [event.event_type for result in results for event in result.events] == [
        "coverage_stalled",
        "coverage_stalled",
        "task_failed",
    ]
    assert [
        event.payload["attempt"]
        for result in results
        for event in result.events
    ] == [1, 2, 3]
    assert controller.route_snapshot().status == "unavailable"
    assert controller.route_snapshot().route
    assert controller.route_snapshot().coverage_progress["stall_replans"] == 2


def test_watchdog_does_not_reset_on_safe_map_revision(controller, observation):
    _start(controller, observation)
    controller.act(_at(observation, 0))
    controller.act(_at(observation, 5, planning_map_version=2))
    result = controller.act(_at(observation, 10, planning_map_version=3))

    assert [event.event_type for event in result.events] == ["coverage_stalled"]
    assert controller.route_snapshot().status == "ready"


def test_safe_connector_is_not_counted_as_continuous_alignment(controller, observation):
    _start(controller, observation)
    results = []
    timestamp = 0.0
    for index in range(0, controller.scan_ranges[1][0] + 1):
        dt_min = 5.0 if index == 1 else 1.0
        results.append(
            controller.act(
                _at(
                    observation,
                    timestamp,
                    pose=controller.route[index],
                    dt_min=dt_min,
                )
            )
        )
        timestamp += dt_min

    assert all(not result.events for result in results)
    assert controller.follower.progress_cells > 0.01


def test_alignment_watchdog_starts_only_on_unstable_scan(controller, observation):
    _start(controller, observation)
    scan_start = controller.scan_ranges[0][0]
    scan_pose = controller.route[scan_start + 1]
    wrong_heading = scan_pose[2] + math.radians(10.0)
    for index in range(scan_start + 1):
        controller.act(
            _at(
                observation,
                float(index * 5),
                pose=controller.route[index],
                dt_min=5.0,
            )
        )
    controller.act(
        _at(
            observation,
            float((scan_start + 1) * 5),
            pose=(scan_pose[0], scan_pose[1], wrong_heading),
            dt_min=5.0,
        )
    )
    assert controller.phase is CoveragePhase.ALIGN_SCAN
    result = controller.act(
        _at(
            observation,
            float((scan_start + 1) * 5 + 8),
            pose=(scan_pose[0], scan_pose[1], wrong_heading),
            dt_min=1.0,
        )
    )

    assert [event.event_type for event in result.events] == ["coverage_stalled"]
    assert result.events[0].payload["reason"] == "align_stalled"


def test_identical_timestamp_cannot_emit_duplicate_stall_events(controller, observation):
    _start(controller, observation)
    controller.act(_at(observation, 0))
    first = controller.act(_at(observation, 10))
    repeated = controller.act(_at(observation, 10))

    assert [event.event_type for event in first.events] == ["coverage_stalled"]
    assert repeated.events == ()


def test_coverage_progress_snapshot_uses_null_for_unobserved_diagnostics(
    controller, observation
):
    _start(controller, observation)
    before_act = controller.route_snapshot().coverage_progress
    assert before_act["progress_cells"] is None
    assert before_act["heading_error_deg"] is None
    assert before_act["cross_track_error_cells"] is None

    controller.act(_at(observation, 0))
    after_act = controller.route_snapshot().coverage_progress
    assert after_act["progress_cells"] == pytest.approx(0.0)
    assert after_act["remaining_route_cells"] is not None
    assert after_act["phase"] == CoveragePhase.TRANSIT_ASTAR.value

"""Heuristic controller for fixed-wing SAR coverage tasks."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
import math

from src.control.common.contracts import (
    ActionSpec,
    ControlDecision,
    ControlObservation,
    ControlTask,
    ControllerEventRequest,
    ObservationSpec,
    OperationMode,
    SensorMode,
    StopReason,
)
from src.control.heuristic.base import HeuristicControllerBase, RouteFollower, _wrap_pi
from src.control.heuristic.navigation import AStarNavigator
from src.utils.coverage_planner import CoveragePath, CoveragePlanner


class CoveragePhase(str, Enum):
    CREATED = "created"
    TRANSIT_ASTAR = "transit_astar"
    ALIGN_SCAN = "align_scan"
    SCANNING = "scanning"
    COMPLETED = "completed"


def scan_endpoint_poses(coverage: CoveragePath) -> tuple[tuple[float, float, float], ...]:
    """Return the ordered straight-scan endpoints from a planner result."""
    return tuple(
        pose
        for start, end in coverage.scan_ranges
        for pose in (coverage.waypoints[start], coverage.waypoints[end])
    )


class CoverageController(HeuristicControllerBase):
    """Plan and follow a coverage route entirely from immutable observations."""

    def __init__(
        self,
        *,
        observation_spec: ObservationSpec,
        action_spec: ActionSpec,
        navigator: AStarNavigator | None = None,
        planner: CoveragePlanner | None = None,
        swath_width: float = 2.0,
        r_min: float = 1.0,
        sar_heading_tolerance_rad: float = math.radians(2.0),
    ) -> None:
        if swath_width <= 0.0 or r_min <= 0.0:
            raise ValueError("swath_width and r_min must be positive")
        if not math.isfinite(sar_heading_tolerance_rad) or sar_heading_tolerance_rad < 0.0:
            raise ValueError("sar_heading_tolerance_rad must be finite and non-negative")
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self.navigator = navigator or AStarNavigator()
        self.planner = planner or CoveragePlanner()
        self.swath_width = float(swath_width)
        self.r_min = float(r_min)
        self.sar_heading_tolerance_rad = float(sar_heading_tolerance_rad)
        self.phase = CoveragePhase.CREATED
        self.task: ControlTask | None = None
        self.follower: RouteFollower | None = None
        self.route: tuple[tuple[float, float, float], ...] = ()
        self.scan_ranges: tuple[tuple[int, int], ...] = ()
        self.planning_map_version: int | None = None
        self._completion_event_emitted = False

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    @property
    def operation_mode(self) -> OperationMode:
        if self.phase is CoveragePhase.TRANSIT_ASTAR:
            return OperationMode.TRANSIT
        return OperationMode.COVERAGE

    def start_task(self, task: ControlTask, observation: ControlObservation) -> None:
        if task.task_type is not OperationMode.COVERAGE or task.region_bbox is None:
            raise ValueError("coverage tasks require a coverage mode and region_bbox")
        self.task = task
        self.phase = CoveragePhase.CREATED
        self._completion_event_emitted = False
        self._plan_route(observation)

    def is_complete(self, observation: ControlObservation) -> bool:
        return self.phase is CoveragePhase.COMPLETED

    def stop_task(self, reason: StopReason) -> None:
        del reason
        self.phase = CoveragePhase.COMPLETED

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self.task is None or self.follower is None:
            raise RuntimeError("start_task must be called before act")
        self._refresh_invalidated_route(observation)
        command = self.follower.next_command(
            observation,
            self.action_spec,
            SensorMode.OFF,
            self.operation_mode,
        )
        self._update_phase(observation)
        sensor_mode = (
            SensorMode.SAR
            if self.phase is CoveragePhase.SCANNING
            and SensorMode.SAR in observation.action_mask.allowed_sensor_modes
            else SensorMode.OFF
        )
        operation_mode = self.operation_mode
        command = replace(
            command, sensor_mode=sensor_mode, operation_mode=operation_mode
        )
        if self.phase is CoveragePhase.COMPLETED and not self._completion_event_emitted:
            self._completion_event_emitted = True
            return ControlDecision(
                command,
                (ControllerEventRequest("search_complete", {"task_id": self.task.task_id}),),
            )
        return ControlDecision(command)

    def _plan_route(self, observation: ControlObservation) -> None:
        assert self.task is not None
        start_pose = (*observation.self_state.position, observation.self_state.heading_rad)
        initial_coverage = self.planner.plan(
            self.task.region_bbox, start_pose, self.swath_width, self.r_min
        )
        endpoints = scan_endpoint_poses(initial_coverage)
        if not endpoints:
            raise ValueError("coverage planner produced no scan endpoints")
        entry = endpoints[0]
        transit = self.navigator.plan_grid(
            start_pose,
            {entry[:2]},
            observation.planning_obstacle_mask,
            self.r_min,
            observation.planning_map_version,
        )
        coverage = self.planner.plan(
            self.task.region_bbox, entry, self.swath_width, self.r_min
        )
        offset = len(transit) - 1
        self.route = tuple(transit) + tuple(coverage.waypoints[1:])
        self.scan_ranges = tuple(
            (offset + start, offset + end) for start, end in coverage.scan_ranges
        )
        self.follower = RouteFollower(self.route)
        self.planning_map_version = observation.planning_map_version
        self.phase = CoveragePhase.TRANSIT_ASTAR

    def _refresh_invalidated_route(self, observation: ControlObservation) -> None:
        if observation.planning_map_version == self.planning_map_version:
            return
        assert self.follower is not None
        unflown = self.route[self.follower.index + 1 :]
        if any(self._pose_blocked(pose, observation.planning_obstacle_mask) for pose in unflown):
            self._plan_route(observation)
        else:
            self.planning_map_version = observation.planning_map_version

    def _update_phase(self, observation: ControlObservation) -> None:
        assert self.follower is not None
        if self.follower.is_complete:
            self.phase = CoveragePhase.COMPLETED
            return
        index = self.follower.index
        if not self.scan_ranges or index < self.scan_ranges[0][0]:
            self.phase = CoveragePhase.TRANSIT_ASTAR
            return
        if any(start < index < end for start, end in self.scan_ranges):
            target = self.route[index + 1]
            desired_heading = math.atan2(
                target[1] - observation.self_state.position[1],
                target[0] - observation.self_state.position[0],
            )
            heading_error = abs(
                _wrap_pi(desired_heading - observation.self_state.heading_rad)
            )
            self.phase = (
                CoveragePhase.SCANNING
                if heading_error <= self.sar_heading_tolerance_rad
                else CoveragePhase.ALIGN_SCAN
            )
            return
        self.phase = CoveragePhase.ALIGN_SCAN

    @staticmethod
    def _pose_blocked(pose: tuple[float, float, float], obstacle_mask: object) -> bool:
        cols, rows = obstacle_mask.shape
        col, row = math.floor(pose[0]), math.floor(pose[1])
        return not (0 <= col < cols and 0 <= row < rows) or bool(obstacle_mask[col, row])


__all__ = ["CoverageController", "CoveragePhase", "scan_endpoint_poses"]

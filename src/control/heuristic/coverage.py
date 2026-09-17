"""Heuristic controller for fixed-wing SAR coverage tasks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from enum import Enum
import math

from src.control.common.contracts import (
    ActionSpec,
    ControlCommand,
    ControlDecision,
    CoverageExecutionConfig,
    ControlObservation,
    ControlRouteSnapshot,
    ControlTask,
    ControllerEventRequest,
    ObservationSpec,
    OperationMode,
    SensorMode,
    StopReason,
)
from src.control.common.safety import InvalidControlCommand, SafetyEnvelope
from src.control.heuristic.base import (
    HeuristicControllerBase,
    _wrap_pi,
    next_route_index,
)
from src.control.heuristic.coverage_guidance import CoverageGuidance, CoverageRouteFollower
from src.control.heuristic.navigation import AStarNavigator
from src.utils.coverage_planner import CoveragePath, CoveragePlanner, ScanSwath


class CoveragePhase(str, Enum):
    CREATED = "created"
    TRANSIT_ASTAR = "transit_astar"
    ALIGN_SCAN = "align_scan"
    SCANNING = "scanning"
    COMPLETED = "completed"


class CoverageRouteBlockedError(RuntimeError):
    """Raised when a required coverage route intersects the planning mask."""

    def __init__(
        self, segment_index: int, cell: tuple[int, int], planning_map_version: int
    ) -> None:
        self.segment_index = segment_index
        self.cell = cell
        self.planning_map_version = planning_map_version
        super().__init__(
            "coverage route blocked: "
            f"segment={segment_index}, cell={cell}, "
            f"planning_map_version={planning_map_version}"
        )


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
        coverage_execution: CoverageExecutionConfig | None = None,
        sar_along_track_cells: float | None = None,
        sar_heading_tolerance_rad: float = math.radians(2.0),
        cross_track_tolerance_cells: float = 0.2,
    ) -> None:
        if coverage_execution is not None:
            swath_width = coverage_execution.swath_width_cells
            near_range = coverage_execution.near_range_cells
            r_min = coverage_execution.min_turn_radius_cells
            sar_along_track_cells = coverage_execution.along_track_cells
            sar_heading_tolerance_rad = coverage_execution.heading_tolerance_rad
            cross_track_tolerance_cells = coverage_execution.cross_track_tolerance_cells
        elif sar_along_track_cells is None:
            near_range = 0.25
            sar_along_track_cells = 0.8
        else:
            near_range = 0.25
        if swath_width <= 0.0 or r_min <= 0.0:
            raise ValueError("swath_width and r_min must be positive")
        if (
            not math.isfinite(sar_heading_tolerance_rad)
            or sar_heading_tolerance_rad < 0.0
            or not math.isfinite(sar_along_track_cells)
            or sar_along_track_cells <= 0.0
            or not math.isfinite(cross_track_tolerance_cells)
            or cross_track_tolerance_cells < 0.0
        ):
            raise ValueError("sar_heading_tolerance_rad must be finite and non-negative")
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self.navigator = navigator or AStarNavigator()
        if planner is not None and not math.isclose(
            float(getattr(planner, "near_range", math.nan)),
            float(near_range),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "custom CoveragePlanner near_range must match near_range_cells"
            )
        self.planner = planner or CoveragePlanner(near_range=near_range)
        self.swath_width = float(swath_width)
        self.r_min = float(r_min)
        self.sar_along_track_cells = float(sar_along_track_cells)
        self.sar_heading_tolerance_rad = float(sar_heading_tolerance_rad)
        self.cross_track_tolerance_cells = float(cross_track_tolerance_cells)
        self.coverage_execution = coverage_execution or CoverageExecutionConfig(
            swath_width_cells=self.swath_width,
            near_range_cells=0.25,
            min_turn_radius_cells=self.r_min,
            along_track_cells=self.sar_along_track_cells,
            heading_tolerance_rad=self.sar_heading_tolerance_rad,
            cross_track_tolerance_cells=self.cross_track_tolerance_cells,
        )
        self.phase = CoveragePhase.CREATED
        self.task: ControlTask | None = None
        self.follower: CoverageRouteFollower | None = None
        self.route: tuple[tuple[float, float, float], ...] = ()
        self.scan_ranges: tuple[tuple[int, int], ...] = ()
        self.scan_swaths: tuple[ScanSwath, ...] = ()
        self.planning_map_version: int | None = None
        self._route_revision = 0
        self._route_status = "pending"
        self._stopped = False
        self._completion_event_emitted = False
        self._direction: str | None = None

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
        self.follower = None
        self.route = ()
        self.scan_ranges = ()
        self.scan_swaths = ()
        self.planning_map_version = None
        self._route_revision = 0
        self._route_status = "pending"
        self._stopped = False
        self._completion_event_emitted = False
        self._direction = None
        self._plan_route(observation)

    def is_complete(self, observation: ControlObservation) -> bool:
        del observation
        return self.follower is not None and self.follower.is_complete

    def stop_task(self, reason: StopReason) -> None:
        del reason
        self._stopped = True
        self._route_status = "cleared"
        # This controller owns no external resources.  Completion remains tied
        # to RouteFollower consuming the final pose, so stopping is idempotent.

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self.task is None or self.follower is None:
            raise RuntimeError("start_task must be called before act")
        self._refresh_conflict_route(observation)
        self._refresh_invalidated_route(observation)
        guidance = self.follower.update(
            position=observation.self_state.position,
            heading_rad=observation.self_state.heading_rad,
            speed_cells_min=observation.self_state.speed_cells_min,
            dt_min=observation.dt_min,
            action_spec=self.action_spec,
        )
        self._update_phase(observation, guidance)
        sensor_mode = (
            SensorMode.SAR
            if self.phase is CoveragePhase.SCANNING
            and SensorMode.SAR in observation.action_mask.allowed_sensor_modes
            else SensorMode.OFF
        )
        operation_mode = self.operation_mode
        self._validate_command_modes(sensor_mode, operation_mode, observation)
        command = ControlCommand(
            turn_rate_rad_min=guidance.turn_rate_rad_min,
            speed_cells_min=guidance.speed_cells_min,
            sensor_mode=sensor_mode,
            operation_mode=operation_mode,
        )
        if sensor_mode is SensorMode.SAR:
            scan_index = guidance.scan_segment_index
            if scan_index is None:
                raise RuntimeError("SAR enabled without an active scan swath")
            swath = self.scan_swaths[scan_index]
            command = replace(
                command,
                sar_look_direction=swath.look_direction,
                sar_scan_heading_rad=swath.heading,
                sar_scan_origin=swath.start,
            )
        if self.phase is CoveragePhase.COMPLETED and not self._completion_event_emitted:
            self._completion_event_emitted = True
            return ControlDecision(
                command,
                (ControllerEventRequest("search_complete", {"task_id": self.task.task_id}),),
            )
        return ControlDecision(command)

    def _plan_route(
        self, observation: ControlObservation, direction: str | None = None
    ) -> None:
        assert self.task is not None
        start_pose = (*observation.self_state.position, observation.self_state.heading_rad)
        initial_coverage = self.planner.plan(
            self.task.region_bbox,
            start_pose,
            self.swath_width,
            self.r_min,
            direction,
            along_track_cells=self.sar_along_track_cells,
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
            self.task.region_bbox,
            entry,
            self.swath_width,
            self.r_min,
            direction,
            along_track_cells=self.sar_along_track_cells,
        )
        offset = len(transit) - 1
        route = tuple(transit) + tuple(coverage.waypoints[1:])
        scan_ranges = tuple(
            (offset + start, offset + end) for start, end in coverage.scan_ranges
        )
        self._set_route(
            route,
            scan_ranges,
            coverage.swaths,
            observation.planning_obstacle_mask,
            observation.planning_map_version,
        )
        self.phase = CoveragePhase.TRANSIT_ASTAR

    def _refresh_conflict_route(self, observation: ControlObservation) -> None:
        """Give a controlled coverage task a new scan orientation after a conflict."""
        if not any(
            event.event_type == "route_blocked"
            and event.payload.get("reason") == "path_conflict"
            for event in observation.events
        ):
            return
        assert self.task is not None
        if self.task.region_bbox is None:
            return
        width = self.task.region_bbox.col_end - self.task.region_bbox.col_start
        height = self.task.region_bbox.row_end - self.task.region_bbox.row_start
        current = self._direction or ("horizontal" if width >= height else "vertical")
        self._direction = "vertical" if current == "horizontal" else "horizontal"
        self.follower = None
        self.route = ()
        self.scan_ranges = ()
        self.scan_swaths = ()
        self.planning_map_version = None
        self._route_status = "pending"
        self._plan_route(observation, direction=self._direction)

    def _refresh_invalidated_route(self, observation: ControlObservation) -> None:
        if observation.planning_map_version == self.planning_map_version:
            return
        assert self.follower is not None
        self._route_status = "pending"
        unflown = self.route[self.follower.index + 1 :]
        current_pose = (
            *observation.self_state.position,
            observation.self_state.heading_rad,
        )
        if self._route_blocked(
            (current_pose, *unflown), observation.planning_obstacle_mask
        ) is not None:
            self._replan_unflown_suffix(observation)
        else:
            self.planning_map_version = observation.planning_map_version
            self._route_status = "ready"

    def _replan_unflown_suffix(self, observation: ControlObservation) -> None:
        assert self.follower is not None
        suffix_start = self._next_unconsumed_scan_start()
        suffix = self.route[suffix_start:]
        if not suffix:
            raise CoverageRouteBlockedError(
                self.follower.index,
                (
                    math.floor(observation.self_state.position[0]),
                    math.floor(observation.self_state.position[1]),
                ),
                observation.planning_map_version,
            )
        start_pose = (
            *observation.self_state.position,
            observation.self_state.heading_rad,
        )
        transit = self.navigator.plan_grid(
            start_pose,
            {suffix[0][:2]},
            observation.planning_obstacle_mask,
            self.r_min,
            observation.planning_map_version,
        )
        offset = len(transit) - 1
        scan_ranges = tuple(
            (
                offset + max(0, start - suffix_start),
                offset + end - suffix_start,
            )
            for start, end in self.scan_ranges
            if end >= suffix_start
        )
        scan_swaths = tuple(
            swath
            for (start, end), swath in zip(self.scan_ranges, self.scan_swaths)
            if end >= suffix_start
        )
        route = tuple(transit) + suffix[1:]
        self._set_route(
            route,
            scan_ranges,
            scan_swaths,
            observation.planning_obstacle_mask,
            observation.planning_map_version,
        )
        self.phase = CoveragePhase.TRANSIT_ASTAR

    def _next_unconsumed_scan_start(self) -> int:
        assert self.follower is not None
        index = self.follower.index
        for start, end in self.scan_ranges:
            if index < start:
                return start
            if start <= index < end:
                return index + 1
        return len(self.route)

    def _set_route(
        self,
        route: Sequence[tuple[float, float, float]],
        scan_ranges: tuple[tuple[int, int], ...],
        scan_swaths: Sequence[ScanSwath],
        obstacle_mask: object,
        planning_map_version: int,
    ) -> None:
        blocked = self._route_blocked(route, obstacle_mask)
        if blocked is not None:
            segment_index, cell = blocked
            raise CoverageRouteBlockedError(segment_index, cell, planning_map_version)
        self.route = tuple(route)
        self.scan_ranges = scan_ranges
        if len(scan_ranges) != len(scan_swaths):
            raise ValueError("scan_ranges and scan_swaths must have matching lengths")
        self.scan_swaths = tuple(scan_swaths)
        self.follower = CoverageRouteFollower(
            self.route,
            scan_ranges=scan_ranges,
            r_min=self.r_min,
        )
        self.planning_map_version = planning_map_version
        self._route_revision += 1
        self._route_status = "ready"

    def _update_phase(
        self, observation: ControlObservation, guidance: CoverageGuidance
    ) -> None:
        assert self.follower is not None
        if guidance.is_complete:
            self.phase = CoveragePhase.COMPLETED
            return
        if guidance.scan_segment_index is None and (
            not self.scan_ranges
            or self.follower.index < self.scan_ranges[0][0]
        ):
            self.phase = CoveragePhase.TRANSIT_ASTAR
            return
        scan_index = guidance.scan_segment_index
        if scan_index is not None:
            desired_heading = self.scan_swaths[scan_index].heading
            heading_error = abs(
                _wrap_pi(desired_heading - observation.self_state.heading_rad)
            )
            self.phase = (
                CoveragePhase.SCANNING
                if (
                    heading_error <= self.sar_heading_tolerance_rad
                    and guidance.cross_track_error_cells
                    <= self.cross_track_tolerance_cells
                )
                else CoveragePhase.ALIGN_SCAN
            )
            return
        self.phase = CoveragePhase.ALIGN_SCAN

    def _scan_segment_index(self) -> int | None:
        assert self.follower is not None
        return self.follower.scan_segment_index

    @staticmethod
    def _route_blocked(
        route: Sequence[tuple[float, float, float]], obstacle_mask: object
    ) -> tuple[int, tuple[int, int]] | None:
        if not route:
            return (0, (0, 0))
        first_cell = (math.floor(route[0][0]), math.floor(route[0][1]))
        if SafetyEnvelope._cell_blocked(*first_cell, obstacle_mask):
            return (0, first_cell)
        for index, (start, end) in enumerate(zip(route, route[1:])):
            for cell in SafetyEnvelope._traversed_cells(*start[:2], *end[:2]):
                if SafetyEnvelope._cell_blocked(*cell, obstacle_mask):
                    return (index, cell)
        return None

    @staticmethod
    def _validate_command_modes(
        sensor_mode: SensorMode,
        operation_mode: OperationMode,
        observation: ControlObservation,
    ) -> None:
        if operation_mode not in observation.action_mask.allowed_operation_modes:
            raise InvalidControlCommand("operation mode is absent from action mask")
        if sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise InvalidControlCommand("sensor mode is absent from action mask")

    def route_snapshot(self) -> ControlRouteSnapshot:
        task = self.task
        route = (
            self.route
            if self._route_status == "ready" and not self._stopped
            else ()
        )
        follower = self.follower if route else None
        status = self._route_status
        if task is None:
            status = "unavailable"
        elif self._stopped:
            status = "cleared"
        elif not route and status == "ready":
            status = "pending"
        return ControlRouteSnapshot(
            task.task_id if task is not None else None,
            OperationMode.COVERAGE.value,
            self.phase.value,
            None,
            route,
            next_route_index(follower),
            self._route_revision,
            self.planning_map_version if route else None,
            status,
        )


__all__ = [
    "CoverageController",
    "CoveragePhase",
    "CoverageRouteBlockedError",
    "scan_endpoint_poses",
]

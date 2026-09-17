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
    ControllerContext,
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
        progress_timeout_min: float = 10.0,
        align_timeout_min: float = 8.0,
        max_stall_replans: int = 2,
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
        if (
            isinstance(progress_timeout_min, bool)
            or not math.isfinite(float(progress_timeout_min))
            or float(progress_timeout_min) <= 0.0
            or isinstance(align_timeout_min, bool)
            or not math.isfinite(float(align_timeout_min))
            or float(align_timeout_min) <= 0.0
        ):
            raise ValueError("coverage watchdog timeouts must be finite and positive")
        if (
            isinstance(max_stall_replans, bool)
            or not isinstance(max_stall_replans, int)
            or max_stall_replans < 0
        ):
            raise ValueError("max_stall_replans must be a non-negative integer")
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
        self.progress_timeout_min = float(progress_timeout_min)
        self.align_timeout_min = float(align_timeout_min)
        self.max_stall_replans = max_stall_replans
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
        self._generation = 0
        self._last_progress_cells: float | None = None
        self._last_progress_time: float | None = None
        self._align_started_time: float | None = None
        self._stalled_elapsed = 0.0
        self._stall_replans = 0
        self._watchdog_failed = False
        self._last_guidance: CoverageGuidance | None = None
        self._last_heading_error_deg: float | None = None
        self._last_cross_track_error_cells: float | None = None

    def reset(self, context: ControllerContext) -> None:
        super().reset(context)
        self._generation = context.generation

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
        self._last_progress_cells = self.follower.progress_cells
        self._last_progress_time = observation.timestamp_min
        self._align_started_time = None
        self._stalled_elapsed = 0.0
        self._stall_replans = 0
        self._watchdog_failed = False
        self._last_guidance = None
        self._last_heading_error_deg = None
        self._last_cross_track_error_cells = None

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
        watchdog_events, route_changed = self._check_progress_watchdog(
            observation, guidance
        )
        if route_changed:
            guidance = self.follower.update(
                position=observation.self_state.position,
                heading_rad=observation.self_state.heading_rad,
                speed_cells_min=observation.self_state.speed_cells_min,
                dt_min=observation.dt_min,
                action_spec=self.action_spec,
            )
            self._update_phase(observation, guidance)
        self._save_guidance_diagnostics(observation, guidance)
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
                tuple(watchdog_events)
                + (ControllerEventRequest("search_complete", {"task_id": self.task.task_id}),),
            )
        return ControlDecision(command, tuple(watchdog_events))

    def _save_guidance_diagnostics(
        self, observation: ControlObservation, guidance: CoverageGuidance
    ) -> None:
        self._last_guidance = guidance
        scan_index = guidance.scan_segment_index
        if scan_index is None or scan_index >= len(self.scan_swaths):
            self._last_heading_error_deg = None
        else:
            self._last_heading_error_deg = math.degrees(
                abs(
                    _wrap_pi(
                        self.scan_swaths[scan_index].heading
                        - observation.self_state.heading_rad
                    )
                )
            )
        self._last_cross_track_error_cells = guidance.cross_track_error_cells

    def _check_progress_watchdog(
        self,
        observation: ControlObservation,
        guidance: CoverageGuidance,
    ) -> tuple[tuple[ControllerEventRequest, ...], bool]:
        """Bound a stalled task without resetting its task-level clock on replans."""
        now = float(observation.timestamp_min)
        progress = float(guidance.progress_cells)
        if self._last_progress_cells is None:
            self._last_progress_cells = progress
            self._last_progress_time = now
        elif progress - self._last_progress_cells > 0.01:
            self._last_progress_cells = progress
            self._last_progress_time = now

        is_aligning = (
            guidance.scan_segment_index is not None
            and self.phase is CoveragePhase.ALIGN_SCAN
        )
        if is_aligning:
            if self._align_started_time is None:
                self._align_started_time = now
        else:
            self._align_started_time = None

        if self._watchdog_failed or self.phase is CoveragePhase.COMPLETED:
            return (), False

        progress_elapsed = (
            now - self._last_progress_time
            if self._last_progress_time is not None
            else 0.0
        )
        align_elapsed = (
            now - self._align_started_time
            if self._align_started_time is not None
            else 0.0
        )
        reason: str | None = None
        elapsed = 0.0
        if progress_elapsed >= self.progress_timeout_min:
            reason = "no_progress"
            elapsed = progress_elapsed
        elif align_elapsed >= self.align_timeout_min:
            reason = "align_stalled"
            elapsed = align_elapsed
        if reason is None:
            return (), False

        attempt = self._stall_replans + 1
        self._stalled_elapsed += max(0.0, elapsed)
        payload = {
            "task_id": self.task.task_id,
            "generation": self._generation,
            "reason": reason if attempt <= self.max_stall_replans else "coverage_stalled",
            "stall_reason": reason,
            "elapsed": float(elapsed),
            "stalled_elapsed": float(self._stalled_elapsed),
            "attempt": attempt,
        }
        # Move both clocks before invoking a planner.  This makes a repeated
        # observation at the same timestamp idempotent even if planning fails.
        self._last_progress_time = now
        if is_aligning:
            self._align_started_time = now
        else:
            self._align_started_time = None

        if attempt > self.max_stall_replans:
            self._watchdog_failed = True
            self._route_status = "unavailable"
            return (ControllerEventRequest("task_failed", payload),), False

        self._stall_replans = attempt
        route_changed = False
        try:
            if attempt == 1:
                self._replan_unflown_suffix(observation)
            else:
                self._replan_with_direction(observation)
            route_changed = True
        except (CoverageRouteBlockedError, ValueError, RuntimeError):
            # Preserve the last route for diagnostics, but never publish it as
            # ready after a watchdog replan failed.
            self._route_status = "unavailable"
        return (ControllerEventRequest("coverage_stalled", payload),), route_changed

    def _replan_with_direction(self, observation: ControlObservation) -> None:
        assert self.task is not None
        assert self.follower is not None
        if self.task.region_bbox is None:
            raise ValueError("coverage task requires region_bbox")
        width = self.task.region_bbox.col_end - self.task.region_bbox.col_start
        height = self.task.region_bbox.row_end - self.task.region_bbox.row_start
        current = self._direction or ("horizontal" if width >= height else "vertical")
        direction = "vertical" if current == "horizontal" else "horizontal"
        self._plan_route(
            observation,
            direction=direction,
            progress_offset_cells=self.follower.progress_cells,
        )
        self._direction = direction

    def _plan_route(
        self,
        observation: ControlObservation,
        direction: str | None = None,
        progress_offset_cells: float = 0.0,
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
        transit = self._plan_transit_to_pose(start_pose, entry, observation)
        coverage = self.planner.plan(
            self.task.region_bbox,
            entry,
            self.swath_width,
            self.r_min,
            direction,
            along_track_cells=self.sar_along_track_cells,
        )
        # The navigator reaches the scan entry position, but its final heading
        # is only a transit tangent.  Let the first scan segment carry the
        # physical heading transition while the SAR gate remains closed.
        transit[-1] = coverage.waypoints[0]
        route = tuple(transit) + tuple(coverage.waypoints[1:])
        offset = len(transit) - 1
        scan_ranges = tuple(
            (offset + start, offset + end) for start, end in coverage.scan_ranges
        )
        self._set_route(
            route,
            scan_ranges,
            coverage.swaths,
            observation.planning_obstacle_mask,
            observation.planning_map_version,
            progress_offset_cells=progress_offset_cells,
        )
        self.phase = CoveragePhase.TRANSIT_ASTAR

    def _plan_transit_to_pose(
        self,
        start_pose: tuple[float, float, float],
        target_pose: tuple[float, float, float],
        observation: ControlObservation,
    ) -> list[tuple[float, float, float]]:
        if isinstance(self.navigator, AStarNavigator):
            return self.navigator.plan_grid(
                start_pose,
                {target_pose[:2]},
                observation.planning_obstacle_mask,
                self.r_min,
                observation.planning_map_version,
                goal_heading_rad=target_pose[2],
            )
        # Keep the established custom-navigator test and extension contract;
        # the route assembly below still installs the true scan-entry heading.
        return self.navigator.plan_grid(
            start_pose,
            {target_pose[:2]},
            observation.planning_obstacle_mask,
            self.r_min,
            observation.planning_map_version,
        )

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
        progress_offset_cells = self.follower.progress_cells
        width = self.task.region_bbox.col_end - self.task.region_bbox.col_start
        height = self.task.region_bbox.row_end - self.task.region_bbox.row_start
        current = self._direction or ("horizontal" if width >= height else "vertical")
        direction = "vertical" if current == "horizontal" else "horizontal"
        try:
            self._plan_route(
                observation,
                direction=direction,
                progress_offset_cells=progress_offset_cells,
            )
        except (CoverageRouteBlockedError, ValueError, RuntimeError):
            self._route_status = "unavailable"
            raise
        self._direction = direction

    def _refresh_invalidated_route(self, observation: ControlObservation) -> None:
        if observation.planning_map_version == self.planning_map_version:
            return
        assert self.follower is not None
        unflown = self.route[self.follower.index + 1 :]
        current_pose = (
            *observation.self_state.position,
            observation.self_state.heading_rad,
        )
        if self._route_blocked(
            (current_pose, *unflown), observation.planning_obstacle_mask
        ) is not None:
            try:
                self._replan_unflown_suffix(observation)
            except (CoverageRouteBlockedError, ValueError, RuntimeError):
                self._route_status = "unavailable"
                raise
        else:
            self.planning_map_version = observation.planning_map_version
            self._route_status = (
                "unavailable" if self._watchdog_failed else "ready"
            )

    def _replan_unflown_suffix(self, observation: ControlObservation) -> None:
        assert self.follower is not None
        progress_offset_cells = self.follower.progress_cells
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
        transit = self._plan_transit_to_pose(start_pose, suffix[0], observation)
        first_scan_range_index = next(
            index
            for index, (start, end) in enumerate(self.scan_ranges)
            if end > suffix_start
        )
        scan_entry = suffix[0]
        transit[-1] = (
            scan_entry[0],
            scan_entry[1],
            self.scan_swaths[first_scan_range_index].heading,
        )
        suffix = (transit[-1],) + suffix[1:]
        offset = len(transit) - 1
        scan_ranges = tuple(
            (
                offset + max(0, start - suffix_start),
                offset + end - suffix_start,
            )
            for start, end in self.scan_ranges
            if end > suffix_start
        )
        scan_swaths = tuple(
            swath
            for (start, end), swath in zip(self.scan_ranges, self.scan_swaths)
            if end > suffix_start
        )
        route = tuple(transit) + suffix[1:]
        self._set_route(
            route,
            scan_ranges,
            scan_swaths,
            observation.planning_obstacle_mask,
            observation.planning_map_version,
            progress_offset_cells=progress_offset_cells,
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
        progress_offset_cells: float = 0.0,
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
            along_track_cells=self.sar_along_track_cells,
            progress_offset_cells=progress_offset_cells,
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
                    self.follower.scan_is_stable
                    and
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
            if self._route_status in {"ready", "unavailable"} and not self._stopped
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
            coverage_progress=self._coverage_progress_snapshot(),
        )

    def _coverage_progress_snapshot(self) -> dict[str, object]:
        guidance = self._last_guidance
        follower = self.follower
        return {
            "phase": self.phase.value,
            "progress_cells": (
                float(guidance.progress_cells) if guidance is not None else None
            ),
            "remaining_route_cells": (
                float(follower.remaining_route_cells)
                if guidance is not None and follower is not None
                else None
            ),
            "scan_segment_index": (
                guidance.scan_segment_index if guidance is not None else None
            ),
            "heading_error_deg": self._last_heading_error_deg,
            "cross_track_error_cells": self._last_cross_track_error_cells,
            "last_progress_min": self._last_progress_time,
            "stall_replans": self._stall_replans,
        }


__all__ = [
    "CoverageController",
    "CoveragePhase",
    "CoverageRouteBlockedError",
    "scan_endpoint_poses",
]

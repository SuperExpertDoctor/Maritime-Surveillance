"""Apply already-safe control commands to UAV entities."""

from __future__ import annotations

from dataclasses import dataclass
import math

from src.control.common.contracts import (
    ControlCommand,
    CoverageExecutionConfig,
    OperationMode,
    SensorMode,
)
from src.control.common.safety import SafetyIntervention, SafetyResult
from src.env.uav_entity import MAX_VISUAL_TRAIL_POINTS, UAVEntity


_STATUS_BY_OPERATION = {
    OperationMode.IDLE: "idle",
    OperationMode.TRANSIT: "transit",
    OperationMode.COVERAGE: "searching",
    # The public UAV status vocabulary has no separate probing state; keep
    # active probe motion visible as contact work while preserving PROBE in the
    # control command and scheduler state.
    OperationMode.PROBE: "tracking",
    OperationMode.TRACK: "tracking",
    OperationMode.RETURN: "returning",
    OperationMode.HOLDING: "holding",
}


@dataclass(frozen=True)
class ExecutionResult:
    requested_command: ControlCommand
    applied_command: ControlCommand
    interventions: tuple[SafetyIntervention, ...]
    distance_cells: float
    position: tuple[float, float]
    heading_rad: float

    @property
    def command(self) -> ControlCommand:
        """Compatibility alias for the command executed by the UAV."""
        return self.applied_command


class UAVDynamicsExecutor:
    """Mutate a UAV through its shared low-level motion primitive."""

    def __init__(self, coverage_execution: CoverageExecutionConfig | None = None):
        self.coverage_execution = coverage_execution or CoverageExecutionConfig(
            swath_width_cells=2.0,
            near_range_cells=0.25,
            min_turn_radius_cells=1.0,
            along_track_cells=0.8,
            heading_tolerance_rad=math.radians(2.0),
            cross_track_tolerance_cells=0.2,
        )

    def execute(
        self,
        uav: UAVEntity,
        command: ControlCommand | SafetyResult,
        dt_min: float,
        *,
        requested_command: ControlCommand | None = None,
    ) -> ExecutionResult:
        if isinstance(command, SafetyResult):
            if requested_command is not None:
                raise ValueError("requested_command cannot accompany SafetyResult")
            requested_command = command.requested_command
            interventions = command.interventions
            applied_command = command.applied_command
        else:
            applied_command = command
            requested_command = requested_command or command
            interventions = ()
        before_position = uav.float_position
        previous_geometry = (
            uav.sensor_mode,
            uav.sar_look_direction,
            uav.sar_scan_heading_rad,
            uav.sar_scan_origin,
        )
        distance = uav.apply_motion(
            applied_command.turn_rate_rad_min,
            applied_command.speed_cells_min,
            dt_min,
        )
        uav.status = _STATUS_BY_OPERATION[applied_command.operation_mode]
        uav.sensor_mode = applied_command.sensor_mode.value
        if applied_command.sensor_mode is SensorMode.SAR:
            same_geometry = previous_geometry == (
                SensorMode.SAR.value,
                applied_command.sar_look_direction,
                applied_command.sar_scan_heading_rad,
                applied_command.sar_scan_origin,
            )
            if not same_geometry:
                uav.sar_aperture_track = [before_position]
            uav.sar_look_direction = applied_command.sar_look_direction
            uav.sar_scan_heading_rad = applied_command.sar_scan_heading_rad
            uav.sar_scan_origin = applied_command.sar_scan_origin
            heading_error = abs(
                _wrap_pi(applied_command.sar_scan_heading_rad - uav.heading_rad)
            ) if applied_command.sar_scan_heading_rad is not None else math.inf
            uav.sar_heading_error_deg = math.degrees(heading_error)
            cross_track_error = _cross_track_error(
                uav.float_position,
                applied_command.sar_scan_origin,
                applied_command.sar_scan_heading_rad,
            )
            uav.sar_cross_track_error_cells = cross_track_error
            uav._append_sar_aperture_position()
            stable_margin = max(
                abs(applied_command.speed_cells_min) * dt_min,
                self.coverage_execution.along_track_cells / 2.0,
            )
            uav.sar_imaging = (
                applied_command.operation_mode is OperationMode.COVERAGE
                and heading_error <= self.coverage_execution.heading_tolerance_rad
                and cross_track_error <= self.coverage_execution.cross_track_tolerance_cells
                and uav._aperture_length() + 1e-12 >= stable_margin
            )
        else:
            uav.sar_look_direction = None
            uav._clear_sar_acquisition()
            uav.sar_scan_origin = None
            uav.sar_cross_track_error_cells = 0.0
        uav.trail.append(uav.float_position)
        if len(uav.trail) > MAX_VISUAL_TRAIL_POINTS:
            uav.trail.pop(0)
        uav.last_requested_command = requested_command
        uav.last_applied_command = applied_command
        uav.last_safety_interventions = interventions
        return ExecutionResult(
            requested_command,
            applied_command,
            interventions,
            distance,
            uav.float_position,
            uav.heading_rad,
        )


__all__ = ["ExecutionResult", "UAVDynamicsExecutor"]


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _cross_track_error(
    position: tuple[float, float],
    origin: tuple[float, float] | None,
    heading: float | None,
) -> float:
    if origin is None or heading is None:
        return math.inf
    dx = position[0] - origin[0]
    dy = position[1] - origin[1]
    return abs(-math.sin(heading) * dx + math.cos(heading) * dy)

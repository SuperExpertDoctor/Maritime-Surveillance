"""Apply already-safe control commands to UAV entities."""

from __future__ import annotations

from dataclasses import dataclass

from src.control.common.contracts import ControlCommand, OperationMode, SensorMode
from src.control.common.safety import SafetyIntervention, SafetyResult
from src.env.uav_entity import MAX_VISUAL_TRAIL_POINTS, UAVEntity


_STATUS_BY_OPERATION = {
    OperationMode.IDLE: "idle",
    OperationMode.TRANSIT: "transit",
    OperationMode.COVERAGE: "searching",
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
        distance = uav.apply_motion(
            applied_command.turn_rate_rad_min,
            applied_command.speed_cells_min,
            dt_min,
        )
        uav.status = _STATUS_BY_OPERATION[applied_command.operation_mode]
        uav.sensor_mode = applied_command.sensor_mode.value
        if applied_command.sensor_mode is SensorMode.SAR:
            uav.sar_imaging = True
        else:
            uav._clear_sar_acquisition()
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

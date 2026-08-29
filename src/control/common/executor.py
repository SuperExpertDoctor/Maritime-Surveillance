"""Apply already-safe control commands to UAV entities."""

from __future__ import annotations

from dataclasses import dataclass

from src.control.common.contracts import ControlCommand, OperationMode, SensorMode
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
    command: ControlCommand
    distance_cells: float
    position: tuple[float, float]
    heading_rad: float


class UAVDynamicsExecutor:
    """Mutate a UAV through its shared low-level motion primitive."""

    def execute(
        self, uav: UAVEntity, command: ControlCommand, dt_min: float
    ) -> ExecutionResult:
        distance = uav.apply_motion(
            command.turn_rate_rad_min, command.speed_cells_min, dt_min
        )
        uav.status = _STATUS_BY_OPERATION[command.operation_mode]
        uav.sensor_mode = command.sensor_mode.value
        if command.sensor_mode is SensorMode.SAR:
            uav.sar_imaging = True
        else:
            uav._clear_sar_acquisition()
        uav.trail.append(uav.float_position)
        if len(uav.trail) > MAX_VISUAL_TRAIL_POINTS:
            uav.trail.pop(0)
        uav.last_requested_command = command
        uav.last_applied_command = command
        return ExecutionResult(command, distance, uav.float_position, uav.heading_rad)


__all__ = ["ExecutionResult", "UAVDynamicsExecutor"]

"""Command validation and collision avoidance for control decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from src.control.common.contracts import (
    ActionSpec,
    ControlCommand,
    ControlObservation,
    OperationMode,
    SensorMode,
)


SAR_HEADING_STABILITY_TOLERANCE_RAD_MIN = math.radians(2.0)


class InvalidControlCommand(ValueError):
    """Raised when a controller command cannot be applied safely."""


class UnsafeControlState(RuntimeError):
    """Raised when no legal motion can avoid the published safety mask."""


@dataclass(frozen=True)
class SafetyIntervention:
    kind: str


@dataclass(frozen=True)
class SafetyResult:
    requested_command: ControlCommand
    applied_command: ControlCommand
    interventions: tuple[SafetyIntervention, ...]


class SafetyEnvelope:
    """Validate controller intent and constrain its next fixed-wing motion."""

    def __init__(self, action_spec: ActionSpec):
        self._action_spec = action_spec

    def apply(
        self,
        command: ControlCommand,
        observation: ControlObservation,
        dt_min: float,
    ) -> SafetyResult:
        self._validate_command(command, observation)
        interventions: list[SafetyIntervention] = []
        turn_rate = self._clip(
            command.turn_rate_rad_min,
            self._action_spec.min_turn_rate_rad_min,
            self._action_spec.max_turn_rate_rad_min,
        )
        speed = self._clip(
            command.speed_cells_min,
            self._action_spec.min_speed_cells_min,
            self._action_spec.max_speed_cells_min,
        )
        if turn_rate != command.turn_rate_rad_min:
            interventions.append(SafetyIntervention("turn_rate_clipped"))
        if speed != command.speed_cells_min:
            interventions.append(SafetyIntervention("speed_clipped"))

        applied = replace(command, turn_rate_rad_min=turn_rate, speed_cells_min=speed)
        if self._motion_blocked(applied, observation, dt_min):
            applied = self._safe_candidate(command, turn_rate, observation, dt_min)
            interventions.append(SafetyIntervention("motion_corrected"))

        if (
            applied.sensor_mode is SensorMode.SAR
            and (
                command.operation_mode is not OperationMode.COVERAGE
                or abs(applied.turn_rate_rad_min)
                > SAR_HEADING_STABILITY_TOLERANCE_RAD_MIN
            )
        ):
            applied = replace(applied, sensor_mode=SensorMode.OFF)
            interventions.append(SafetyIntervention("sensor_mode_masked"))
        elif applied.sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise InvalidControlCommand("sensor mode is absent from action mask")

        return SafetyResult(command, applied, tuple(interventions))

    def _validate_command(
        self, command: ControlCommand, observation: ControlObservation
    ) -> None:
        if command.schema_version != "control-command/v1":
            raise InvalidControlCommand("unsupported control command schema")
        if not all(
            math.isfinite(value)
            for value in (command.turn_rate_rad_min, command.speed_cells_min)
        ):
            raise InvalidControlCommand("continuous action values must be finite")
        if command.operation_mode not in observation.action_mask.allowed_operation_modes:
            raise InvalidControlCommand("operation mode is absent from action mask")
        if (
            command.target_contact_id is not None
            and command.target_contact_id not in observation.action_mask.target_contact_ids
        ):
            raise InvalidControlCommand("target contact is absent from action mask")

    def _safe_candidate(
        self,
        command: ControlCommand,
        requested_turn: float,
        observation: ControlObservation,
        dt_min: float,
    ) -> ControlCommand:
        candidate_turns = (
            requested_turn,
            self._action_spec.max_turn_rate_rad_min,
            self._action_spec.min_turn_rate_rad_min,
            0.0,
        )
        for turn_rate in candidate_turns:
            legal_turn = self._clip(
                turn_rate,
                self._action_spec.min_turn_rate_rad_min,
                self._action_spec.max_turn_rate_rad_min,
            )
            candidate = replace(
                command,
                turn_rate_rad_min=legal_turn,
                speed_cells_min=self._action_spec.min_speed_cells_min,
            )
            if not self._motion_blocked(candidate, observation, dt_min):
                return candidate
        raise UnsafeControlState("no collision-free legal control candidate")

    @staticmethod
    def _clip(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)

    @staticmethod
    def _motion_blocked(
        command: ControlCommand,
        observation: ControlObservation,
        dt_min: float,
    ) -> bool:
        start_col, start_row = observation.self_state.position
        mid_heading = observation.self_state.heading_rad + command.turn_rate_rad_min * dt_min / 2.0
        distance = command.speed_cells_min * dt_min
        end_col = start_col + distance * math.cos(mid_heading)
        end_row = start_row + distance * math.sin(mid_heading)
        mask = observation.planning_obstacle_mask
        return any(
            SafetyEnvelope._point_blocked(col, row, mask)
            for col, row in ((start_col, start_row), (end_col, end_row))
        )

    @staticmethod
    def _point_blocked(col: float, row: float, mask: object) -> bool:
        cols, rows = mask.shape
        if not (0.0 <= col < cols and 0.0 <= row < rows):
            return True
        return bool(mask[math.floor(col), math.floor(row)])


__all__ = [
    "InvalidControlCommand",
    "SAR_HEADING_STABILITY_TOLERANCE_RAD_MIN",
    "SafetyEnvelope",
    "SafetyIntervention",
    "SafetyResult",
    "UnsafeControlState",
]

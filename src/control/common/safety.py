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
        if command.sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise InvalidControlCommand("sensor mode is absent from action mask")
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

    @classmethod
    def _motion_blocked(
        cls,
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
            cls._cell_blocked(col, row, mask)
            for col, row in cls._traversed_cells(
                start_col, start_row, end_col, end_row
            )
        )

    @staticmethod
    def _traversed_cells(
        start_col: float,
        start_row: float,
        end_col: float,
        end_row: float,
    ) -> tuple[tuple[int, int], ...]:
        """Return the supercover cells for a segment in planning-grid space."""
        col = math.floor(start_col)
        row = math.floor(start_row)
        end_cell = (math.floor(end_col), math.floor(end_row))
        cells = [(col, row)]
        delta_col = end_col - start_col
        delta_row = end_row - start_row
        step_col = (delta_col > 0) - (delta_col < 0)
        step_row = (delta_row > 0) - (delta_row < 0)
        infinity = math.inf
        t_delta_col = abs(1.0 / delta_col) if step_col else infinity
        t_delta_row = abs(1.0 / delta_row) if step_row else infinity
        next_col = col + (1 if step_col > 0 else 0)
        next_row = row + (1 if step_row > 0 else 0)
        t_max_col = (next_col - start_col) / delta_col if step_col else infinity
        t_max_row = (next_row - start_row) / delta_row if step_row else infinity

        while (col, row) != end_cell:
            next_t_col = t_max_col if col != end_cell[0] else infinity
            next_t_row = t_max_row if row != end_cell[1] else infinity
            if math.isclose(next_t_col, next_t_row, rel_tol=0.0, abs_tol=1e-12):
                if step_col:
                    cells.append((col + step_col, row))
                if step_row:
                    cells.append((col, row + step_row))
                col += step_col
                row += step_row
                t_max_col += t_delta_col
                t_max_row += t_delta_row
            elif next_t_col < next_t_row:
                col += step_col
                t_max_col += t_delta_col
            else:
                row += step_row
                t_max_row += t_delta_row
            cells.append((col, row))
        return tuple(cells)

    @staticmethod
    def _cell_blocked(col: int, row: int, mask: object) -> bool:
        cols, rows = mask.shape
        if not (0 <= col < cols and 0 <= row < rows):
            return True
        return bool(mask[col, row])

    @staticmethod
    def _point_blocked(col: float, row: float, mask: object) -> bool:
        return SafetyEnvelope._cell_blocked(math.floor(col), math.floor(row), mask)


__all__ = [
    "InvalidControlCommand",
    "SAR_HEADING_STABILITY_TOLERANCE_RAD_MIN",
    "SafetyEnvelope",
    "SafetyIntervention",
    "SafetyResult",
    "UnsafeControlState",
]

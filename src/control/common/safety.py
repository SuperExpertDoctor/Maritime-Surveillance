"""Command validation and collision avoidance for control decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math

import numpy as np

from src.control.common.contracts import (
    ActionSpec,
    ControlCommand,
    ControlObservation,
    OperationMode,
    SensorMode,
)

SAR_HEADING_STABILITY_TOLERANCE_RAD_MIN = math.radians(2.0)


class ControlOutcome(str, Enum):
    """Classification for a control tick or rejected control attempt."""

    CLEAN = "clean"
    CLIPPED = "clipped"
    MASKED = "masked"
    INVALID = "invalid"
    UNSAFE = "unsafe"


class ControlError(Exception):
    """Base for errors that require a control-layer recovery policy."""

    code = "control_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(message)


class InvalidControlCommand(ControlError, ValueError):
    """Raised when a controller command cannot be applied safely."""

    outcome = ControlOutcome.INVALID
    code = "invalid_control_command"


class ProbeValidationError(ControlError, ValueError):
    """Raised when frozen probe evidence cannot satisfy its control contract."""

    outcome = ControlOutcome.UNSAFE
    code = "probe_validation"


class UnsafeControlState(ControlError, RuntimeError):
    """Raised when no legal motion can avoid the published safety mask."""

    outcome = ControlOutcome.UNSAFE
    code = "unsafe_control_state"


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
        forecasts = self._forecast_masks(observation, dt_min)
        if not self._can_escape_after(applied, observation, dt_min, forecasts):
            applied, relaxed = self._safe_candidate(
                command, turn_rate, observation, dt_min, forecasts
            )
            interventions.append(SafetyIntervention("motion_corrected"))
            if relaxed:
                interventions.append(
                    SafetyIntervention("escape_contract_relaxed")
                )

        if applied.sensor_mode is SensorMode.SAR and (
            # En-route imaging allows TRANSIT/RETURN legs to collect too;
            # the heading/turn-rate gates below still strip SAR on curves.
            command.operation_mode not in (
                OperationMode.COVERAGE,
                OperationMode.TRANSIT,
                OperationMode.RETURN,
            )
            or abs(applied.turn_rate_rad_min) > SAR_HEADING_STABILITY_TOLERANCE_RAD_MIN
        ):
            applied = replace(
                applied,
                sensor_mode=SensorMode.OFF,
                sar_look_direction=None,
                sar_scan_heading_rad=None,
                sar_scan_origin=None,
            )
            interventions.append(SafetyIntervention("sensor_mode_masked"))
        elif applied.sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise InvalidControlCommand("sensor mode is absent from action mask")
        elif applied.sensor_mode is not SensorMode.SAR:
            applied = replace(
                applied,
                sar_look_direction=None,
                sar_scan_heading_rad=None,
                sar_scan_origin=None,
            )

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
        if command.sensor_mode is SensorMode.SAR:
            if command.sar_look_direction not in ("left", "right"):
                raise InvalidControlCommand("SAR geometry requires a valid look direction")
            if command.sar_scan_heading_rad is None or not math.isfinite(
                command.sar_scan_heading_rad
            ):
                raise InvalidControlCommand("SAR geometry requires a finite scan heading")
            origin = command.sar_scan_origin
            valid_origin = isinstance(origin, (tuple, list)) and len(origin) == 2
            if valid_origin:
                try:
                    valid_origin = all(math.isfinite(float(value)) for value in origin)
                except (TypeError, ValueError):
                    valid_origin = False
            if not valid_origin:
                raise InvalidControlCommand("SAR geometry requires a finite scan origin")

    def _safe_candidate(
        self,
        command: ControlCommand,
        requested_turn: float,
        observation: ControlObservation,
        dt_min: float,
        forecasts: list[np.ndarray],
    ) -> tuple[ControlCommand, bool]:
        for candidate in self._candidate_commands(command, requested_turn):
            if self._can_escape_after(candidate, observation, dt_min, forecasts):
                return candidate, False
        # Wedged cells happen next to boundaries and storm edges: the full
        # escape-turn contract can reject every command even though a
        # physically free next step still exists.  Airframes never fail —
        # relax to single-step legality so the aircraft can creep back to
        # clear space instead of freezing forever.  Straight flight wins
        # over turning whenever a straight rollout is legal: usable
        # airframes must keep covering or transiting, never circle in
        # place, so turning escapes are only a last resort against walls.
        best = None
        best_key = None
        for candidate in self._candidate_commands(command, requested_turn):
            depth, retains_clearance = self._continuation_outlook(
                candidate, observation, dt_min, forecasts
            )
            if depth <= 0:
                continue
            key = (
                retains_clearance,
                abs(candidate.turn_rate_rad_min) <= 1e-6,
                depth,
                -abs(candidate.turn_rate_rad_min),
            )
            if best_key is None or key > best_key:
                best, best_key = candidate, key
        if best is not None:
            return best, True
        raise UnsafeControlState("no collision-free legal control candidate")

    def _candidate_commands(
        self, command: ControlCommand, requested_turn: float
    ) -> tuple[ControlCommand, ...]:
        candidate_turns = (
            requested_turn,
            self._action_spec.max_turn_rate_rad_min,
            self._action_spec.min_turn_rate_rad_min,
            0.0,
        )
        candidates = []
        for turn_rate, candidate_speed in (
            (turn, speed)
            for speed in (
                self._action_spec.min_speed_cells_min,
                self._action_spec.max_speed_cells_min,
            )
            for turn in candidate_turns
        ):
            legal_turn = self._clip(
                turn_rate,
                self._action_spec.min_turn_rate_rad_min,
                self._action_spec.max_turn_rate_rad_min,
            )
            candidates.append(
                replace(
                    command,
                    turn_rate_rad_min=legal_turn,
                    speed_cells_min=candidate_speed,
                )
            )
        return tuple(candidates)

    def _continuation_outlook(
        self,
        command: ControlCommand,
        observation: ControlObservation,
        dt_min: float,
        forecasts: list[np.ndarray],
    ) -> tuple[int, bool]:
        """(legal rollout depth, boundary turn-clearance retained).

        Deepest collision-free rollout the command opens up — the same
        shape the strict contract uses, minus its end-of-runway
        turn-clearance gate — plus whether any pose on that rollout
        still leaves a feasible turn back.  A rollout that stays legal
        but loses every clearance pose walks the airframe into a pocket
        it can never leave (e.g. hugging a boundary face-first), so the
        relaxed tier ranks it below continuations that keep a way out.
        0 depth means the command's first step is already illegal.
        """
        state = observation.self_state
        first = self._advance_pose(
            (*state.position, state.heading_rad),
            command.speed_cells_min,
            command.turn_rate_rad_min,
            0,
            dt_min,
            forecasts,
        )
        if first is None:
            return 0, False
        backups = tuple(
            turn
            for turn in (
                self._action_spec.min_turn_rate_rad_min,
                self._action_spec.max_turn_rate_rad_min,
            )
            if turn
        )
        final_mask = forecasts[-1]
        retains_clearance = any(
            self._has_boundary_turn_clearance(first, backup, dt_min, final_mask)
            for backup in backups
        )
        best = 1
        for turn in (
            self._action_spec.max_turn_rate_rad_min,
            self._action_spec.min_turn_rate_rad_min,
        ):
            if not turn:
                continue
            turn_steps = math.ceil(math.pi / (abs(turn) * dt_min))
            for escape_speed in (
                self._action_spec.min_speed_cells_min,
                self._action_spec.max_speed_cells_min,
            ):
                for duration in (
                    0,
                    math.ceil(turn_steps / 2),
                    turn_steps,
                    2 * turn_steps,
                ):
                    pose = first
                    depth = 1
                    for index in range(1, len(forecasts) - 1):
                        pose = self._advance_pose(
                            pose,
                            escape_speed,
                            turn if index <= duration else 0.0,
                            index,
                            dt_min,
                            forecasts,
                        )
                        if pose is None:
                            break
                        depth += 1
                        retains_clearance = retains_clearance or any(
                            self._has_boundary_turn_clearance(
                                pose, backup, dt_min, final_mask
                            )
                            for backup in backups
                        )
                    if depth > best:
                        best = depth
        return best, retains_clearance

    def _forecast_masks(
        self, observation: ControlObservation, dt_min: float
    ) -> list[np.ndarray]:
        """Keep present exclusions and forecast moving storms over an escape turn."""
        turns = [
            abs(turn)
            for turn in (
                self._action_spec.min_turn_rate_rad_min,
                self._action_spec.max_turn_rate_rad_min,
            )
            if turn
        ]
        steps = 1 + max(
            (2 * math.ceil(math.pi / (turn * dt_min)) for turn in turns), default=0
        )
        mask = observation.planning_obstacle_mask
        moving = [
            hazard
            for hazard in observation.hazards
            if hazard.hazard_type == "thunderstorm" and any(hazard.velocity_cells_min)
        ]
        if not moving:
            return [mask] * (steps + 1)
        cols, rows = mask.shape
        x, y = np.indices(mask.shape) + 0.5
        forecasts = [mask]
        for _ in range(steps):
            future = np.array(mask, copy=True)
            next_hazards = []
            for hazard in moving:
                # Match Thunderstorm.step(), including boundary reflection.
                cx, cy = hazard.center
                vx, vy = hazard.velocity_cells_min
                cx, cy = cx + vx * dt_min, cy + vy * dt_min
                h = hazard.half_extent_cells
                if cx - h < 0 or cx + h > cols:
                    vx, cx = -vx, min(max(cx, h), cols - h)
                if cy - h < 0 or cy + h > rows:
                    vy, cy = -vy, min(max(cy, h), rows - h)
                extent = h + hazard.safety_margin_cells
                future |= (np.abs(x - cx) <= extent) & (np.abs(y - cy) <= extent)
                next_hazards.append(
                    replace(hazard, center=(cx, cy), velocity_cells_min=(vx, vy))
                )
            moving = next_hazards
            forecasts.append(future)
        return forecasts

    def _can_escape_after(
        self,
        command: ControlCommand,
        observation: ControlObservation,
        dt_min: float,
        forecasts: list[np.ndarray],
    ) -> bool:
        """A clear next segment must leave a feasible bounded escape manoeuvre.

        Checking only the next endpoint can fly an aircraft into a pose from
        which every subsequent command hits a boundary. Roll out the same
        midpoint dynamics as the executor, including the next storm update.
        """

        def advance(pose, speed, turn, index):
            return self._advance_pose(
                pose, speed, turn, index, dt_min, forecasts
            )

        state = observation.self_state
        first = advance(
            (*state.position, state.heading_rad),
            command.speed_cells_min,
            command.turn_rate_rad_min,
            0,
        )
        if first is None:
            return False
        # Try straight departure, quarter/half turns, and an orbit. Fast
        # departure matters when a drifting storm overtakes a slow turn.
        for turn in (
            self._action_spec.max_turn_rate_rad_min,
            self._action_spec.min_turn_rate_rad_min,
        ):
            if not turn:
                continue
            turn_steps = math.ceil(math.pi / (abs(turn) * dt_min))
            for escape_speed in (
                self._action_spec.min_speed_cells_min,
                self._action_spec.max_speed_cells_min,
            ):
                for duration in (
                    0,
                    math.ceil(turn_steps / 2),
                    turn_steps,
                    2 * turn_steps,
                ):
                    pose = first
                    for index in range(1, len(forecasts) - 1):
                        pose = advance(
                            pose,
                            escape_speed,
                            turn if index <= duration else 0.0,
                            index,
                        )
                        if pose is None:
                            break
                    else:
                        if any(
                            self._has_boundary_turn_clearance(
                                pose, backup, dt_min, forecasts[-1]
                            )
                            for backup in (
                                self._action_spec.min_turn_rate_rad_min,
                                self._action_spec.max_turn_rate_rad_min,
                            )
                            if backup
                        ):
                            return True
        return False

    def _advance_pose(
        self,
        pose: tuple[float, float, float],
        speed: float,
        turn: float,
        index: int,
        dt_min: float,
        forecasts: list[np.ndarray],
    ) -> tuple[float, float, float] | None:
        """One midpoint-dynamics step, or None when it clips a blocked cell."""
        col, row, heading = pose
        mid = heading + turn * dt_min / 2
        end = (
            col + speed * dt_min * math.cos(mid),
            row + speed * dt_min * math.sin(mid),
            heading + turn * dt_min,
        )
        if any(
            self._cell_blocked(c, r, forecasts[index])
            for c, r in self._traversed_cells(col, row, *end[:2])
        ):
            return None
        if self._point_blocked(*end[:2], forecasts[index + 1]):
            return None
        return end

    def _has_boundary_turn_clearance(
        self,
        pose: tuple[float, float, float],
        turn: float,
        dt_min: float,
        mask: np.ndarray,
    ) -> bool:
        """Check circle extrema too: sampled chords alone can miss an edge."""
        radius = (
            self._action_spec.min_speed_cells_min
            * dt_min
            / (2 * math.sin(abs(turn) * dt_min / 2))
        )
        col, row, heading = pose
        sign = 1 if turn > 0 else -1
        cx, cy = col - sign * radius * math.sin(
            heading
        ), row + sign * radius * math.cos(heading)
        cols, rows = mask.shape
        if (
            cx - radius < 0
            or cy - radius < 0
            or cx + radius >= cols
            or cy + radius >= rows
        ):
            return False
        return True

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
    "ControlError",
    "ControlOutcome",
    "InvalidControlCommand",
    "ProbeValidationError",
    "SAR_HEADING_STABILITY_TOLERANCE_RAD_MIN",
    "SafetyEnvelope",
    "SafetyIntervention",
    "SafetyResult",
    "UnsafeControlState",
]

"""Abstract base for single-task heuristic controllers."""

from __future__ import annotations

from abc import abstractmethod
import math
from collections.abc import Sequence

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    ActionSpec,
    ControlCommand,
    ControlMode,
    ControlObservation,
    ControlTask,
    OperationMode,
    Pose,
    SensorMode,
    StopReason,
)


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


class RouteFollower:
    """Follow an immutable pose route without changing simulation state."""

    def __init__(self, poses: Sequence[Sequence[float]]) -> None:
        if not poses:
            raise ValueError("route must contain at least one pose")
        normalised = tuple(tuple(map(float, pose)) for pose in poses)
        if any(len(pose) != 3 for pose in normalised):
            raise ValueError("route poses must be (column, row, heading) triples")
        self._poses: tuple[Pose, ...] = normalised
        self._index = 0

    @property
    def poses(self) -> tuple[Pose, ...]:
        return self._poses

    @property
    def index(self) -> int:
        return self._index

    @property
    def is_complete(self) -> bool:
        return self._index >= len(self._poses) - 1

    def next_command(
        self,
        observation: ControlObservation,
        action_spec: ActionSpec,
        sensor_mode: SensorMode,
        operation_mode: OperationMode,
    ) -> ControlCommand:
        dt_min = observation.dt_min
        if not math.isfinite(dt_min) or dt_min <= 0.0:
            raise ValueError("observation dt_min must be a finite positive number")
        speed = min(
            max(observation.self_state.speed_cells_min, action_spec.min_speed_cells_min),
            action_spec.max_speed_cells_min,
        )
        position = observation.self_state.position
        arrival_radius = max(speed * dt_min, 0.05)
        while self._index < len(self._poses) - 1:
            target = self._poses[self._index + 1]
            if math.dist(position, target[:2]) > arrival_radius:
                break
            self._index += 1

        target = self._poses[min(self._index + 1, len(self._poses) - 1)]
        delta_col = target[0] - position[0]
        delta_row = target[1] - position[1]
        desired_heading = (
            math.atan2(delta_row, delta_col)
            if math.hypot(delta_col, delta_row) > 1e-12
            else target[2]
        )
        turn_rate = _wrap_pi(desired_heading - observation.self_state.heading_rad) / dt_min
        turn_rate = min(
            max(turn_rate, action_spec.min_turn_rate_rad_min),
            action_spec.max_turn_rate_rad_min,
        )
        return ControlCommand(turn_rate, speed, sensor_mode, operation_mode)


class HeuristicControllerBase(ControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.HEURISTIC

    @property
    @abstractmethod
    def operation_mode(self) -> OperationMode:
        raise NotImplementedError

    @abstractmethod
    def start_task(self, task: ControlTask, observation: ControlObservation) -> None:
        raise NotImplementedError

    @abstractmethod
    def is_complete(self, observation: ControlObservation) -> bool:
        raise NotImplementedError

    @abstractmethod
    def stop_task(self, reason: StopReason) -> None:
        raise NotImplementedError

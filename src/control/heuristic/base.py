"""Abstract base for single-task heuristic controllers."""

from __future__ import annotations

from abc import abstractmethod

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    ControlMode,
    ControlObservation,
    ControlTask,
    OperationMode,
    StopReason,
)


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

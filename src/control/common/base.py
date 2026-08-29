"""Abstract common controller interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.control.common.contracts import (
    ActionSpec,
    ControlDecision,
    ControlMode,
    ControlObservation,
    ControllerContext,
    ObservationSpec,
    PolicySource,
)


class ControllerBase(ABC):
    @property
    @abstractmethod
    def control_mode(self) -> ControlMode:
        raise NotImplementedError

    @property
    @abstractmethod
    def observation_spec(self) -> ObservationSpec:
        raise NotImplementedError

    @property
    @abstractmethod
    def action_spec(self) -> ActionSpec:
        raise NotImplementedError

    def reset(self, context: ControllerContext) -> None:
        self.context = context

    @abstractmethod
    def act(self, observation: ControlObservation) -> ControlDecision:
        raise NotImplementedError

    def close(self) -> None:
        pass


class LearningControllerBase(ControllerBase):
    @property
    def ownership_scope(self) -> str:
        return "sortie"

    @abstractmethod
    def load_policy(self, source: PolicySource) -> None:
        raise NotImplementedError

"""Abstract behavioral-cloning controller template."""

from __future__ import annotations

from abc import abstractmethod

from src.control.common.base import LearningControllerBase
from src.control.common.contracts import (
    ControlCommand,
    ControlDecision,
    ControlMode,
    ControlObservation,
)


class BCControllerBase(LearningControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.BC

    def act(self, observation: ControlObservation) -> ControlDecision:
        encoded = self.encode_observation(observation)
        output = self.predict_action(encoded)
        return ControlDecision(command=self.decode_action(output))

    @abstractmethod
    def encode_observation(self, observation: ControlObservation) -> object:
        raise NotImplementedError

    @abstractmethod
    def predict_action(self, encoded_observation: object) -> object:
        raise NotImplementedError

    @abstractmethod
    def decode_action(self, model_output: object) -> ControlCommand:
        raise NotImplementedError

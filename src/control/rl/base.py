"""Abstract reinforcement-learning controller template."""

from __future__ import annotations

from abc import abstractmethod

from src.control.common.base import LearningControllerBase
from src.control.common.contracts import (
    ControlCommand,
    ControlDecision,
    ControlMode,
    ControlObservation,
    ControllerContext,
)


class RLControllerBase(LearningControllerBase):
    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.RL

    def reset(self, context: ControllerContext) -> None:
        super().reset(context)
        self._policy_state = self.initial_policy_state()
        self._deterministic = True
        self.reset_episode(context.episode_id)

    def act(self, observation: ControlObservation) -> ControlDecision:
        command, self._policy_state = self.predict_action(
            observation,
            self._policy_state,
            self._deterministic,
        )
        return ControlDecision(command=command)

    def set_evaluation_mode(self, enabled: bool) -> None:
        self._deterministic = enabled

    @property
    @abstractmethod
    def observation_space(self) -> object:
        raise NotImplementedError

    @property
    @abstractmethod
    def action_space(self) -> object:
        raise NotImplementedError

    @abstractmethod
    def initial_policy_state(self) -> object | None:
        raise NotImplementedError

    @abstractmethod
    def predict_action(
        self,
        observation: ControlObservation,
        policy_state: object | None,
        deterministic: bool,
    ) -> tuple[ControlCommand, object | None]:
        raise NotImplementedError

    @abstractmethod
    def reset_episode(self, episode_id: str) -> None:
        raise NotImplementedError

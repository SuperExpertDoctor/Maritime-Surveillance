"""Fail-fast construction of control strategy implementations."""

from __future__ import annotations

from collections.abc import Callable
from inspect import signature
from math import pi

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    ActionSpec,
    ControlMode,
    ControlTask,
    ObservationSpec,
    OperationMode,
)
from src.control.heuristic.coverage import CoverageController
from src.control.heuristic.return_to_base import (
    ReturnToBaseController,
    SystemHoldingController,
)
from src.control.heuristic.tracking import TrackingController
from src.schedule.config_loader import ControlConfig


ControlProvider = Callable[..., ControllerBase]


class ControlFactoryError(RuntimeError):
    """Raised when a requested controller cannot be constructed safely."""


class ControlFactory:
    def __init__(
        self,
        config: ControlConfig,
        *,
        observation_spec: ObservationSpec | None = None,
        action_spec: ActionSpec | None = None,
    ) -> None:
        self._config = config
        self._observation_spec = observation_spec or ObservationSpec(
            config.observation.schema_version,
            config.observation.local_window_cells,
        )
        self._action_spec = action_spec or ActionSpec(-pi, pi, 0.1, 1.0)
        self._providers: dict[ControlMode, ControlProvider] = {
            ControlMode.HEURISTIC: self._create_builtin_heuristic,
        }

    def register(self, mode: ControlMode, provider: ControlProvider) -> None:
        mode = ControlMode(mode)
        if mode in self._providers:
            raise ControlFactoryError(
                f"provider for control mode {mode.value} is already registered"
            )
        if not callable(provider):
            raise ControlFactoryError(
                f"provider for control mode {mode.value} must be callable"
            )
        self._providers[mode] = provider

    def create_learning(
        self, uav_id: str, mode: ControlMode
    ) -> ControllerBase:
        mode = ControlMode(mode)
        if mode is ControlMode.HEURISTIC:
            raise ControlFactoryError(
                f"{mode.value} is not a learning mode for {uav_id}"
            )
        return self._create(uav_id, mode, None)

    def create_heuristic(
        self, uav_id: str, task: ControlTask
    ) -> ControllerBase:
        self._validate_heuristic_task(task)
        return self._create(uav_id, ControlMode.HEURISTIC, task)

    def _create(
        self,
        uav_id: str,
        mode: ControlMode,
        task: ControlTask | None,
    ) -> ControllerBase:
        provider = self._providers.get(mode)
        if provider is None:
            raise ControlFactoryError(
                f"controller for {uav_id} in mode {mode.value} is not registered"
            )
        controller = self._invoke_provider(provider, uav_id, task)
        if not isinstance(controller, ControllerBase):
            raise ControlFactoryError(
                f"provider for {uav_id} in mode {mode.value} returned "
                f"{type(controller).__name__}, not a ControllerBase"
            )
        if controller.control_mode is not mode:
            raise ControlFactoryError(
                f"provider for {uav_id} in mode {mode.value} returned "
                f"controller mode {controller.control_mode.value}"
            )
        return controller

    @staticmethod
    def _invoke_provider(
        provider: ControlProvider,
        uav_id: str,
        task: ControlTask | None,
    ) -> ControllerBase:
        provider_signature = signature(provider)
        try:
            provider_signature.bind(uav_id, task)
        except TypeError:
            try:
                provider_signature.bind(uav_id)
            except TypeError as exc:
                raise ControlFactoryError(
                    "control provider must accept uav_id and may accept task"
                ) from exc
            return provider(uav_id)
        return provider(uav_id, task)

    def _create_builtin_heuristic(
        self, uav_id: str, task: ControlTask | None
    ) -> ControllerBase:
        del uav_id
        assert task is not None
        kwargs = {
            "observation_spec": self._observation_spec,
            "action_spec": self._action_spec,
        }
        if task.task_type is OperationMode.COVERAGE:
            return CoverageController(**kwargs)
        if task.task_type is OperationMode.TRACK:
            return TrackingController(**kwargs)
        if task.task_type is OperationMode.RETURN:
            return ReturnToBaseController(**kwargs)
        if task.task_type is OperationMode.HOLDING:
            return SystemHoldingController(**kwargs)
        raise ControlFactoryError(
            f"no built-in heuristic controller for {task.task_type.value}"
        )

    @staticmethod
    def _validate_heuristic_task(task: ControlTask) -> None:
        if task.task_type is OperationMode.COVERAGE and task.region_bbox is None:
            raise ControlFactoryError("coverage task requires region_bbox")
        if task.task_type is OperationMode.TRACK and not task.target_contact_id:
            raise ControlFactoryError("track task requires target_contact_id")
        if task.task_type is OperationMode.RETURN and task.recovery_plan is None:
            raise ControlFactoryError("return task requires recovery_plan")
        if task.task_type not in {
            OperationMode.COVERAGE,
            OperationMode.TRACK,
            OperationMode.RETURN,
            OperationMode.HOLDING,
        }:
            raise ControlFactoryError(
                f"no built-in heuristic controller for {task.task_type.value}"
            )


__all__ = [
    "ControlFactory",
    "ControlFactoryError",
    "ControlProvider",
]

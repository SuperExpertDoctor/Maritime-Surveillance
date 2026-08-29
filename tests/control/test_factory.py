from __future__ import annotations

import pytest

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    ActionSpec,
    ControlMode,
    ControlTask,
    ObservationSpec,
    OperationMode,
    RecoveryPlan,
)
from src.control.common.factory import ControlFactory, ControlFactoryError
from src.control.heuristic.coverage import CoverageController
from src.control.heuristic.return_to_base import (
    ReturnToBaseController,
    SystemHoldingController,
)
from src.control.heuristic.tracking import TrackingController
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox


class ControllerDouble(ControllerBase):
    def __init__(self, mode: ControlMode):
        self._mode = mode

    @property
    def control_mode(self):
        return self._mode

    @property
    def observation_spec(self):
        return ObservationSpec("control-observation/v1", 11)

    @property
    def action_spec(self):
        return ActionSpec(-1.0, 1.0, 0.5, 1.0)

    def act(self, observation):
        raise NotImplementedError


@pytest.fixture
def config():
    return ConfigLoader.load()


@pytest.fixture
def factory(config):
    return ControlFactory(
        config.control,
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0),
    )


def test_unregistered_learning_mode_fails_fast(config):
    factory = ControlFactory(config.control)

    with pytest.raises(ControlFactoryError, match="UAV-1.*bc.*not registered"):
        factory.create_learning("UAV-1", ControlMode.BC)


@pytest.mark.parametrize("mode", [ControlMode.BC, ControlMode.RL])
def test_registered_learning_provider_is_keyed_by_control_mode(factory, mode):
    expected = ControllerDouble(mode)
    factory.register(mode, lambda uav_id, task: expected)

    assert factory.create_learning("UAV-1", mode) is expected


def test_registered_learning_provider_may_accept_only_the_uav_id(factory):
    expected = ControllerDouble(ControlMode.BC)
    factory.register(ControlMode.BC, lambda uav_id: expected)

    assert factory.create_learning("UAV-1", ControlMode.BC) is expected


def test_duplicate_registration_fails_fast(factory):
    factory.register(ControlMode.BC, lambda uav_id, task: ControllerDouble(ControlMode.BC))

    with pytest.raises(ControlFactoryError, match="bc.*already registered"):
        factory.register(
            ControlMode.BC,
            lambda uav_id, task: ControllerDouble(ControlMode.BC),
        )


def test_provider_mode_mismatch_fails_fast(factory):
    factory.register(
        ControlMode.BC,
        lambda uav_id, task: ControllerDouble(ControlMode.RL),
    )

    with pytest.raises(ControlFactoryError, match="UAV-1.*bc.*returned.*rl"):
        factory.create_learning("UAV-1", ControlMode.BC)


def test_create_learning_rejects_heuristic_mode(factory):
    with pytest.raises(ControlFactoryError, match="heuristic.*not a learning mode"):
        factory.create_learning("UAV-1", ControlMode.HEURISTIC)


def test_builtin_heuristic_task_controllers(factory):
    recovery = RecoveryPlan(
        base_id="B1",
        base_position=(0.0, 0.0),
        reservation_id="R1",
        path=((1.0, 1.0, 0.0), (0.0, 0.0, 0.0)),
        path_length_cells=2.0,
        reserve_cells=1.0,
        planning_map_version=1,
    )
    cases = [
        (
            ControlTask(
                "S1",
                OperationMode.COVERAGE,
                region_bbox=BBox(1, 1, 5, 5),
            ),
            CoverageController,
        ),
        (
            ControlTask("track:C1", OperationMode.TRACK, target_contact_id="C1"),
            TrackingController,
        ),
        (
            ControlTask("return:R1", OperationMode.RETURN, recovery_plan=recovery),
            ReturnToBaseController,
        ),
        (
            ControlTask("holding:UAV-1", OperationMode.HOLDING),
            SystemHoldingController,
        ),
    ]

    for task, expected_type in cases:
        controller = factory.create_heuristic("UAV-1", task)

        assert isinstance(controller, expected_type)
        assert controller.control_mode is ControlMode.HEURISTIC
        assert controller.operation_mode is task.task_type


@pytest.mark.parametrize(
    "task",
    [
        ControlTask("bad-coverage", OperationMode.COVERAGE),
        ControlTask("bad-track", OperationMode.TRACK),
        ControlTask("bad-return", OperationMode.RETURN),
        ControlTask("idle", OperationMode.IDLE),
    ],
)
def test_invalid_heuristic_task_fails_before_controller_creation(factory, task):
    with pytest.raises(ControlFactoryError, match=task.task_type.value):
        factory.create_heuristic("UAV-1", task)

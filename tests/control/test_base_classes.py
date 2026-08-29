import numpy as np
import pytest

from src.control.bc.base import BCControllerBase
from src.control.common.base import ControllerBase, LearningControllerBase
from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ControlCommand,
    ControlObservation,
    ControllerContext,
    ObservationSpec,
    OperationMode,
    SensorMode,
)
from src.control.heuristic.base import HeuristicControllerBase
from src.control.rl.base import RLControllerBase


class BCControllerDouble(BCControllerBase):
    def __init__(self):
        self.calls = []

    @property
    def observation_spec(self):
        return ObservationSpec(schema_version="control-observation/v1", local_window_cells=11)

    @property
    def action_spec(self):
        return ActionSpec(-1.0, 1.0, 0.0, 1.0)

    def load_policy(self, source):
        return None

    def encode_observation(self, observation):
        self.calls.append("encode")
        return "encoded"

    def predict_action(self, encoded_observation):
        self.calls.append("predict")
        assert encoded_observation == "encoded"
        return "model-output"

    def decode_action(self, model_output):
        self.calls.append("decode")
        assert model_output == "model-output"
        return ControlCommand(0.0, 0.25, SensorMode.SAR, OperationMode.COVERAGE)


class RLControllerDouble(RLControllerBase):
    def __init__(self):
        self.initial_state_calls = 0
        self.reset_episode_calls = []
        self.predict_calls = []

    @property
    def observation_spec(self):
        return ObservationSpec(schema_version="control-observation/v1", local_window_cells=11)

    @property
    def action_spec(self):
        return ActionSpec(-1.0, 1.0, 0.0, 1.0)

    @property
    def observation_space(self):
        return object()

    @property
    def action_space(self):
        return object()

    def load_policy(self, source):
        return None

    def initial_policy_state(self):
        self.initial_state_calls += 1
        return "initial-state"

    def predict_action(self, observation, policy_state, deterministic):
        self.predict_calls.append((policy_state, deterministic))
        return (
            ControlCommand(0.0, 0.25, SensorMode.SAR, OperationMode.COVERAGE),
            "next-state",
        )

    def reset_episode(self, episode_id):
        self.reset_episode_calls.append(episode_id)


@pytest.fixture
def observation():
    array = np.zeros((2, 2), dtype=np.float32)
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=None,
        local_info=array.copy(),
        local_value=array.copy(),
        obstacle_mask=array.copy(),
        searchable_mask=array.copy(),
        planning_obstacle_mask=array.copy(),
        planning_map_version=1,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask((SensorMode.OFF,), (OperationMode.IDLE,), ()),
    )


@pytest.fixture
def context():
    return ControllerContext(
        uav_id="uav-1",
        dt_min=1.0,
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=ActionSpec(-1.0, 1.0, 0.0, 1.0),
        episode_id="episode-1",
    )


@pytest.fixture
def bc_controller():
    return BCControllerDouble()


@pytest.fixture
def rl_controller():
    return RLControllerDouble()


@pytest.mark.parametrize(
    "controller_class",
    [
        ControllerBase,
        LearningControllerBase,
        HeuristicControllerBase,
        BCControllerBase,
        RLControllerBase,
    ],
)
def test_base_classes_cannot_be_instantiated_directly(controller_class):
    with pytest.raises(TypeError):
        controller_class()


def test_bc_template_method_returns_decoded_command(bc_controller, observation):
    result = bc_controller.act(observation)

    assert result.command.operation_mode is OperationMode.COVERAGE
    assert bc_controller.calls == ["encode", "predict", "decode"]


def test_rl_reset_initializes_policy_state_and_episode(rl_controller, context):
    rl_controller.reset(context)

    assert rl_controller.context is context
    assert rl_controller.initial_state_calls == 1
    assert rl_controller.reset_episode_calls == ["episode-1"]


def test_rl_template_method_tracks_policy_state_and_evaluation_mode(
    rl_controller, context, observation
):
    rl_controller.reset(context)
    result = rl_controller.act(observation)
    rl_controller.set_evaluation_mode(False)
    rl_controller.act(observation)

    assert result.command.operation_mode is OperationMode.COVERAGE
    assert rl_controller.predict_calls == [("initial-state", True), ("next-state", False)]

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from src.control.bc.base import BCControllerBase
from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ContactObservation,
    ControlCommand,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControllerContext,
    ObservationSpec,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.control.common.factory import ControlFactory
from src.control.common.operation_registry import InvalidOperationIntent, OperationRegistry
from src.control.rl.base import RLControllerBase
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager


TRACK_COMMAND = ControlCommand(
    0.0,
    1.0,
    SensorMode.EO,
    OperationMode.TRACK,
    target_contact_id="C1",
)


class TrackingBC(BCControllerBase):
    @property
    def observation_spec(self):
        return ObservationSpec("control-observation/v1", 11)

    @property
    def action_spec(self):
        return ActionSpec(-1.0, 1.0, 0.5, 1.0)

    def load_policy(self, source):
        del source

    def encode_observation(self, observation):
        return observation

    def predict_action(self, encoded_observation):
        del encoded_observation
        return TRACK_COMMAND

    def decode_action(self, model_output):
        return model_output


class TrackingRL(RLControllerBase):
    @property
    def observation_spec(self):
        return ObservationSpec("control-observation/v1", 11)

    @property
    def action_spec(self):
        return ActionSpec(-1.0, 1.0, 0.5, 1.0)

    @property
    def observation_space(self):
        return object()

    @property
    def action_space(self):
        return object()

    def load_policy(self, source):
        del source

    def initial_policy_state(self):
        return None

    def predict_action(self, observation, policy_state, deterministic):
        del observation, policy_state, deterministic
        return TRACK_COMMAND, None

    def reset_episode(self, episode_id):
        del episode_id


def _observation(mode: ControlMode, position=(10.0, 10.0)) -> ControlObservation:
    arrays = np.zeros((3, 3), dtype=np.float32)
    masks = np.zeros((3, 3), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=1.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="UAV-1",
            position=(1.0, 1.0),
            heading_rad=0.0,
            speed_cells_min=1.0,
            remaining_range_cells=100.0,
            control_mode=mode,
            control_owner=ControlOwner.LEARNING,
            operation_mode=OperationMode.TRACK,
            sensor_mode=SensorMode.EO,
            safety_intervened=False,
        ),
        local_info=arrays,
        local_value=arrays,
        obstacle_mask=masks,
        searchable_mask=np.ones((3, 3), dtype=bool),
        planning_obstacle_mask=np.zeros((30, 30), dtype=bool),
        planning_map_version=1,
        contacts=(
            ContactObservation(
                contact_id="C1",
                group_id="G1",
                estimated_position=position,
                estimated_velocity=(0.0, 0.0),
                source="eo",
                observed_at_min=1.0,
                age_min=0.0,
                confidence=1.0,
            ),
        ),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.EO),
            (OperationMode.COVERAGE, OperationMode.TRACK),
            ("C1",),
        ),
    )


@pytest.fixture(params=[ControlMode.BC, ControlMode.RL])
def learning_command(request):
    config = ConfigLoader.load()
    factory = ControlFactory(config.control)
    controller_type = TrackingBC if request.param is ControlMode.BC else TrackingRL
    factory.register(request.param, lambda uav_id, task: controller_type())
    controller = factory.create_learning("UAV-1", request.param)
    observation = _observation(request.param)
    if request.param is ControlMode.RL:
        controller.reset(
            ControllerContext(
                "UAV-1",
                1.0,
                controller.observation_spec,
                controller.action_spec,
                "episode-1",
            )
        )
    return controller.act(observation).command, observation


@pytest.fixture
def registry():
    config = ConfigLoader.load()
    state_manager = StateManager(config)
    return OperationRegistry(state_manager), state_manager


def test_entering_track_creates_and_binds_one_region_then_updates_without_duplicate(
    registry, learning_command
):
    operation_registry, state_manager = registry
    command, observation = learning_command

    operation_registry.reconcile("UAV-1", None, command, observation)
    first_region = state_manager.get_track_regions()[0]
    first_bbox = first_region.bbox
    updated = replace(
        observation,
        contacts=(replace(observation.contacts[0], estimated_position=(14.0, 12.0)),),
    )
    operation_registry.reconcile("UAV-1", command, command, updated)

    assert len(state_manager.get_track_regions()) == 1
    assert state_manager.get_track_regions()[0] is first_region
    assert first_region.bbox != first_bbox
    assert first_region.target_group_id == "G1"
    assert first_region.assigned_uav_id == "UAV-1"
    uav = state_manager.get_uav("UAV-1")
    assert uav.assigned_region_id == first_region.id
    assert uav.target_group_id == "G1"
    assert uav.sensor_mode == "eo"


def test_leaving_track_releases_only_this_uav_binding(registry, learning_command):
    operation_registry, state_manager = registry
    command, observation = learning_command
    operation_registry.reconcile("UAV-1", None, command, observation)
    coverage = ControlCommand(0.0, 1.0, SensorMode.SAR, OperationMode.COVERAGE)

    operation_registry.reconcile("UAV-1", command, coverage, observation)

    assert state_manager.get_track_regions() == []
    uav = state_manager.get_uav("UAV-1")
    assert uav.assigned_region_id is None
    assert uav.target_group_id is None
    assert uav.sensor_mode == "sar"


def test_leaving_track_preserves_another_uav_binding(registry, learning_command):
    operation_registry, state_manager = registry
    command, observation = learning_command
    operation_registry.reconcile("UAV-1", None, command, observation)
    operation_registry.reconcile("UAV-2", None, command, observation)
    coverage = ControlCommand(0.0, 1.0, SensorMode.SAR, OperationMode.COVERAGE)

    operation_registry.reconcile("UAV-1", command, coverage, observation)

    assert len(state_manager.get_track_regions()) == 1
    region = state_manager.get_track_regions()[0]
    assert region.assigned_uav_id == "UAV-2"
    assert state_manager.get_uav("UAV-2").assigned_region_id == region.id
    assert state_manager.get_uav("UAV-1").assigned_region_id is None


def test_unknown_track_contact_raises_without_changing_world_state(
    registry, learning_command
):
    operation_registry, state_manager = registry
    _, observation = learning_command
    invalid = replace(TRACK_COMMAND, target_contact_id="unknown")
    before_uav = deepcopy(state_manager.get_uav("UAV-1"))
    before_regions = deepcopy(state_manager.get_track_regions())

    with pytest.raises(InvalidOperationIntent, match="unknown"):
        operation_registry.reconcile("UAV-1", None, invalid, observation)

    assert state_manager.get_track_regions() == before_regions
    assert state_manager.get_uav("UAV-1") == before_uav


def test_track_contact_must_resolve_a_group_before_state_changes(
    registry, learning_command
):
    operation_registry, state_manager = registry
    command, observation = learning_command
    unresolved = replace(
        observation,
        contacts=(replace(observation.contacts[0], group_id=None),),
    )

    with pytest.raises(InvalidOperationIntent, match="group_id"):
        operation_registry.reconcile("UAV-1", None, command, unresolved)

    assert state_manager.get_track_regions() == []
    assert state_manager.get_uav("UAV-1").assigned_region_id is None

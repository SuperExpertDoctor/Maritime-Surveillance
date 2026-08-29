import math

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    ControlCommand,
    ControlMode,
    ControlObservation,
    ControlOwner,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.control.common.safety import (
    InvalidControlCommand,
    SafetyEnvelope,
    UnsafeControlState,
)


@pytest.fixture
def setup():
    action_spec = ActionSpec(-0.2, 0.2, 0.1, 0.5)
    envelope = SafetyEnvelope(action_spec)
    observation = make_observation(
        position=(1.5, 2.5),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.SAR, SensorMode.EO),
            (OperationMode.TRANSIT, OperationMode.COVERAGE, OperationMode.TRACK),
            ("G1",),
        ),
    )
    return envelope, observation


def make_observation(
    *,
    position=(1.5, 2.5),
    heading_rad=0.0,
    obstacle_mask=None,
    action_mask=None,
):
    obstacle_mask = (
        np.zeros((5, 5), dtype=bool) if obstacle_mask is None else obstacle_mask
    )
    action_mask = action_mask or ActionMask(
        (SensorMode.OFF, SensorMode.SAR, SensorMode.EO),
        (OperationMode.TRANSIT, OperationMode.COVERAGE, OperationMode.TRACK),
        ("G1",),
    )
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="UAV-1",
            position=position,
            heading_rad=heading_rad,
            speed_cells_min=0.25,
            remaining_range_cells=10.0,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.HEURISTIC,
            operation_mode=OperationMode.TRANSIT,
            sensor_mode=SensorMode.OFF,
            safety_intervened=False,
        ),
        local_info=np.zeros((1, 1), dtype=np.float32),
        local_value=np.zeros((1, 1), dtype=np.float32),
        obstacle_mask=np.zeros((1, 1), dtype=bool),
        searchable_mask=np.ones((1, 1), dtype=bool),
        planning_obstacle_mask=obstacle_mask,
        planning_map_version=1,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=action_mask,
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_safety_rejects_non_finite_continuous_actions(setup, value):
    envelope, observation = setup
    command = ControlCommand(
        turn_rate_rad_min=value,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    with pytest.raises(InvalidControlCommand, match="finite"):
        envelope.apply(command, observation, dt_min=1.0)


def test_safety_clips_turn_and_speed_to_action_spec(setup):
    envelope, observation = setup
    command = ControlCommand(
        turn_rate_rad_min=1.0,
        speed_cells_min=1.0,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.turn_rate_rad_min == pytest.approx(0.2)
    assert result.applied_command.speed_cells_min == pytest.approx(0.5)
    assert {item.kind for item in result.interventions} == {
        "turn_rate_clipped",
        "speed_clipped",
    }


def test_safety_preserves_task_intent_while_avoiding_blocked_boundary_step(setup):
    envelope, _ = setup
    observation = make_observation(position=(4.9, 2.5))
    requested = ControlCommand(
        turn_rate_rad_min=10.0,
        speed_cells_min=10.0,
        sensor_mode=SensorMode.EO,
        operation_mode=OperationMode.TRACK,
        target_contact_id="G1",
    )

    result = envelope.apply(requested, observation, dt_min=1.0)

    assert result.applied_command.operation_mode is OperationMode.TRACK
    assert result.applied_command.target_contact_id == "G1"
    assert result.applied_command.speed_cells_min == pytest.approx(0.1)
    assert result.interventions


def test_safety_avoids_predicted_obstacle_collision(setup):
    envelope, observation = setup
    obstacle_mask = observation.planning_obstacle_mask.copy()
    obstacle_mask[2, 2] = True
    observation = make_observation(obstacle_mask=obstacle_mask)
    command = ControlCommand(
        turn_rate_rad_min=0.0,
        speed_cells_min=0.5,
        sensor_mode=SensorMode.OFF,
        operation_mode=OperationMode.TRANSIT,
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.speed_cells_min == pytest.approx(0.1)
    assert "motion_corrected" in {item.kind for item in result.interventions}


def test_safety_masks_sar_outside_stable_coverage_leg(setup):
    envelope, observation = setup
    command = ControlCommand(
        turn_rate_rad_min=0.2,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.COVERAGE,
    )

    result = envelope.apply(command, observation, dt_min=1.0)

    assert result.applied_command.sensor_mode is SensorMode.OFF
    assert result.applied_command.operation_mode is OperationMode.COVERAGE
    assert "sensor_mode_masked" in {item.kind for item in result.interventions}


def test_safety_rejects_masked_operation_or_unknown_target(setup):
    envelope, observation = setup
    operation = ControlCommand(0.0, 0.25, SensorMode.OFF, OperationMode.RETURN)
    unknown_target = ControlCommand(
        0.0, 0.25, SensorMode.EO, OperationMode.TRACK, "missing"
    )

    with pytest.raises(InvalidControlCommand, match="operation"):
        envelope.apply(operation, observation, dt_min=1.0)
    with pytest.raises(InvalidControlCommand, match="target"):
        envelope.apply(unknown_target, observation, dt_min=1.0)


def test_safety_rejects_unknown_command_schema(setup):
    envelope, observation = setup
    command = ControlCommand(
        0.0,
        0.25,
        SensorMode.OFF,
        OperationMode.TRANSIT,
        schema_version="control-command/v0",
    )

    with pytest.raises(InvalidControlCommand, match="schema"):
        envelope.apply(command, observation, dt_min=1.0)


def test_safety_raises_when_no_legal_collision_free_candidate_exists(setup):
    envelope, observation = setup
    obstacle_mask = observation.planning_obstacle_mask.copy()
    obstacle_mask[1, 2] = True
    observation = make_observation(obstacle_mask=obstacle_mask)
    command = ControlCommand(0.0, 0.5, SensorMode.OFF, OperationMode.TRANSIT)

    with pytest.raises(UnsafeControlState):
        envelope.apply(command, observation, dt_min=1.0)

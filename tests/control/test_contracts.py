import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
    ControlCommand,
    ControlEvent,
    ControlMode,
    ControlObservation,
    OperationMode,
    SensorMode,
)


def test_control_command_uses_physical_step_units():
    command = ControlCommand(
        turn_rate_rad_min=0.2,
        speed_cells_min=0.25,
        sensor_mode=SensorMode.SAR,
        operation_mode=OperationMode.COVERAGE,
    )

    assert command.turn_rate_rad_min == 0.2
    assert command.speed_cells_min == 0.25
    assert command.target_contact_id is None


def test_action_mask_is_immutable_and_mode_specific():
    mask = ActionMask(
        allowed_sensor_modes=(SensorMode.OFF, SensorMode.SAR),
        allowed_operation_modes=(OperationMode.TRANSIT, OperationMode.COVERAGE),
        target_contact_ids=(),
    )

    assert SensorMode.EO not in mask.allowed_sensor_modes
    assert ControlMode("heuristic") is ControlMode.HEURISTIC


def test_control_event_copies_payload_into_an_immutable_snapshot():
    payload = {"task_id": "task-1"}

    event = ControlEvent(
        sequence=1,
        timestamp_min=2.0,
        event_type="task_started",
        source="coordinator",
        uav_id="uav-1",
        payload=payload,
    )
    payload["task_id"] = "task-2"

    assert event.payload == {"task_id": "task-1"}
    with pytest.raises(TypeError):
        event.payload["task_id"] = "task-3"


def test_control_observation_marks_arrays_read_only():
    arrays = [np.zeros((2, 2), dtype=np.float32) for _ in range(6)]

    observation = ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=0.0,
        dt_min=1.0,
        self_state=None,
        local_info=arrays[0],
        local_value=arrays[1],
        obstacle_mask=arrays[2],
        searchable_mask=arrays[3],
        planning_obstacle_mask=arrays[4],
        planning_map_version=1,
        contacts=(),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=None,
    )

    for array in (
        observation.local_info,
        observation.local_value,
        observation.obstacle_mask,
        observation.searchable_mask,
        observation.planning_obstacle_mask,
    ):
        assert not array.flags.writeable

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
    BaseObservation,
    ContactObservation,
    ControlCommand,
    ControlDecision,
    ControlEvent,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControllerEventRequest,
    HazardObservation,
    OperationMode,
    PolicySource,
    RecoveryPlan,
    SensorMode,
    UAVObservation,
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


def make_self_state() -> UAVObservation:
    return UAVObservation(
        uav_id="uav-1",
        position=(1.0, 2.0),
        heading_rad=0.0,
        speed_cells_min=0.25,
        remaining_range_cells=10.0,
        control_mode=ControlMode.HEURISTIC,
        control_owner=ControlOwner.HEURISTIC,
        operation_mode=OperationMode.IDLE,
        sensor_mode=SensorMode.OFF,
        safety_intervened=False,
    )


def make_observation(**overrides) -> ControlObservation:
    defaults = {
        "schema_version": "control-observation/v1",
        "timestamp_min": 0.0,
        "dt_min": 1.0,
        "self_state": make_self_state(),
        "local_info": np.zeros((2, 2), dtype=np.float32),
        "local_value": np.zeros((2, 2), dtype=np.float32),
        "obstacle_mask": np.zeros((2, 2), dtype=bool),
        "searchable_mask": np.ones((2, 2), dtype=bool),
        "planning_obstacle_mask": np.zeros((4, 4), dtype=bool),
        "planning_map_version": 1,
        "contacts": (),
        "hazards": (),
        "bases": (),
        "shared_uavs": (),
        "events": (),
        "action_mask": ActionMask((SensorMode.OFF,), (OperationMode.IDLE,), ()),
    }
    defaults.update(overrides)
    return ControlObservation(**defaults)


def test_control_event_request_deep_freezes_nested_payload():
    payload = {
        "metadata": {"task_id": "task-1"},
        "phases": ["transit"],
        "tags": {"priority"},
    }

    request = ControllerEventRequest(event_type="task_started", payload=payload)
    payload["metadata"]["task_id"] = "task-2"
    payload["phases"].append("coverage")
    payload["tags"].add("mutated")

    assert request.payload == {
        "metadata": {"task_id": "task-1"},
        "phases": ("transit",),
        "tags": frozenset({"priority"}),
    }
    with pytest.raises(TypeError):
        request.payload["metadata"]["task_id"] = "task-3"


def test_control_event_deep_freezes_nested_payload():
    payload = {"metadata": {"task_id": "task-1"}, "phases": ["transit"]}

    event = ControlEvent(
        sequence=1,
        timestamp_min=2.0,
        event_type="task_started",
        source="coordinator",
        uav_id="uav-1",
        payload=payload,
    )
    payload["metadata"]["task_id"] = "task-2"
    payload["phases"].append("coverage")

    assert event.payload == {
        "metadata": {"task_id": "task-1"},
        "phases": ("transit",),
    }
    with pytest.raises(TypeError):
        event.payload["metadata"]["task_id"] = "task-3"


def test_control_observation_owns_read_only_array_snapshots():
    arrays = [np.zeros((2, 2), dtype=np.float32) for _ in range(5)]

    observation = make_observation(
        local_info=arrays[0],
        local_value=arrays[1],
        obstacle_mask=arrays[2],
        searchable_mask=arrays[3],
        planning_obstacle_mask=arrays[4],
    )

    assert observation.local_info is not arrays[0]
    assert all(array.flags.writeable for array in arrays)
    arrays[0][0, 0] = 1.0
    assert observation.local_info[0, 0] == 0.0

    for array in (
        observation.local_info,
        observation.local_value,
        observation.obstacle_mask,
        observation.searchable_mask,
        observation.planning_obstacle_mask,
    ):
        assert not array.flags.writeable


def test_control_observation_normalizes_collection_and_mask_snapshots():
    contacts = [
        ContactObservation(
            contact_id="contact-1",
            group_id=None,
            estimated_position=(3.0, 4.0),
            estimated_velocity=(0.0, 0.0),
            source="sar",
            observed_at_min=0.0,
            age_min=0.0,
            confidence=1.0,
        )
    ]
    hazards = [
        HazardObservation(
            hazard_id="hazard-1",
            hazard_type="weather",
            center=(5.0, 6.0),
            half_extent_cells=1.0,
            velocity_cells_min=(0.0, 0.0),
            intensity=0.5,
        )
    ]
    bases = [BaseObservation("base-1", (0.0, 0.0), 1, 0)]
    shared_uavs = [make_self_state()]
    events = [
        ControlEvent(1, 0.0, "weather_updated", "env", "uav-1", {"version": 1})
    ]
    allowed_sensor_modes = [SensorMode.OFF]
    allowed_operation_modes = [OperationMode.IDLE]
    target_contact_ids = []

    observation = make_observation(
        contacts=contacts,
        hazards=hazards,
        bases=bases,
        shared_uavs=shared_uavs,
        events=events,
        action_mask=ActionMask(
            allowed_sensor_modes,
            allowed_operation_modes,
            target_contact_ids,
        ),
    )
    contacts.clear()
    hazards.clear()
    bases.clear()
    shared_uavs.clear()
    events.clear()
    allowed_sensor_modes.append(SensorMode.SAR)
    allowed_operation_modes.append(OperationMode.COVERAGE)
    target_contact_ids.append("contact-1")

    assert isinstance(observation.contacts, tuple)
    assert isinstance(observation.hazards, tuple)
    assert isinstance(observation.bases, tuple)
    assert isinstance(observation.shared_uavs, tuple)
    assert isinstance(observation.events, tuple)
    assert observation.contacts[0].contact_id == "contact-1"
    assert observation.hazards[0].hazard_id == "hazard-1"
    assert observation.bases[0].base_id == "base-1"
    assert observation.shared_uavs[0].uav_id == "uav-1"
    assert observation.events[0].event_type == "weather_updated"
    assert observation.action_mask.allowed_sensor_modes == (SensorMode.OFF,)
    assert observation.action_mask.allowed_operation_modes == (OperationMode.IDLE,)
    assert observation.action_mask.target_contact_ids == ()


def test_control_decision_snapshots_event_requests():
    events = [ControllerEventRequest(event_type="task_started")]

    decision = ControlDecision(
        command=ControlCommand(0.0, 0.25, SensorMode.SAR, OperationMode.COVERAGE),
        events=events,
    )
    events.append(ControllerEventRequest(event_type="task_completed"))

    assert decision.events == (ControllerEventRequest(event_type="task_started"),)


def test_policy_source_deep_freezes_metadata():
    metadata = {"model": {"revision": "v1"}, "labels": ["baseline"]}

    source = PolicySource(uri="models://controller", metadata=metadata)
    metadata["model"]["revision"] = "v2"
    metadata["labels"].append("candidate")

    assert source.metadata == {
        "model": {"revision": "v1"},
        "labels": ("baseline",),
    }
    with pytest.raises(TypeError):
        source.metadata["model"]["revision"] = "v3"


def test_coordinate_and_path_fields_snapshot_mutable_collections():
    uav_position = [1.0, 2.0]
    contact_position = [3.0, 4.0]
    contact_velocity = [0.1, 0.2]
    hazard_center = [5.0, 6.0]
    hazard_velocity = [0.3, 0.4]
    base_position = [7.0, 8.0]
    recovery_base_position = [9.0, 10.0]
    path = [[9.0, 10.0, 0.0], [11.0, 12.0, 0.5]]

    uav = UAVObservation(
        uav_id="uav-1",
        position=uav_position,
        heading_rad=0.0,
        speed_cells_min=0.25,
        remaining_range_cells=10.0,
        control_mode=ControlMode.HEURISTIC,
        control_owner=ControlOwner.HEURISTIC,
        operation_mode=OperationMode.IDLE,
        sensor_mode=SensorMode.OFF,
        safety_intervened=False,
    )
    contact = ContactObservation(
        "contact-1", None, contact_position, contact_velocity, "sar", 0.0, 0.0, 1.0
    )
    hazard = HazardObservation(
        "hazard-1", "weather", hazard_center, 1.0, hazard_velocity, 0.5
    )
    base = BaseObservation("base-1", base_position, 1, 0)
    recovery_plan = RecoveryPlan(
        "base-1", recovery_base_position, "reservation-1", path, 2.0, 1.0, 1
    )

    for collection in (
        uav_position,
        contact_position,
        contact_velocity,
        hazard_center,
        hazard_velocity,
        base_position,
        recovery_base_position,
    ):
        collection.clear()
    path[0].clear()
    path.append([13.0, 14.0, 1.0])

    assert uav.position == (1.0, 2.0)
    assert contact.estimated_position == (3.0, 4.0)
    assert contact.estimated_velocity == (0.1, 0.2)
    assert hazard.center == (5.0, 6.0)
    assert hazard.velocity_cells_min == (0.3, 0.4)
    assert base.position == (7.0, 8.0)
    assert recovery_plan.base_position == (9.0, 10.0)
    assert recovery_plan.path == ((9.0, 10.0, 0.0), (11.0, 12.0, 0.5))

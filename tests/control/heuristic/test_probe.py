from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionSpec,
    ContactObservation,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControlTask,
    ObservationSpec,
    OperationMode,
    SensorMode,
    UAVObservation,
)
from src.control.common.observation import ObservationProvider
from src.control.heuristic.navigation import PathNotFoundError
from src.control.heuristic.probe import ProbeValidationError
from src.mission.contracts import ContactSnapshot, ProbeSession
from src.schedule.config_loader import ConfigLoader


class NavigatorSpy:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.calls: list[tuple[float, float]] = []
        self.targets: list[tuple[float, float]] = []

    def plan_to_standoff(self, start, target, radius, mask, r_min, map_version=0):
        self.calls.append((float(radius), float(map_version)))
        self.targets.append(tuple(map(float, target)))
        if self.blocked:
            raise PathNotFoundError(tuple(start), "probe standoff", map_version)
        goal = (float(target[0]) - float(radius), float(target[1]), 0.0)
        return [tuple(start), goal]


class TrackerSpy:
    def plan_entry(self, pose, target, radius):
        return SimpleNamespace(waypoints=[tuple(pose), tuple(pose)])

    def compute_guidance(self, pose, target, radius, speed, **_kwargs):
        return 0.0, speed


def _contact() -> ContactObservation:
    return ContactObservation(
        contact_id="C0001",
        group_id="C0001",
        estimated_position=(12.0, 10.0),
        estimated_velocity=(0.0, 0.0),
        source="UAV-1",
        observed_at_min=1.0,
        age_min=0.0,
        confidence=1.0,
    )


def _history() -> ContactSnapshot:
    return ContactSnapshot(
        "C0001", 1, "observing", "unknown", None, 0.0, 1.0,
        (12.0, 10.0), (0.0, 0.0), 0.1, "UAV-1", "P0001", None,
        None, 0.0, (),
    )


def _probe(phase: str = "baseline") -> ProbeSession:
    return ProbeSession(
        "P0001", "C0001", "UAV-1", phase, 0.0, None, 0.0, (), (), 0.0, None
    )


def _observation(*, probe: ProbeSession, position=(2.0, 10.0), contacts=None):
    contacts = (_contact(),) if contacts is None else tuple(contacts)
    arrays = np.zeros((24, 24), dtype=bool)
    return ControlObservation(
        schema_version="control-observation/v2",
        timestamp_min=1.0,
        dt_min=1.0,
        self_state=UAVObservation(
            "UAV-1", position, 0.0, 0.75, 100.0, ControlMode.HEURISTIC,
            ControlOwner.HEURISTIC, OperationMode.PROBE, SensorMode.OFF, False,
        ),
        local_info=arrays,
        local_value=arrays,
        obstacle_mask=arrays,
        searchable_mask=np.ones((24, 24), dtype=bool),
        planning_obstacle_mask=arrays,
        planning_map_version=3,
        contacts=contacts,
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ObservationProvider._action_mask(
            ControlOwner.HEURISTIC, OperationMode.PROBE, contacts
        ),
        probe=probe,
        contact_histories=(_history(),),
    )


def _controller(navigator: NavigatorSpy):
    from src.control.heuristic.probe import ProbeController

    config = ConfigLoader.load().mission.contact
    return ProbeController(
        observation_spec=ObservationSpec("control-observation/v2", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0),
        contact_config=config,
        navigator=navigator,
        tracker=TrackerSpy(),
        r_min=1.0,
    )


def _start(controller, observation):
    controller.start_task(
        ControlTask("probe:C0001", OperationMode.PROBE, target_contact_id="C0001", probe_id="P0001"),
        observation,
    )


def test_probe_uses_frozen_session_to_progress_baseline_then_near():
    navigator = NavigatorSpy()
    controller = _controller(navigator)
    baseline = _observation(probe=_probe())
    _start(controller, baseline)

    approaching = controller.act(baseline)

    assert approaching.command.operation_mode is OperationMode.PROBE
    assert approaching.command.sensor_mode is SensorMode.OFF
    assert navigator.calls == [(ConfigLoader.load().mission.contact.baseline_standoff_cells, 3.0)]

    at_baseline = replace(
        baseline,
        self_state=replace(baseline.self_state, position=(10.2, 10.0)),
    )
    observing = controller.act(at_baseline)
    assert observing.command.sensor_mode is SensorMode.EO
    assert [event.event_type for event in observing.events] == ["probe_phase_changed"]

    closing = replace(at_baseline, probe=_probe("closing"))
    controller.act(closing)
    assert navigator.calls[-1][0] == ConfigLoader.load().mission.contact.near_standoff_cells


def test_probe_start_task_plans_a_baseline_route_for_the_route_snapshot():
    navigator = NavigatorSpy()
    controller = _controller(navigator)
    observation = _observation(probe=_probe())

    _start(controller, observation)
    snapshot = controller.route_snapshot()

    assert snapshot is not None
    assert navigator.calls == [
        (ConfigLoader.load().mission.contact.baseline_standoff_cells, 3.0)
    ]
    assert snapshot.task_id == "probe:C0001"
    assert snapshot.task_type == OperationMode.PROBE.value
    assert snapshot.phase == "baseline"
    assert snapshot.target_contact_id == "C0001"
    assert snapshot.route == controller._route.poses
    assert snapshot.next_index == 1
    assert snapshot.route_revision == 1
    assert snapshot.status == "ready"


def test_probe_missing_contact_exports_unavailable_route_without_motion():
    controller = _controller(NavigatorSpy())
    observation = _observation(probe=_probe(), contacts=())

    _start(controller, observation)
    snapshot = controller.route_snapshot()

    assert snapshot is not None
    assert snapshot.status == "unavailable"
    assert snapshot.route == ()


def test_missing_contact_does_not_advance_the_frozen_probe_session():
    controller = _controller(NavigatorSpy())
    observation = _observation(probe=_probe())
    _start(controller, observation)

    decision = controller.act(replace(observation, contacts=(), contact_histories=()))

    assert decision.command.operation_mode is OperationMode.HOLDING
    assert not [event for event in decision.events if event.event_type == "probe_phase_changed"]


def test_probe_validation_errors_are_control_errors_with_reason():
    controller = _controller(NavigatorSpy())
    observation = _observation(probe=_probe())
    invalid_contact = replace(
        _contact(), estimated_velocity=(float("nan"), 0.0),
    )

    with pytest.raises(ProbeValidationError, match="finite") as caught:
        controller._predicted_contact_position(
            observation, invalid_contact,
        )

    assert caught.value.code == "probe_validation"


def test_awaiting_assessment_keeps_probe_reservation_until_assessment():
    controller = _controller(NavigatorSpy())
    observation = _observation(probe=_probe("awaiting_assessment"))
    _start(controller, observation)

    decision = controller.act(observation)

    assert decision.command.operation_mode is OperationMode.PROBE
    assert decision.command.target_contact_id == "C0001"
    assert decision.command.sensor_mode is SensorMode.OFF


def test_production_probe_mask_allows_holding_fallback_without_contact():
    mask = ObservationProvider._action_mask(
        ControlOwner.HEURISTIC, OperationMode.PROBE, ()
    )

    assert OperationMode.HOLDING in mask.allowed_operation_modes
    assert SensorMode.OFF in mask.allowed_sensor_modes


def test_production_probe_mask_allows_holding_before_first_probe_command():
    task = ControlTask(
        "probe:C0001", OperationMode.PROBE,
        target_contact_id="C0001", probe_id="P0001",
    )
    mask = ObservationProvider._action_mask(
        ControlOwner.HEURISTIC, OperationMode.IDLE, (), task=task
    )

    assert OperationMode.HOLDING in mask.allowed_operation_modes


def test_unreachable_near_orbit_reports_probe_blocked_once():
    controller = _controller(NavigatorSpy(blocked=True))
    observation = _observation(probe=_probe("closing"))
    _start(controller, observation)

    first = controller.act(observation)
    second = controller.act(observation)

    assert [event.event_type for event in first.events] == ["probe_blocked"]
    assert second.events == ()


def test_probe_predicts_observable_motion_and_replans_on_fresh_estimate():
    navigator = NavigatorSpy()
    controller = _controller(navigator)
    first_contact = replace(
        _contact(), estimated_velocity=(1.0, 0.5), observed_at_min=1.0
    )
    first = _observation(probe=_probe(), contacts=(first_contact,))
    _start(controller, first)

    controller.act(first)

    assert navigator.targets == [(13.0, 10.5)]

    fresh_contact = replace(
        first_contact,
        estimated_position=(14.0, 11.0),
        estimated_velocity=(2.0, -1.0),
        observed_at_min=2.0,
    )
    fresh = replace(first, timestamp_min=2.0, contacts=(fresh_contact,))
    controller.act(fresh)

    assert len(navigator.targets) == 2
    assert navigator.targets[-1] == (16.0, 10.0)

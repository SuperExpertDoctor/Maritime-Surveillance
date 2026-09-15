from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
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
from src.control.heuristic.navigation import PathNotFoundError
from src.mission.contracts import ContactSnapshot, ProbeSession
from src.schedule.config_loader import ConfigLoader


class NavigatorSpy:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.calls: list[tuple[float, float]] = []

    def plan_to_standoff(self, start, target, radius, mask, r_min, map_version=0):
        self.calls.append((float(radius), float(map_version)))
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
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.EO),
            (OperationMode.PROBE, OperationMode.HOLDING),
            tuple(contact.contact_id for contact in contacts),
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


def test_missing_contact_does_not_advance_the_frozen_probe_session():
    controller = _controller(NavigatorSpy())
    observation = _observation(probe=_probe())
    _start(controller, observation)

    decision = controller.act(replace(observation, contacts=(), contact_histories=()))

    assert decision.command.operation_mode is OperationMode.HOLDING
    assert not [event for event in decision.events if event.event_type == "probe_phase_changed"]


def test_unreachable_near_orbit_reports_probe_blocked_once():
    controller = _controller(NavigatorSpy(blocked=True))
    observation = _observation(probe=_probe("closing"))
    _start(controller, observation)

    first = controller.act(observation)
    second = controller.act(observation)

    assert [event.event_type for event in first.events] == ["probe_blocked"]
    assert second.events == ()

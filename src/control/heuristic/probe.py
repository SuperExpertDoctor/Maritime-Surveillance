"""Observation-only progressive contact investigation control."""

from __future__ import annotations

import math
from collections.abc import Sequence

from src.control.common.contracts import (
    ActionSpec, ContactObservation, ControlCommand, ControlDecision,
    ControlObservation, ControlTask, ControllerEventRequest, ObservationSpec,
    OperationMode, SensorMode, StopReason,
)
from src.control.heuristic.base import HeuristicControllerBase, RouteFollower
from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.control.heuristic.tracking import plan_contact_orbit_entry
from src.mission.config import ContactConfig
from src.utils.track_orbit import LGVFTracker


class ProbeController(HeuristicControllerBase):
    """Navigate a frozen probe phase; simulation owns all evidence timing."""

    def __init__(
        self, *, observation_spec: ObservationSpec, action_spec: ActionSpec,
        contact_config: ContactConfig, navigator: AStarNavigator | None = None,
        tracker: LGVFTracker | None = None, r_min: float = 1.0,
    ) -> None:
        if not math.isfinite(r_min) or r_min <= 0.0:
            raise ValueError("r_min must be finite and positive")
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self._config = contact_config
        self.navigator = navigator or AStarNavigator()
        self.tracker = tracker or LGVFTracker(R_min=r_min)
        self.r_min = float(r_min)
        self.task: ControlTask | None = None
        self._route: RouteFollower | None = None
        self._route_phase: str | None = None
        self._reported_phases: set[str] = set()
        self._blocked = False
        self._timeout_reported = False

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    @property
    def operation_mode(self) -> OperationMode:
        return OperationMode.PROBE

    def start_task(self, task: ControlTask, observation: ControlObservation) -> None:
        if task.task_type is not OperationMode.PROBE or not task.target_contact_id or not task.probe_id:
            raise ValueError("probe tasks require PROBE, target_contact_id, and probe_id")
        self.task = task
        self._route = None
        self._route_phase = None
        self._reported_phases.clear()
        self._blocked = False
        self._timeout_reported = False

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self.task is None:
            raise RuntimeError("start_task must be called before act")
        probe = observation.probe
        contact = self._contact(observation.contacts)
        if probe is None or contact is None or not self._matches(probe, contact):
            return ControlDecision(self._holding_command(observation))
        if probe.phase == "finished":
            return self._finished_decision(observation, probe.completed_reason)
        if probe.phase == "awaiting_assessment":
            return ControlDecision(self._holding_command(observation))
        if self._blocked:
            return ControlDecision(self._holding_command(observation))

        phase, standoff = self._phase_and_standoff(probe.phase)
        try:
            if self._route_phase != probe.phase or self._route is None:
                self._route = self._plan_route(observation, contact, standoff)
                self._route_phase = probe.phase
            if self._at_standoff(observation, contact, standoff):
                entry = plan_contact_orbit_entry(
                    self.tracker, self._pose(observation), contact.estimated_position, standoff
                )
                self._route = RouteFollower(entry)
                self._route_phase = probe.phase
                command = self._route.next_command(
                    observation, self.action_spec, SensorMode.EO, OperationMode.PROBE
                )
                events = self._phase_event(phase)
            else:
                command = self._route.next_command(
                    observation, self.action_spec, SensorMode.OFF, OperationMode.PROBE
                )
                events = ()
        except (PathNotFoundError, ValueError) as exc:
            self._blocked = True
            return self._blocked_decision(observation, str(exc))
        targeted = ControlCommand(
            command.turn_rate_rad_min, command.speed_cells_min, command.sensor_mode,
            OperationMode.PROBE, self.task.target_contact_id,
        )
        self._validate(targeted, observation)
        return ControlDecision(targeted, events)

    def is_complete(self, observation: ControlObservation) -> bool:
        return observation.probe is not None and observation.probe.phase == "finished"

    def stop_task(self, reason: StopReason) -> None:
        del reason
        self._route = None

    def _plan_route(self, observation: ControlObservation, contact: ContactObservation, standoff: float) -> RouteFollower:
        path = self.navigator.plan_to_standoff(
            self._pose(observation), contact.estimated_position, standoff,
            observation.planning_obstacle_mask, self.r_min, observation.planning_map_version,
        )
        return RouteFollower(path)

    def _blocked_decision(self, observation: ControlObservation, reason: str) -> ControlDecision:
        events: tuple[ControllerEventRequest, ...] = ()
        if "blocked" not in self._reported_phases:
            self._reported_phases.add("blocked")
            events = (ControllerEventRequest("probe_blocked", {"task_id": self.task.task_id, "reason": reason}),)
        return ControlDecision(self._holding_command(observation), events)

    def _finished_decision(self, observation: ControlObservation, reason: str | None) -> ControlDecision:
        events: tuple[ControllerEventRequest, ...] = ()
        if reason in {"probe_timeout", "approach_timeout"} and not self._timeout_reported:
            self._timeout_reported = True
            events = (ControllerEventRequest("probe_timed_out", {"task_id": self.task.task_id, "reason": reason}),)
        return ControlDecision(self._holding_command(observation), events)

    def _phase_event(self, phase: str) -> tuple[ControllerEventRequest, ...]:
        if phase in self._reported_phases:
            return ()
        self._reported_phases.add(phase)
        return (ControllerEventRequest("probe_phase_changed", {"probe_id": self.task.probe_id, "phase": phase}),)

    def _holding_command(self, observation: ControlObservation) -> ControlCommand:
        command = ControlCommand(0.0, max(self.action_spec.min_speed_cells_min,
                                            min(observation.self_state.speed_cells_min,
                                                self.action_spec.max_speed_cells_min)),
                                 SensorMode.OFF, OperationMode.HOLDING)
        self._validate(command, observation)
        return command

    def _phase_and_standoff(self, phase: str) -> tuple[str, float]:
        if phase == "baseline":
            return "baseline", self._config.baseline_standoff_cells
        if phase in {"closing", "near"}:
            return "near", self._config.near_standoff_cells
        raise ValueError(f"unsupported probe phase {phase}")

    @staticmethod
    def _pose(observation: ControlObservation) -> tuple[float, float, float]:
        return (*map(float, observation.self_state.position), float(observation.self_state.heading_rad))

    def _contact(self, contacts: Sequence[ContactObservation]) -> ContactObservation | None:
        return next((contact for contact in contacts if contact.contact_id == self.task.target_contact_id), None)

    def _matches(self, probe, contact: ContactObservation) -> bool:
        return probe.probe_id == self.task.probe_id and probe.contact_id == contact.contact_id

    @staticmethod
    def _at_standoff(observation: ControlObservation, contact: ContactObservation, standoff: float) -> bool:
        return math.dist(observation.self_state.position, contact.estimated_position) <= standoff + 0.15

    @staticmethod
    def _validate(command: ControlCommand, observation: ControlObservation) -> None:
        if command.operation_mode not in observation.action_mask.allowed_operation_modes:
            raise ValueError("operation mode is absent from action mask")
        if command.sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise ValueError("sensor mode is absent from action mask")


__all__ = ["ProbeController"]

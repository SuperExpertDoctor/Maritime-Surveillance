"""Observation-only progressive contact investigation control."""

from __future__ import annotations

import math
from collections.abc import Sequence

from src.control.common.contracts import (
    ActionSpec, ContactObservation, ControlCommand, ControlDecision,
    ControlObservation, ControlRouteSnapshot, ControlTask,
    ControllerEventRequest, ObservationSpec, OperationMode, SensorMode,
    StopReason,
)
from src.control.common.safety import ProbeValidationError
from src.control.heuristic.base import (
    HeuristicControllerBase,
    RouteFollower,
    next_route_index,
)
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
        self._route_contact_key: tuple[tuple[float, float], tuple[float, float], float] | None = None
        self._planning_map_version: int | None = None
        self._route_revision = 0
        self._route_status = "pending"
        self._stopped = False
        self._reported_phases: set[str] = set()
        self._blocked = False
        self._timeout_reported = False
        self._orbit_entry_active = False
        self._guidance_phase: str | None = None

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
            raise ProbeValidationError(
                "probe tasks require PROBE, target_contact_id, and probe_id",
            )
        self.task = task
        self._route = None
        self._route_phase = None
        self._route_contact_key = None
        self._planning_map_version = None
        self._route_revision = 0
        self._route_status = "pending"
        self._stopped = False
        self._reported_phases.clear()
        self._blocked = False
        self._timeout_reported = False
        self._orbit_entry_active = False
        self._guidance_phase = None
        contact = self._contact(observation.contacts)
        if contact is None:
            self._route_status = "unavailable"
            return
        try:
            self._set_route(
                self._plan_route(
                    observation,
                    contact,
                    self._config.baseline_standoff_cells,
                ),
                observation.planning_map_version,
            )
            self._route_phase = "baseline"
            self._route_contact_key = self._contact_key(observation, contact)
        except (PathNotFoundError, ValueError):
            self._route_status = "unavailable"

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self.task is None:
            raise RuntimeError("start_task must be called before act")
        probe = observation.probe
        contact = self._contact(observation.contacts)
        if probe is None or contact is None or not self._matches(probe, contact):
            if contact is None:
                self._route_status = "unavailable"
            return ControlDecision(self._holding_command(observation))
        if probe.phase == "finished":
            self._route_status = "cleared"
            return self._finished_decision(observation, probe.completed_reason)
        if probe.phase == "awaiting_assessment":
            self._route_phase = probe.phase
            self._route_status = "guidance_only"
            return ControlDecision(self._awaiting_assessment_command(observation))
        if self._blocked:
            self._route_status = "unavailable"
            return ControlDecision(self._holding_command(observation))

        phase, standoff = self._phase_and_standoff(probe.phase)
        contact_key = self._contact_key(observation, contact)
        try:
            if (
                self._route_phase != probe.phase
                or self._route is None
                or (
                    self._route_contact_key != contact_key
                    and not self._orbit_entry_active
                    and self._route_contact_key is not None
                    and math.dist(
                        self._route_contact_key[0], contact_key[0]
                    ) > max(
                        0.5,
                        observation.self_state.speed_cells_min
                        * observation.dt_min
                        * 2.0,
                    )
                )
            ):
                self._set_route(
                    self._plan_route(observation, contact, standoff),
                    observation.planning_map_version,
                )
                self._route_phase = probe.phase
                self._route_contact_key = contact_key
                self._orbit_entry_active = False
                self._guidance_phase = None
            if self._guidance_phase == phase:
                command = self._guidance_command(observation, contact, standoff)
                events = self._phase_event(phase)
                return ControlDecision(command, events)
            if self._at_standoff(observation, contact, standoff):
                if not self._orbit_entry_active:
                    target_position = self._predicted_contact_position(observation, contact)
                    entry = plan_contact_orbit_entry(
                        self.tracker, self._pose(observation), target_position, standoff
                    )
                    self._set_route(entry, observation.planning_map_version)
                    self._route_phase = probe.phase
                    self._route_contact_key = contact_key
                    self._orbit_entry_active = True
                command = self._route.next_command(
                    observation, self.action_spec, SensorMode.EO, OperationMode.PROBE
                )
                if self._route.is_complete:
                    self._guidance_phase = phase
                    command = self._guidance_command(observation, contact, standoff)
                events = self._phase_event(phase)
            else:
                command = self._route.next_command(
                    observation, self.action_spec, SensorMode.OFF, OperationMode.PROBE
                )
                events = ()
        except (PathNotFoundError, ValueError) as exc:
            self._blocked = True
            self._route = None
            self._route_status = "unavailable"
            return self._blocked_decision(observation, str(exc))
        targeted = ControlCommand(
            command.turn_rate_rad_min, command.speed_cells_min, command.sensor_mode,
            OperationMode.PROBE, self.task.target_contact_id,
        )
        self._validate(targeted, observation)
        return ControlDecision(targeted, events)

    def _guidance_command(
        self,
        observation: ControlObservation,
        contact: ContactObservation,
        standoff: float,
    ) -> ControlCommand:
        target_position = self._predicted_contact_position(observation, contact)
        # Aim inside the near evidence band so target motion and fixed-wing
        # turn-rate quantisation do not push otherwise valid samples outside
        # the strict evidence gate.
        guidance_standoff = max(self.r_min, standoff - 0.1)
        speed = max(
            self.action_spec.min_speed_cells_min,
            min(observation.self_state.speed_cells_min, self.action_spec.max_speed_cells_min),
        )
        turn_rate, speed = self.tracker.compute_guidance(
            self._pose(observation), target_position, guidance_standoff, speed,
        )
        self._route_status = "guidance_only"
        command = ControlCommand(
            float(turn_rate), float(speed), SensorMode.EO,
            OperationMode.PROBE, self.task.target_contact_id,
        )
        self._validate(command, observation)
        return command

    def is_complete(self, observation: ControlObservation) -> bool:
        return observation.probe is not None and observation.probe.phase == "finished"

    def stop_task(self, reason: StopReason) -> None:
        del reason
        self._stopped = True
        self._route = None
        self._route_status = "cleared"

    def _plan_route(self, observation: ControlObservation, contact: ContactObservation, standoff: float) -> RouteFollower:
        target_position = self._predicted_contact_position(observation, contact)
        path = self.navigator.plan_to_standoff(
            self._pose(observation), target_position, standoff,
            observation.planning_obstacle_mask, self.r_min, observation.planning_map_version,
        )
        return RouteFollower(path)

    def _set_route(
        self,
        follower: RouteFollower | Sequence[Sequence[float]],
        planning_map_version: int,
    ) -> None:
        self._route = (
            follower if isinstance(follower, RouteFollower) else RouteFollower(follower)
        )
        self._planning_map_version = planning_map_version
        self._route_revision += 1
        self._route_status = "ready"

    def _contact_key(
        self, observation: ControlObservation, contact: ContactObservation
    ) -> tuple[tuple[float, float], tuple[float, float], float]:
        return (
            tuple(map(float, contact.estimated_position)),
            tuple(map(float, contact.estimated_velocity)),
            self._prediction_horizon(observation),
        )

    def _predicted_contact_position(
        self, observation: ControlObservation, contact: ContactObservation
    ) -> tuple[float, float]:
        position = tuple(map(float, contact.estimated_position))
        velocity = tuple(map(float, contact.estimated_velocity))
        if len(position) != 2 or len(velocity) != 2:
            raise ProbeValidationError(
                "contact position and velocity must contain two values",
            )
        if not all(math.isfinite(value) for value in (*position, *velocity)):
            raise ProbeValidationError(
                "contact position and velocity must be finite",
            )
        horizon = self._prediction_horizon(observation)
        return tuple(position[index] + velocity[index] * horizon for index in range(2))

    def _prediction_horizon(self, observation: ControlObservation) -> float:
        dt_min = float(observation.dt_min)
        if not math.isfinite(dt_min) or dt_min <= 0.0:
            raise ProbeValidationError(
                "observation dt_min must be finite and positive",
            )
        return min(dt_min, float(self._config.max_sample_gap_min))

    def _blocked_decision(self, observation: ControlObservation, reason: str) -> ControlDecision:
        self._route_status = "unavailable"
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

    def _awaiting_assessment_command(
        self, observation: ControlObservation
    ) -> ControlCommand:
        command = ControlCommand(
            0.0,
            max(
                self.action_spec.min_speed_cells_min,
                min(
                    observation.self_state.speed_cells_min,
                    self.action_spec.max_speed_cells_min,
                ),
            ),
            SensorMode.OFF,
            OperationMode.PROBE,
            self.task.target_contact_id,
        )
        self._validate(command, observation)
        return command

    def _phase_and_standoff(self, phase: str) -> tuple[str, float]:
        if phase == "baseline":
            return "baseline", self._config.baseline_standoff_cells
        if phase in {"closing", "near"}:
            return "near", self._config.near_standoff_cells
        raise ProbeValidationError(f"unsupported probe phase {phase}")

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
            raise ProbeValidationError("operation mode is absent from action mask")
        if command.sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise ProbeValidationError("sensor mode is absent from action mask")

    def route_snapshot(self) -> ControlRouteSnapshot:
        task = self.task
        route = (
            self._route.poses
            if self._route is not None
            and self._route_status == "ready"
            and not self._stopped
            else ()
        )
        follower = self._route if route else None
        status = self._route_status
        if task is None:
            status = "unavailable"
        elif self._stopped:
            status = "cleared"
        return ControlRouteSnapshot(
            task.task_id if task is not None else None,
            OperationMode.PROBE.value,
            self._route_phase or "pending",
            task.target_contact_id if task is not None else None,
            route,
            next_route_index(follower),
            self._route_revision,
            self._planning_map_version if route else None,
            status,
        )


__all__ = ["ProbeController", "ProbeValidationError"]

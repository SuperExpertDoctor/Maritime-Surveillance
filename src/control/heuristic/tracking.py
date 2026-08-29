"""Observation-only heuristic controller for fixed-wing target tracking."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
import math

from src.control.common.contracts import (
    ActionSpec,
    ContactObservation,
    ControlCommand,
    ControlDecision,
    ControlObservation,
    ControlTask,
    ControllerEventRequest,
    HazardObservation,
    ObservationSpec,
    OperationMode,
    Pose,
    SensorMode,
    StopReason,
)
from src.control.common.safety import InvalidControlCommand, SafetyEnvelope
from src.control.heuristic.base import HeuristicControllerBase, RouteFollower
from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.utils.storm_avoider import StormAvoider, ThreatLevel
from src.utils.track_orbit import LGVFTracker


class TrackingPhase(str, Enum):
    CREATED = "created"
    APPROACH_ASTAR = "approach_astar"
    ORBIT_ENTRY = "orbit_entry"
    TRACKING = "tracking"
    COMPLETED = "completed"
    LOST = "lost"


class TrackingRouteError(RuntimeError):
    """Raised internally when no observation-safe tracking route exists."""


@dataclass(frozen=True)
class _StormGeometry:
    """Immutable square geometry accepted by ``StormAvoider`` and LGVF."""

    center: tuple[float, float]
    half_extent: float

    def contains(
        self, point: Sequence[float], safety_margin: float = 0.0
    ) -> bool:
        margin = max(0.0, float(safety_margin))
        half_extent = self.half_extent + margin
        return (
            abs(float(point[0]) - self.center[0]) <= half_extent
            and abs(float(point[1]) - self.center[1]) <= half_extent
        )

    def distance_to_boundary(self, point: Sequence[float]) -> float:
        delta_col = max(
            abs(float(point[0]) - self.center[0]) - self.half_extent, 0.0
        )
        delta_row = max(
            abs(float(point[1]) - self.center[1]) - self.half_extent, 0.0
        )
        return math.hypot(delta_col, delta_row)


def orbit_waypoint_positions(
    target_position: Sequence[float],
    standoff_cells: float = 3.0,
    num_points: int = 8,
) -> tuple[tuple[float, float], ...]:
    """Return evenly spaced positions on a circular standoff orbit."""
    if len(target_position) < 2:
        raise ValueError("target_position must contain column and row")
    if not math.isfinite(standoff_cells) or standoff_cells <= 0.0:
        raise ValueError("standoff_cells must be a finite positive number")
    if num_points < 1:
        raise ValueError("num_points must be positive")
    center_col, center_row = map(float, target_position[:2])
    return tuple(
        (
            center_col + standoff_cells * math.cos(2.0 * math.pi * index / num_points),
            center_row + standoff_cells * math.sin(2.0 * math.pi * index / num_points),
        )
        for index in range(num_points)
    )


def shifted_orbit_positions(
    old_waypoints: Sequence[Sequence[float]],
    target_displacement: Sequence[float],
) -> tuple[tuple[float, float], ...]:
    """Translate orbit positions by the supplied target displacement."""
    if len(target_displacement) < 2:
        raise ValueError("target_displacement must contain column and row")
    delta_col, delta_row = map(float, target_displacement[:2])
    return tuple(
        (float(point[0]) + delta_col, float(point[1]) + delta_row)
        for point in old_waypoints
    )


class TrackingController(HeuristicControllerBase):
    """Approach and orbit a contact estimate without access to world truth."""

    def __init__(
        self,
        *,
        observation_spec: ObservationSpec,
        action_spec: ActionSpec,
        navigator: AStarNavigator | None = None,
        tracker: LGVFTracker | None = None,
        storm_avoider: StormAvoider | None = None,
        eo_range_cells: float = 2.5,
        standoff_radius_cells: float = 1.8,
        r_min: float = 1.0,
        nominal_speed_cells_min: float | None = None,
        storm_safety_margin_cells: float = 1.0,
    ) -> None:
        values = (
            eo_range_cells,
            standoff_radius_cells,
            r_min,
            storm_safety_margin_cells,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError("tracking distances must be finite and positive")
        speed = (
            action_spec.max_speed_cells_min
            if nominal_speed_cells_min is None
            else nominal_speed_cells_min
        )
        if not math.isfinite(speed) or speed <= 0.0:
            raise ValueError("nominal speed must be finite and positive")
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self.navigator = navigator or AStarNavigator()
        self.tracker = tracker or LGVFTracker(R_min=r_min)
        self.storm_avoider = storm_avoider or StormAvoider(
            safety_margin_cells=storm_safety_margin_cells,
            eo_detection_range_cells=eo_range_cells,
        )
        self.eo_range_cells = float(eo_range_cells)
        self.standoff_radius_cells = float(standoff_radius_cells)
        self.r_min = float(r_min)
        self.nominal_speed_cells_min = min(
            max(float(speed), action_spec.min_speed_cells_min),
            action_spec.max_speed_cells_min,
        )
        self.storm_safety_margin_cells = float(storm_safety_margin_cells)
        self.phase = TrackingPhase.CREATED
        self.task: ControlTask | None = None
        self.target_position: tuple[float, float] | None = None
        self.target_observed_at_min = -math.inf
        self.route: tuple[Pose, ...] = ()
        self.follower: RouteFollower | None = None
        self.planning_map_version: int | None = None
        self.avoidance_route: tuple[Pose, ...] = ()
        self._avoidance_follower: RouteFollower | None = None
        self._avoidance_planning_map_version: int | None = None
        self._failure_event_emitted = False

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    @property
    def operation_mode(self) -> OperationMode:
        return OperationMode.TRACK

    def start_task(self, task: ControlTask, observation: ControlObservation) -> None:
        if task.task_type is not OperationMode.TRACK or not task.target_contact_id:
            raise ValueError("tracking tasks require TRACK and target_contact_id")
        contact = self._resolve_contact(task.target_contact_id, observation.contacts)
        if contact is None:
            raise ValueError(
                f"target contact is absent from observation: {task.target_contact_id}"
            )
        self.task = task
        self.target_position = tuple(map(float, contact.estimated_position))
        self.target_observed_at_min = float(contact.observed_at_min)
        self.phase = TrackingPhase.CREATED
        self.route = ()
        self.follower = None
        self.planning_map_version = None
        self.avoidance_route = ()
        self._avoidance_follower = None
        self._avoidance_planning_map_version = None
        self._failure_event_emitted = False

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self.task is None or self.target_position is None:
            raise RuntimeError("start_task must be called before act")
        if self.phase is TrackingPhase.LOST:
            return self._failure_decision(observation, "tracking route unavailable")
        self._refresh_contact(observation)
        try:
            self._refresh_invalidated_route(observation)
            while True:
                if self.phase is TrackingPhase.CREATED:
                    if self._within_eo_range(observation):
                        self._plan_orbit_entry(observation)
                    else:
                        self._plan_approach(observation)
                if self.phase is TrackingPhase.APPROACH_ASTAR:
                    if self._within_eo_range(observation):
                        self._plan_orbit_entry(observation)
                        continue
                    assert self.follower is not None
                    command = self.follower.next_command(
                        observation,
                        self.action_spec,
                        SensorMode.OFF,
                        OperationMode.TRACK,
                    )
                    return ControlDecision(self._with_target(command, observation))
                if self.phase is TrackingPhase.ORBIT_ENTRY:
                    assert self.follower is not None
                    command = self.follower.next_command(
                        observation,
                        self.action_spec,
                        SensorMode.EO,
                        OperationMode.TRACK,
                    )
                    if self.follower.is_complete:
                        self.phase = TrackingPhase.TRACKING
                        continue
                    return ControlDecision(self._with_target(command, observation))
                if self.phase is TrackingPhase.TRACKING:
                    return ControlDecision(self._tracking_command(observation))
                raise RuntimeError(f"tracking controller cannot act in {self.phase.value}")
        except (PathNotFoundError, TrackingRouteError) as exc:
            self.phase = TrackingPhase.LOST
            return self._failure_decision(observation, str(exc))

    def is_complete(self, observation: ControlObservation) -> bool:
        del observation
        return self.phase in (TrackingPhase.COMPLETED, TrackingPhase.LOST)

    def stop_task(self, reason: StopReason) -> None:
        self.phase = (
            TrackingPhase.COMPLETED
            if reason is StopReason.COMPLETED
            else TrackingPhase.LOST
        )

    def _plan_approach(self, observation: ControlObservation) -> None:
        assert self.target_position is not None
        start = self._current_pose(observation)
        path = self.navigator.plan_to_standoff(
            start,
            self.target_position,
            self.eo_range_cells,
            observation.planning_obstacle_mask,
            self.r_min,
            observation.planning_map_version,
        )
        self._set_route(path, observation, "A* standoff")
        self.phase = TrackingPhase.APPROACH_ASTAR

    def _plan_orbit_entry(self, observation: ControlObservation) -> None:
        assert self.target_position is not None
        entry = self.tracker.plan_entry(
            self._current_pose(observation),
            self.target_position,
            self.standoff_radius_cells,
        )
        self._set_route(entry.waypoints, observation, "LGVF orbit entry")
        self.phase = TrackingPhase.ORBIT_ENTRY

    def _tracking_command(self, observation: ControlObservation) -> ControlCommand:
        assert self.target_position is not None
        pose = self._current_pose(observation)
        storms = self._storm_geometry(observation.hazards)
        threat = self.storm_avoider.detect_threat(
            pose,
            self.target_position,
            storms,
            observation.self_state.speed_cells_min,
            observation.dt_min,
            self.standoff_radius_cells,
        )
        if threat.level is ThreatLevel.LEVEL_3:
            raise TrackingRouteError("target standoff is obscured by a storm")
        if threat.level is ThreatLevel.LEVEL_2:
            self._refresh_avoidance_route(observation, pose)
            if self._avoidance_follower is None or self._avoidance_follower.is_complete:
                path = self.storm_avoider.plan_avoidance(
                    pose,
                    self.target_position,
                    storms,
                    self.r_min,
                    standoff_radius=self.standoff_radius_cells,
                )
                if not path:
                    raise TrackingRouteError("no safe storm-avoidance route")
                route = self._normalise_route(path)
                if self._route_blocked(route, observation.planning_obstacle_mask):
                    raise TrackingRouteError("storm-avoidance route intersects planning mask")
                self.avoidance_route = route
                self._avoidance_follower = RouteFollower(route)
                self._avoidance_planning_map_version = observation.planning_map_version
            command = self._avoidance_follower.next_command(
                observation,
                self.action_spec,
                SensorMode.EO,
                OperationMode.TRACK,
            )
            return self._with_target(command, observation)
        self._avoidance_follower = None
        self._avoidance_planning_map_version = None
        turn_rate, speed = self.tracker.compute_guidance(
            pose,
            self.target_position,
            self.standoff_radius_cells,
            self.nominal_speed_cells_min,
            storm_zones=storms,
            storm_safety_margin=self.storm_safety_margin_cells,
        )
        command = ControlCommand(
            float(turn_rate),
            float(speed),
            SensorMode.EO,
            OperationMode.TRACK,
            self.task.target_contact_id,
        )
        self._validate_command_modes(command, observation)
        return command

    def _set_route(
        self,
        path: Sequence[Sequence[float]],
        observation: ControlObservation,
        label: str,
    ) -> None:
        route = self._normalise_route(path)
        if self._route_blocked(route, observation.planning_obstacle_mask):
            raise TrackingRouteError(f"{label} route intersects planning mask")
        self.route = route
        self.follower = RouteFollower(route)
        self.planning_map_version = observation.planning_map_version

    def _refresh_contact(self, observation: ControlObservation) -> None:
        assert self.task is not None
        contact = self._resolve_contact(
            self.task.target_contact_id, observation.contacts
        )
        if contact is None or contact.observed_at_min <= self.target_observed_at_min:
            return
        self.target_position = tuple(map(float, contact.estimated_position))
        self.target_observed_at_min = float(contact.observed_at_min)
        self._avoidance_follower = None
        self.avoidance_route = ()
        self._avoidance_planning_map_version = None
        if self.phase in (
            TrackingPhase.CREATED,
            TrackingPhase.APPROACH_ASTAR,
            TrackingPhase.ORBIT_ENTRY,
        ):
            self.route = ()
            self.follower = None
            self.planning_map_version = None
            self.phase = TrackingPhase.CREATED

    def _refresh_invalidated_route(self, observation: ControlObservation) -> None:
        if self.phase not in (
            TrackingPhase.APPROACH_ASTAR,
            TrackingPhase.ORBIT_ENTRY,
        ):
            return
        if observation.planning_map_version == self.planning_map_version:
            return
        assert self.follower is not None
        current_pose = self._current_pose(observation)
        unflown = self.route[self.follower.index + 1 :]
        if not self._route_blocked(
            (current_pose, *unflown), observation.planning_obstacle_mask
        ):
            self.planning_map_version = observation.planning_map_version
            return
        self.route = ()
        self.follower = None
        self.planning_map_version = None
        self.phase = TrackingPhase.CREATED

    def _refresh_avoidance_route(
        self, observation: ControlObservation, current_pose: Pose
    ) -> None:
        if self._avoidance_follower is None:
            return
        if observation.planning_map_version == self._avoidance_planning_map_version:
            return
        unflown = self.avoidance_route[self._avoidance_follower.index + 1 :]
        if not self._route_blocked(
            (current_pose, *unflown), observation.planning_obstacle_mask
        ):
            self._avoidance_planning_map_version = observation.planning_map_version
            return
        self.avoidance_route = ()
        self._avoidance_follower = None
        self._avoidance_planning_map_version = None

    def _within_eo_range(self, observation: ControlObservation) -> bool:
        assert self.target_position is not None
        return (
            math.dist(observation.self_state.position, self.target_position)
            <= self.eo_range_cells
        )

    def _with_target(
        self, command: ControlCommand, observation: ControlObservation
    ) -> ControlCommand:
        assert self.task is not None
        targeted = ControlCommand(
            command.turn_rate_rad_min,
            command.speed_cells_min,
            command.sensor_mode,
            command.operation_mode,
            self.task.target_contact_id,
        )
        self._validate_command_modes(targeted, observation)
        return targeted

    def _failure_decision(
        self, observation: ControlObservation, reason: str
    ) -> ControlDecision:
        assert self.task is not None
        speed = min(
            max(
                observation.self_state.speed_cells_min,
                self.action_spec.min_speed_cells_min,
            ),
            self.action_spec.max_speed_cells_min,
        )
        sensor_mode = next(
            (
                mode
                for mode in (SensorMode.OFF, SensorMode.EO, SensorMode.SAR)
                if mode in observation.action_mask.allowed_sensor_modes
            ),
            None,
        )
        operation_mode = next(
            (
                mode
                for mode in (
                    OperationMode.HOLDING,
                    OperationMode.IDLE,
                    OperationMode.RETURN,
                    OperationMode.TRANSIT,
                    OperationMode.COVERAGE,
                    OperationMode.TRACK,
                )
                if mode in observation.action_mask.allowed_operation_modes
            ),
            None,
        )
        if sensor_mode is None or operation_mode is None:
            raise InvalidControlCommand("action mask contains no legal failure command")
        command = ControlCommand(
            0.0,
            speed,
            sensor_mode,
            operation_mode,
        )
        self._validate_command_modes(command, observation)
        if self._failure_event_emitted:
            return ControlDecision(command)
        self._failure_event_emitted = True
        return ControlDecision(
            command,
            (
                ControllerEventRequest(
                    "task_failed",
                    {"task_id": self.task.task_id, "reason": reason},
                ),
            ),
        )

    @staticmethod
    def _resolve_contact(
        contact_id: str | None, contacts: Sequence[ContactObservation]
    ) -> ContactObservation | None:
        return next(
            (contact for contact in contacts if contact.contact_id == contact_id),
            None,
        )

    @staticmethod
    def _storm_geometry(
        hazards: Sequence[HazardObservation],
    ) -> tuple[_StormGeometry, ...]:
        return tuple(
            _StormGeometry(
                tuple(map(float, hazard.center)),
                float(hazard.half_extent_cells),
            )
            for hazard in hazards
            if hazard.hazard_type.lower() in {"storm", "thunderstorm"}
        )

    @staticmethod
    def _current_pose(observation: ControlObservation) -> Pose:
        return (
            *tuple(map(float, observation.self_state.position)),
            float(observation.self_state.heading_rad),
        )

    @staticmethod
    def _normalise_route(path: Sequence[Sequence[float]]) -> tuple[Pose, ...]:
        route = tuple(tuple(map(float, pose)) for pose in path)
        if not route or any(len(pose) != 3 for pose in route):
            raise TrackingRouteError("tracking route must contain pose triples")
        if not all(math.isfinite(value) for pose in route for value in pose):
            raise TrackingRouteError("tracking route contains non-finite values")
        return route

    @staticmethod
    def _route_blocked(route: Sequence[Pose], obstacle_mask: object) -> bool:
        first = route[0]
        if SafetyEnvelope._point_blocked(*first[:2], obstacle_mask):
            return True
        return any(
            SafetyEnvelope._cell_blocked(*cell, obstacle_mask)
            for start, end in zip(route, route[1:])
            for cell in SafetyEnvelope._traversed_cells(*start[:2], *end[:2])
        )

    @staticmethod
    def _validate_command_modes(
        command: ControlCommand, observation: ControlObservation
    ) -> None:
        if command.operation_mode not in observation.action_mask.allowed_operation_modes:
            raise InvalidControlCommand("operation mode is absent from action mask")
        if command.sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise InvalidControlCommand("sensor mode is absent from action mask")
        if (
            command.target_contact_id is not None
            and command.target_contact_id
            not in observation.action_mask.target_contact_ids
        ):
            raise InvalidControlCommand("target contact is absent from action mask")


__all__ = [
    "TrackingController",
    "TrackingPhase",
    "TrackingRouteError",
    "orbit_waypoint_positions",
    "shifted_orbit_positions",
]

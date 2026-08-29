"""Observation-only recovery planning, return, and system holding control."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import math

from src.control.common.contracts import (
    ActionSpec,
    BaseObservation,
    ControlCommand,
    ControlDecision,
    ControlObservation,
    ControlOwner,
    ControlTask,
    ObservationSpec,
    OperationMode,
    Pose,
    RecoveryPlan,
    SensorMode,
    StopReason,
)
from src.control.common.safety import InvalidControlCommand, SafetyEnvelope
from src.control.heuristic.base import HeuristicControllerBase, RouteFollower
from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.utils.track_orbit import LGVFTracker


@dataclass(frozen=True)
class RecoveryCandidate:
    base: BaseObservation
    path: tuple[Pose, ...]
    path_length_cells: float
    reserve_cells: float
    planning_map_version: int


class NoSafeRecoveryPath(RuntimeError):
    """Raised when the reserved recovery base has no currently safe route."""

    def __init__(
        self, base_id: str, planning_map_version: int, reason: str
    ) -> None:
        self.base_id = base_id
        self.planning_map_version = planning_map_version
        self.reason = reason
        super().__init__(
            "no safe recovery path to reserved base "
            f"{base_id}: planning_map_version={planning_map_version}, {reason}"
        )


def path_length_cells(path: Sequence[Sequence[float]]) -> float:
    """Measure a sampled path using its actual consecutive positions."""
    return sum(
        math.dist(start[:2], end[:2]) for start, end in zip(path, path[1:])
    )


def legacy_return_endpoints(
    current: Sequence[float], base_position: Sequence[float]
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Retain the endpoint-only shape of the pre-controller return helper."""
    if len(current) < 2 or len(base_position) < 2:
        raise ValueError("return endpoints must contain column and row")
    return (
        (float(current[0]), float(current[1])),
        (float(base_position[0]), float(base_position[1])),
    )


class RecoveryPlanner:
    """Evaluate every available base using checked Hybrid A* paths."""

    def __init__(self, navigator: AStarNavigator | None = None) -> None:
        self.navigator = navigator or AStarNavigator()

    def evaluate(
        self,
        start_pose: Pose,
        remaining_range_cells: float,
        bases: Sequence[BaseObservation],
        planning_obstacle_mask: object,
        planning_map_version: int,
        r_min: float,
        reserve_cells: float,
    ) -> tuple[RecoveryCandidate, ...]:
        if not math.isfinite(remaining_range_cells) or remaining_range_cells < 0.0:
            raise ValueError("remaining_range_cells must be finite and non-negative")
        if not math.isfinite(r_min) or r_min <= 0.0:
            raise ValueError("r_min must be finite and positive")
        if not math.isfinite(reserve_cells) or reserve_cells < 0.0:
            raise ValueError("reserve_cells must be finite and non-negative")
        candidates = []
        for base in bases:
            if base.reserved_load >= base.capacity:
                continue
            try:
                planned = self.navigator.plan_grid(
                    start_pose,
                    {tuple(map(float, base.position))},
                    planning_obstacle_mask,
                    r_min,
                    planning_map_version,
                )
            except PathNotFoundError:
                continue
            try:
                path = _normalise_route(planned)
            except ValueError:
                continue
            if path[-1][:2] != tuple(map(float, base.position)):
                continue
            if _route_blocked(path, planning_obstacle_mask):
                continue
            actual_length = path_length_cells(path)
            if actual_length + reserve_cells > remaining_range_cells:
                continue
            candidates.append(
                RecoveryCandidate(
                    base,
                    path,
                    actual_length,
                    float(reserve_cells),
                    planning_map_version,
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda candidate: (
                    candidate.path_length_cells,
                    candidate.base.base_id,
                ),
            )
        )


class ReturnToBaseController(HeuristicControllerBase):
    """Follow a reserved recovery plan and replan only to its base."""

    def __init__(
        self,
        *,
        observation_spec: ObservationSpec,
        action_spec: ActionSpec,
        navigator: AStarNavigator | None = None,
        release_reservation: Callable[[str], None] | None = None,
        r_min: float = 1.0,
    ) -> None:
        if not math.isfinite(r_min) or r_min <= 0.0:
            raise ValueError("r_min must be finite and positive")
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self.navigator = navigator or AStarNavigator()
        self._release_reservation = release_reservation or (lambda _: None)
        self.r_min = float(r_min)
        self.task: ControlTask | None = None
        self.recovery_plan: RecoveryPlan | None = None
        self.route: tuple[Pose, ...] = ()
        self.follower: RouteFollower | None = None
        self.planning_map_version: int | None = None
        self.reservation_id: str | None = None
        self._arrived = False
        self._reservation_released = False
        self._failure: NoSafeRecoveryPath | None = None

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    @property
    def operation_mode(self) -> OperationMode:
        return OperationMode.RETURN

    @property
    def lease_owner(self) -> ControlOwner:
        return ControlOwner.SYSTEM

    @property
    def control_owner(self) -> ControlOwner:
        return self.lease_owner

    def start_task(self, task: ControlTask, observation: ControlObservation) -> None:
        if task.task_type is not OperationMode.RETURN:
            raise ValueError("ReturnToBaseController requires a RETURN task")
        if task.recovery_plan is None:
            raise ValueError("RETURN task requires a RecoveryPlan")
        plan = task.recovery_plan
        route = _normalise_route(plan.path)
        if route[-1][:2] != tuple(map(float, plan.base_position)):
            raise ValueError("RecoveryPlan path must end at base_position")
        if not all(
            math.isfinite(value) and value >= 0.0
            for value in (plan.path_length_cells, plan.reserve_cells)
        ):
            raise ValueError("RecoveryPlan lengths must be finite and non-negative")
        actual_length = path_length_cells(route)
        if not math.isclose(
            actual_length, plan.path_length_cells, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise ValueError("RecoveryPlan path_length_cells does not match path")
        if (
            plan.planning_map_version == observation.planning_map_version
            and _route_blocked(route, observation.planning_obstacle_mask)
        ):
            raise NoSafeRecoveryPath(
                plan.base_id,
                observation.planning_map_version,
                "validated route intersects planning mask",
            )
        self.task = task
        self.recovery_plan = plan
        self.route = route
        self.follower = RouteFollower(route)
        self.planning_map_version = plan.planning_map_version
        self.reservation_id = plan.reservation_id
        self._arrived = False
        self._reservation_released = False
        self._failure = None

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self.recovery_plan is None:
            raise RuntimeError("start_task must be called before act")
        if self._failure is not None:
            raise self._failure
        if self.follower is None:
            raise RuntimeError("recovery route is unavailable")
        self._refresh_route(observation)
        command = self.follower.next_command(
            observation,
            self.action_spec,
            SensorMode.OFF,
            OperationMode.RETURN,
        )
        self._validate_command_modes(command, observation)
        self.is_complete(observation)
        return ControlDecision(command)

    def is_complete(self, observation: ControlObservation) -> bool:
        if self.recovery_plan is None:
            return False
        arrival_radius = max(
            observation.self_state.speed_cells_min * observation.dt_min, 0.05
        )
        self._arrived = (
            math.dist(
                observation.self_state.position,
                self.recovery_plan.base_position,
            )
            <= arrival_radius
        )
        return self._arrived

    def stop_task(self, reason: StopReason) -> None:
        del reason
        if (
            self.reservation_id is not None
            and not self._arrived
            and not self._reservation_released
        ):
            self._release_reservation(self.reservation_id)
            self._reservation_released = True

    def _refresh_route(self, observation: ControlObservation) -> None:
        if observation.planning_map_version == self.planning_map_version:
            return
        assert self.recovery_plan is not None
        assert self.follower is not None
        current_pose = _current_pose(observation)
        suffix = self.route[self.follower.index + 1 :]
        route_to_validate = (current_pose, *suffix)
        if not _route_blocked(
            route_to_validate, observation.planning_obstacle_mask
        ):
            self.planning_map_version = observation.planning_map_version
            return
        try:
            planned = self.navigator.plan_grid(
                current_pose,
                {tuple(map(float, self.recovery_plan.base_position))},
                observation.planning_obstacle_mask,
                self.r_min,
                observation.planning_map_version,
            )
        except PathNotFoundError as exc:
            raise self._fail_recovery(observation, str(exc)) from exc
        try:
            route = _normalise_route(planned)
        except ValueError as exc:
            raise self._fail_recovery(observation, str(exc)) from exc
        if route[-1][:2] != tuple(map(float, self.recovery_plan.base_position)):
            raise self._fail_recovery(
                observation, "replanned route does not end at the reserved base"
            )
        if _route_blocked(route, observation.planning_obstacle_mask):
            raise self._fail_recovery(
                observation, "replanned route intersects planning mask"
            )
        actual_length = path_length_cells(route)
        if (
            actual_length + self.recovery_plan.reserve_cells
            > observation.self_state.remaining_range_cells
        ):
            raise self._fail_recovery(
                observation, "replanned route plus reserve exceeds remaining range"
            )
        self.route = route
        self.follower = RouteFollower(route)
        self.planning_map_version = observation.planning_map_version

    def _fail_recovery(
        self, observation: ControlObservation, reason: str
    ) -> NoSafeRecoveryPath:
        assert self.recovery_plan is not None
        failure = NoSafeRecoveryPath(
            self.recovery_plan.base_id,
            observation.planning_map_version,
            reason,
        )
        self.route = ()
        self.follower = None
        self._failure = failure
        return failure

    @staticmethod
    def _validate_command_modes(
        command: ControlCommand, observation: ControlObservation
    ) -> None:
        if command.operation_mode not in observation.action_mask.allowed_operation_modes:
            raise InvalidControlCommand("operation mode is absent from action mask")
        if command.sensor_mode not in observation.action_mask.allowed_sensor_modes:
            raise InvalidControlCommand("sensor mode is absent from action mask")


class SystemHoldingController(HeuristicControllerBase):
    """Emit an observation-safe fixed-wing holding orbit under SYSTEM owner."""

    def __init__(
        self,
        *,
        observation_spec: ObservationSpec,
        action_spec: ActionSpec,
        tracker: LGVFTracker | None = None,
        orbit_radius_cells: float = 2.0,
        nominal_speed_cells_min: float | None = None,
        r_min: float = 1.0,
    ) -> None:
        if not all(
            math.isfinite(value) and value > 0.0
            for value in (orbit_radius_cells, r_min)
        ):
            raise ValueError("holding radii must be finite and positive")
        speed = (
            action_spec.max_speed_cells_min
            if nominal_speed_cells_min is None
            else nominal_speed_cells_min
        )
        if not math.isfinite(speed) or speed <= 0.0:
            raise ValueError("nominal speed must be finite and positive")
        self._observation_spec = observation_spec
        self._action_spec = action_spec
        self.tracker = tracker or LGVFTracker(R_min=r_min)
        self.orbit_radius_cells = float(orbit_radius_cells)
        self.nominal_speed_cells_min = min(
            max(float(speed), action_spec.min_speed_cells_min),
            action_spec.max_speed_cells_min,
        )
        self.task: ControlTask | None = None
        self.orbit_center: tuple[float, float] | None = None
        self._safety = SafetyEnvelope(action_spec)

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    @property
    def operation_mode(self) -> OperationMode:
        return OperationMode.HOLDING

    @property
    def lease_owner(self) -> ControlOwner:
        return ControlOwner.SYSTEM

    @property
    def control_owner(self) -> ControlOwner:
        return self.lease_owner

    def start_task(self, task: ControlTask, observation: ControlObservation) -> None:
        if task.task_type is not OperationMode.HOLDING:
            raise ValueError("SystemHoldingController requires a HOLDING task")
        position = observation.self_state.position
        heading = observation.self_state.heading_rad
        self.orbit_center = (
            float(position[0]) - self.orbit_radius_cells * math.sin(heading),
            float(position[1]) + self.orbit_radius_cells * math.cos(heading),
        )
        self.task = task

    def act(self, observation: ControlObservation) -> ControlDecision:
        if self.task is None or self.orbit_center is None:
            raise RuntimeError("start_task must be called before act")
        turn_rate, speed = self.tracker.compute_guidance(
            _current_pose(observation),
            self.orbit_center,
            self.orbit_radius_cells,
            self.nominal_speed_cells_min,
        )
        requested = ControlCommand(
            float(turn_rate),
            float(speed),
            SensorMode.OFF,
            OperationMode.HOLDING,
        )
        safe = self._safety.apply(requested, observation, observation.dt_min)
        return ControlDecision(safe.applied_command)

    def is_complete(self, observation: ControlObservation) -> bool:
        del observation
        return False

    def stop_task(self, reason: StopReason) -> None:
        del reason


def _current_pose(observation: ControlObservation) -> Pose:
    return (
        *tuple(map(float, observation.self_state.position)),
        float(observation.self_state.heading_rad),
    )


def _normalise_route(path: Sequence[Sequence[float]]) -> tuple[Pose, ...]:
    route = tuple(tuple(map(float, pose)) for pose in path)
    if not route or any(len(pose) != 3 for pose in route):
        raise ValueError("recovery route must contain pose triples")
    if not all(math.isfinite(value) for pose in route for value in pose):
        raise ValueError("recovery route contains non-finite values")
    return route


def _route_blocked(route: Sequence[Pose], obstacle_mask: object) -> bool:
    if not route:
        return True
    if SafetyEnvelope._point_blocked(*route[0][:2], obstacle_mask):
        return True
    return any(
        SafetyEnvelope._cell_blocked(*cell, obstacle_mask)
        for start, end in zip(route, route[1:])
        for cell in SafetyEnvelope._traversed_cells(*start[:2], *end[:2])
    )


__all__ = [
    "NoSafeRecoveryPath",
    "RecoveryCandidate",
    "RecoveryPlanner",
    "ReturnToBaseController",
    "SystemHoldingController",
    "legacy_return_endpoints",
    "path_length_cells",
]

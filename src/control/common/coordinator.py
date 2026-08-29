"""Per-UAV observe, act, safety, and execution coordination."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
from threading import RLock
from typing import TypeVar

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    BaseObservation,
    ControlDecision,
    ControlEvent,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControlTask,
    ControllerContext,
    ControllerEventRequest,
    OperationMode,
    RecoveryPlan,
    StopReason,
)
from src.control.common.executor import ExecutionResult, UAVDynamicsExecutor
from src.control.common.factory import ControlFactory
from src.control.common.observation import ObservationProvider
from src.control.common.operation_registry import OperationRegistry
from src.control.common.ownership import ControlLease, ControlOwnership
from src.control.common.safety import (
    InvalidControlCommand,
    SafetyEnvelope,
    SafetyIntervention,
    SafetyResult,
)
from src.control.heuristic.base import HeuristicControllerBase
from src.control.heuristic.return_to_base import ReturnToBaseController
from src.control.heuristic.task_flow import EVENT_TRANSITIONS, HeuristicTaskFlow
from src.env.uav_entity import UAVEntity
from src.schedule.config_loader import ControlConfig
from src.schedule.state_manager import StateManager


@dataclass(frozen=True)
class ControlTickResult:
    lease: ControlLease
    observation: ControlObservation
    decision: ControlDecision
    safety: SafetyResult
    execution: ExecutionResult
    emitted_events: tuple[ControlEvent, ...]


class ControlCoordinatorError(RuntimeError):
    """Raised when coordinator lifecycle or routing invariants are violated."""


class StaleControlCommand(ControlCoordinatorError):
    """Raised when ownership changes while a controller is deciding."""


class EmergencyRevokeRequired(ControlCoordinatorError):
    """Raised before execution when invalid commands reach the configured bound."""

    def __init__(self, uav_id: str, invalid_streak: int) -> None:
        self.uav_id = uav_id
        self.invalid_streak = invalid_streak
        super().__init__(
            f"emergency revoke required for {uav_id}: "
            f"invalid command streak {invalid_streak}"
        )


_T = TypeVar("_T")
BaseSource = Sequence[BaseObservation | object] | Callable[
    [], Sequence[BaseObservation | object]
]


class ControlCoordinator:
    """Own the authoritative command path for every configured UAV."""

    def __init__(
        self,
        *,
        config: ControlConfig,
        state_manager: StateManager,
        ownership: ControlOwnership,
        observations: ObservationProvider,
        safety: SafetyEnvelope,
        executor: UAVDynamicsExecutor,
        factory: ControlFactory,
        operation_registry: OperationRegistry,
        configured_modes: Mapping[str, ControlMode | str],
        bases: BaseSource = (),
    ) -> None:
        if not configured_modes:
            raise ValueError("configured_modes must contain at least one UAV")
        self.config = config
        self.state_manager = state_manager
        self.ownership = ownership
        self.observations = observations
        self.safety = safety
        self.executor = executor
        self.factory = factory
        self.operation_registry = operation_registry
        self._bases = bases
        self._lock = RLock()

        self._configured_modes = {
            uav_id: ControlMode(mode) for uav_id, mode in configured_modes.items()
        }
        self._controllers: dict[str, ControllerBase] = {}
        self._pending_tasks: dict[str, ControlTask] = {}
        self._queued_events: dict[str, list[ControlEvent]] = {
            uav_id: [] for uav_id in self._configured_modes
        }
        self._invalid_streaks = {
            uav_id: 0 for uav_id in self._configured_modes
        }
        self._last_applied_commands = {
            uav_id: None for uav_id in self._configured_modes
        }
        self._operation_modes = {
            uav_id: OperationMode.IDLE for uav_id in self._configured_modes
        }
        self._last_safety_intervened = {
            uav_id: False for uav_id in self._configured_modes
        }
        self._last_tick_times: dict[str, float | None] = {
            uav_id: None for uav_id in self._configured_modes
        }
        self._episode_ids: dict[str, str] = {}
        self._sortie_numbers: dict[str, int] = {}
        self._next_event_sequence = 1
        self._task_flow = HeuristicTaskFlow(
            ownership,
            factory,
            self._controllers,
            self._pending_tasks,
            atomic=self._atomic,
        )

    def queue_event(self, event: ControlEvent) -> None:
        """Queue an external event for its UAV, or all UAVs when global."""
        if not isinstance(event, ControlEvent):
            raise TypeError("event must be a ControlEvent")
        with self._lock:
            if event.uav_id is None:
                targets = tuple(self._queued_events)
            else:
                self._require_uav(event.uav_id)
                targets = (event.uav_id,)
            for uav_id in targets:
                self._queued_events[uav_id].append(event)
            self._next_event_sequence = max(
                self._next_event_sequence, event.sequence + 1
            )

    def start_work(
        self,
        uav_id: str,
        *,
        sortie_number: int,
        current_time: float,
        dt_min: float,
        task: ControlTask | None = None,
    ) -> ControlLease:
        """Acquire the configured work owner and initialize a new sortie."""
        mode = self._mode_for(uav_id)
        self._validate_time(current_time, "current_time", allow_zero=True)
        self._validate_time(dt_min, "dt_min")
        if (
            isinstance(sortie_number, bool)
            or not isinstance(sortie_number, int)
            or sortie_number < 0
        ):
            raise ControlCoordinatorError(
                "sortie_number must be a non-negative integer"
            )
        previous_sortie = self._sortie_numbers.get(uav_id)
        if previous_sortie is not None and sortie_number <= previous_sortie:
            raise ControlCoordinatorError(
                f"new sortie for {uav_id} must be greater than {previous_sortie}"
            )
        current = self.ownership.current(uav_id)
        if current.owner is not ControlOwner.SYSTEM:
            raise ControlCoordinatorError(
                f"cannot start work for {uav_id}: current owner is "
                f"{current.owner.value}"
            )

        if mode is ControlMode.HEURISTIC:
            if task is None:
                raise ControlCoordinatorError(
                    "heuristic work requires an initial ControlTask"
                )
            controller = self.factory.create_heuristic(uav_id, task)
            owner = ControlOwner.HEURISTIC
            controller_id = self._task_controller_id(task)
        else:
            if task is not None:
                raise ControlCoordinatorError(
                    "learning work does not accept a ControlTask"
                )
            controller = self.factory.create_learning(uav_id, mode)
            owner = ControlOwner.LEARNING
            controller_id = f"{mode.value}:{uav_id}"
        self._validate_controller(controller, mode)
        episode_id = f"{uav_id}:{sortie_number}"
        context = ControllerContext(
            uav_id,
            dt_min,
            controller.observation_spec,
            controller.action_spec,
            episode_id,
            task,
        )
        controller.reset(context)

        old_controller: ControllerBase | None = None
        with self._lock:
            latest = self.ownership.current(uav_id)
            if latest != current:
                raise StaleControlCommand(
                    self._stale_message(uav_id, current, latest)
                )
            self._task_flow.clear_saved_coverage(uav_id)
            lease = self.ownership.acquire(
                uav_id, owner, controller_id, current_time
            )
            old_controller = self._controllers.get(uav_id)
            self._controllers[uav_id] = controller
            if task is None:
                self._pending_tasks.pop(uav_id, None)
            else:
                self._pending_tasks[uav_id] = task
            self._operation_modes[uav_id] = OperationMode.IDLE
            self._episode_ids[uav_id] = episode_id
            self._sortie_numbers[uav_id] = sortie_number
            self._invalid_streaks[uav_id] = 0
            self._last_applied_commands[uav_id] = None
            self._last_safety_intervened[uav_id] = False
            self._last_tick_times[uav_id] = None
        if old_controller is not None and old_controller is not controller:
            self._stop_controller(old_controller, StopReason.CANCELLED)
        return lease

    def assign_task(
        self,
        uav_id: str,
        task: ControlTask,
        *,
        current_time: float,
    ) -> ControlLease:
        """Atomically stage a heuristic task controller for the next tick."""
        mode = self._mode_for(uav_id)
        if mode is not ControlMode.HEURISTIC:
            raise ControlCoordinatorError(
                f"assign_task requires configured heuristic mode for {uav_id}"
            )
        if uav_id not in self._episode_ids:
            raise ControlCoordinatorError(
                f"cannot assign a task before work starts for {uav_id}"
            )
        self._validate_time(current_time, "current_time", allow_zero=True)
        controller = self.factory.create_heuristic(uav_id, task)
        self._validate_controller(controller, ControlMode.HEURISTIC)
        current = self.ownership.current(uav_id)
        if current.owner is ControlOwner.LEARNING:
            raise ControlCoordinatorError(
                f"cannot assign a heuristic task under LEARNING owner for {uav_id}"
            )
        if (
            current.owner is ControlOwner.SYSTEM
            and self._operation_modes[uav_id] is OperationMode.RETURN
        ):
            raise ControlCoordinatorError(
                f"cannot assign a task while {uav_id} is returning"
            )

        with self._lock:
            latest = self.ownership.current(uav_id)
            if latest != current:
                raise StaleControlCommand(
                    self._stale_message(uav_id, current, latest)
                )
            if current.owner is ControlOwner.SYSTEM:
                lease = self.ownership.acquire(
                    uav_id,
                    ControlOwner.HEURISTIC,
                    self._task_controller_id(task),
                    current_time,
                )
            else:
                lease = self.ownership.replace(
                    current,
                    ControlOwner.HEURISTIC,
                    self._task_controller_id(task),
                    current_time,
                )
            old_controller = self._controllers.get(uav_id)
            self._controllers[uav_id] = controller
            self._pending_tasks[uav_id] = task
        if old_controller is not None and old_controller is not controller:
            self._stop_controller(old_controller, StopReason.PREEMPTED)
        return lease

    def revoke_for_return(
        self,
        uav_id: str,
        recovery_plan: RecoveryPlan,
        *,
        current_time: float,
    ) -> ControlLease:
        """Replace a work lease with its already-reserved SYSTEM return plan."""
        self._require_uav(uav_id)
        self._validate_time(current_time, "current_time", allow_zero=True)
        self._validate_recovery_plan(recovery_plan)
        current = self.ownership.current(uav_id)
        if current.owner not in (ControlOwner.HEURISTIC, ControlOwner.LEARNING):
            raise ControlCoordinatorError(
                f"return revocation requires a work owner for {uav_id}"
            )
        task = ControlTask(
            recovery_plan.reservation_id,
            OperationMode.RETURN,
            recovery_plan=recovery_plan,
        )
        controller = self.factory.create_heuristic(uav_id, task)
        if not isinstance(controller, ReturnToBaseController):
            raise ControlCoordinatorError(
                "return revocation requires ReturnToBaseController"
            )

        with self._lock:
            latest = self.ownership.current(uav_id)
            if latest != current:
                raise StaleControlCommand(
                    self._stale_message(uav_id, current, latest)
                )
            self._task_flow.clear_saved_coverage(uav_id)
            lease = self.ownership.replace(
                current,
                ControlOwner.SYSTEM,
                f"return:{recovery_plan.reservation_id}",
                current_time,
            )
            old_controller = self._controllers.get(uav_id)
            self._controllers[uav_id] = controller
            self._pending_tasks[uav_id] = task
            self._operation_modes[uav_id] = OperationMode.RETURN
        if old_controller is not None and old_controller is not controller:
            self._stop_controller(old_controller, StopReason.PREEMPTED)
        return lease

    def current_lease(self, uav_id: str) -> ControlLease:
        self._require_uav(uav_id)
        return self.ownership.current(uav_id)

    def step_uav(
        self,
        uav: UAVEntity,
        *,
        current_time: float,
        dt_min: float = 1.0,
    ) -> ControlTickResult:
        """Run exactly one ordered control tick for one UAV."""
        uav_id = uav.id
        self._require_uav(uav_id)
        self._validate_time(current_time, "current_time", allow_zero=True)
        self._validate_time(dt_min, "dt_min")
        with self._lock:
            last_tick = self._last_tick_times[uav_id]
            if last_tick is not None and current_time <= last_tick:
                raise ControlCoordinatorError(
                    f"{uav_id} already stepped at {last_tick}; "
                    f"received tick {current_time}"
                )
            self._last_tick_times[uav_id] = current_time

        events = self._take_queued_events(uav_id, current_time)
        remaining_events = self._apply_heuristic_transitions(
            uav_id, events, current_time
        )

        with self._lock:
            lease = self.ownership.current(uav_id)
            try:
                controller = self._controllers[uav_id]
            except KeyError as exc:
                raise ControlCoordinatorError(
                    f"work has not started for {uav_id}"
                ) from exc
            observation = self.observations.build(
                uav,
                self.state_manager,
                events=remaining_events,
                bases=self._base_observations(),
                control_mode=self._configured_modes[uav_id],
                control_owner=lease.owner,
                operation_mode=self._operation_modes[uav_id],
                safety_intervened=self._last_safety_intervened[uav_id],
                current_time=current_time,
                dt_min=dt_min,
            )
            pending_task = self._pending_tasks.pop(uav_id, None)
            if pending_task is not None:
                if not isinstance(controller, HeuristicControllerBase):
                    raise ControlCoordinatorError(
                        "only task controllers accept ControlTask"
                    )
                context = self._controller_context(
                    uav_id, controller, pending_task, dt_min
                )
                controller.reset(context)
                controller.start_task(pending_task, observation)

        try:
            decision = controller.act(observation)
        except InvalidControlCommand as exc:
            self._raise_rejected_command(uav_id, exc)
        if not isinstance(decision, ControlDecision):
            self._raise_rejected_command(
                uav_id,
                InvalidControlCommand(
                    "controller must return a ControlDecision with a ControlCommand"
                ),
            )

        with self._lock:
            if not self.ownership.accepts(lease):
                current = self.ownership.current(uav_id)
                raise StaleControlCommand(
                    self._stale_message(uav_id, lease, current)
                )
            try:
                safety = self.safety.apply(decision.command, observation, dt_min)
            except InvalidControlCommand as exc:
                self._raise_rejected_command(uav_id, exc)
            invalid_streak = self._update_invalid_streak(
                uav_id, safety.interventions
            )
            if invalid_streak >= self.config.safety.max_invalid_commands:
                raise EmergencyRevokeRequired(uav_id, invalid_streak)
            execution = self.executor.execute(uav, safety, dt_min)
            self.operation_registry.reconcile(
                uav_id,
                self._last_applied_commands[uav_id],
                safety.applied_command,
                observation,
            )
            self._operation_modes[uav_id] = safety.applied_command.operation_mode
            self._last_applied_commands[uav_id] = safety.applied_command
            self._last_safety_intervened[uav_id] = bool(safety.interventions)
            emitted = self._queue_decision_events(
                uav_id, decision.events, current_time
            )
        return ControlTickResult(
            lease, observation, decision, safety, execution, emitted
        )

    def _take_queued_events(
        self, uav_id: str, current_time: float
    ) -> tuple[ControlEvent, ...]:
        with self._lock:
            queued = self._queued_events[uav_id]
            due = [
                event for event in queued if event.timestamp_min <= current_time
            ]
            self._queued_events[uav_id] = [
                event for event in queued if event.timestamp_min > current_time
            ]
        return tuple(
            sorted(due, key=lambda event: (event.timestamp_min, event.sequence))
        )

    def _apply_heuristic_transitions(
        self,
        uav_id: str,
        events: Sequence[ControlEvent],
        current_time: float,
    ) -> tuple[ControlEvent, ...]:
        del current_time
        if self.ownership.current(uav_id).owner is not ControlOwner.HEURISTIC:
            return tuple(events)
        remaining = []
        for event in events:
            lease = self.ownership.current(uav_id)
            if (
                lease.owner is not ControlOwner.HEURISTIC
                or event.event_type not in EVENT_TRANSITIONS
            ):
                remaining.append(event)
                continue
            transition = self._task_flow.handle(event, lease)
            if not transition.consumed:
                remaining.append(event)
                continue
            if (
                transition.task is not None
                and transition.current_lease.owner is ControlOwner.SYSTEM
            ):
                with self._lock:
                    self._operation_modes[uav_id] = transition.task.task_type
        return tuple(remaining)

    def _controller_context(
        self,
        uav_id: str,
        controller: ControllerBase,
        task: ControlTask,
        dt_min: float,
    ) -> ControllerContext:
        try:
            episode_id = self._episode_ids[uav_id]
        except KeyError as exc:
            raise ControlCoordinatorError(
                f"work has not started for {uav_id}"
            ) from exc
        return ControllerContext(
            uav_id,
            dt_min,
            controller.observation_spec,
            controller.action_spec,
            episode_id,
            task,
        )

    def _update_invalid_streak(
        self,
        uav_id: str,
        interventions: Sequence[SafetyIntervention],
    ) -> int:
        if interventions:
            self._invalid_streaks[uav_id] += 1
        else:
            self._invalid_streaks[uav_id] = 0
        return self._invalid_streaks[uav_id]

    def _raise_rejected_command(
        self, uav_id: str, error: InvalidControlCommand
    ) -> None:
        with self._lock:
            self._invalid_streaks[uav_id] += 1
            invalid_streak = self._invalid_streaks[uav_id]
        if invalid_streak >= self.config.safety.max_invalid_commands:
            raise EmergencyRevokeRequired(uav_id, invalid_streak) from error
        raise error

    def _queue_decision_events(
        self,
        uav_id: str,
        requests: Sequence[ControllerEventRequest],
        current_time: float,
    ) -> tuple[ControlEvent, ...]:
        emitted = []
        for request in requests:
            if not isinstance(request, ControllerEventRequest):
                raise ControlCoordinatorError(
                    "controller events must be ControllerEventRequest values"
                )
            event = ControlEvent(
                self._next_event_sequence,
                current_time,
                request.event_type,
                "controller",
                uav_id,
                request.payload,
            )
            self._next_event_sequence += 1
            self._queued_events[uav_id].append(event)
            emitted.append(event)
        return tuple(emitted)

    def _base_observations(self) -> tuple[BaseObservation, ...]:
        source = self._bases() if callable(self._bases) else self._bases
        observations = []
        for base in source:
            if isinstance(base, BaseObservation):
                observations.append(base)
                continue
            position = getattr(base, "position", None)
            if position is None:
                raise ControlCoordinatorError(
                    "base snapshots require a position"
                )
            if hasattr(position, "col") and hasattr(position, "row"):
                coordinates = (float(position.col), float(position.row))
            else:
                coordinates = (float(position[0]), float(position[1]))
            observations.append(
                BaseObservation(
                    base_id=str(getattr(base, "id")),
                    position=coordinates,
                    capacity=int(getattr(base, "capacity")),
                    reserved_load=int(
                        getattr(
                            base,
                            "reserved_load",
                            getattr(base, "occupancy", 0),
                        )
                    ),
                )
            )
        return tuple(sorted(observations, key=lambda base: base.base_id))

    def _validate_controller(
        self, controller: ControllerBase, expected_mode: ControlMode
    ) -> None:
        if not isinstance(controller, ControllerBase):
            raise ControlCoordinatorError(
                "control factory must return a ControllerBase"
            )
        if controller.control_mode is not expected_mode:
            raise ControlCoordinatorError(
                f"controller mode {controller.control_mode.value} does not match "
                f"configured mode {expected_mode.value}"
            )
        if (
            controller.observation_spec.schema_version
            != self.config.observation.schema_version
        ):
            raise ControlCoordinatorError(
                "controller observation schema does not match control config"
            )

    def _validate_recovery_plan(self, plan: RecoveryPlan) -> None:
        if not isinstance(plan, RecoveryPlan):
            raise ControlCoordinatorError(
                "return revocation requires a validated RecoveryPlan"
            )
        if not plan.reservation_id.strip():
            raise ControlCoordinatorError(
                "RecoveryPlan requires a reserved reservation_id"
            )
        if not plan.base_id.strip():
            raise ControlCoordinatorError("RecoveryPlan requires a base_id")
        if len(plan.base_position) != 2 or not all(
            math.isfinite(value) for value in plan.base_position
        ):
            raise ControlCoordinatorError(
                "RecoveryPlan base_position must be finite"
            )
        if not plan.path or any(len(pose) != 3 for pose in plan.path):
            raise ControlCoordinatorError(
                "RecoveryPlan path must contain pose triples"
            )
        if not all(math.isfinite(value) for pose in plan.path for value in pose):
            raise ControlCoordinatorError(
                "RecoveryPlan path must contain finite poses"
            )
        if not all(
            math.isfinite(value) and value >= 0.0
            for value in (plan.path_length_cells, plan.reserve_cells)
        ):
            raise ControlCoordinatorError(
                "RecoveryPlan path_length and reserve must be non-negative"
            )
        actual_length = sum(
            math.dist(start[:2], end[:2])
            for start, end in zip(plan.path, plan.path[1:])
        )
        if not math.isclose(
            actual_length,
            plan.path_length_cells,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ControlCoordinatorError(
                "RecoveryPlan path_length_cells does not match path"
            )
        if not all(
            math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)
            for actual, expected in zip(plan.path[-1][:2], plan.base_position)
        ):
            raise ControlCoordinatorError(
                "RecoveryPlan path must end at base_position"
            )
        if (
            isinstance(plan.planning_map_version, bool)
            or not isinstance(plan.planning_map_version, int)
            or plan.planning_map_version < 0
        ):
            raise ControlCoordinatorError(
                "RecoveryPlan planning_map_version must be non-negative"
            )

    def _mode_for(self, uav_id: str) -> ControlMode:
        self._require_uav(uav_id)
        return self._configured_modes[uav_id]

    def _require_uav(self, uav_id: str) -> None:
        if uav_id not in self._configured_modes:
            raise KeyError(f"unknown UAV: {uav_id}")

    @staticmethod
    def _validate_time(
        value: float, name: str, *, allow_zero: bool = False
    ) -> None:
        lower_bound = 0.0 if allow_zero else 0.0
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < lower_bound
            or (not allow_zero and value == 0.0)
        ):
            qualifier = "non-negative" if allow_zero else "positive"
            raise ControlCoordinatorError(
                f"{name} must be a finite {qualifier} number"
            )

    @staticmethod
    def _task_controller_id(task: ControlTask) -> str:
        prefix = (
            "tracking"
            if task.task_type is OperationMode.TRACK
            else task.task_type.value
        )
        return f"{prefix}:{task.task_id}"

    @staticmethod
    def _stale_message(
        uav_id: str, expected: ControlLease, current: ControlLease
    ) -> str:
        return (
            f"stale command for {uav_id}: expected generation "
            f"{expected.generation}, current generation {current.generation}"
        )

    @staticmethod
    def _stop_controller(
        controller: ControllerBase, reason: StopReason
    ) -> None:
        if isinstance(controller, HeuristicControllerBase):
            controller.stop_task(reason)
        else:
            controller.close()

    def _atomic(self, action: Callable[[], _T]) -> _T:
        with self._lock:
            return action()


__all__ = [
    "ControlCoordinator",
    "ControlCoordinatorError",
    "ControlTickResult",
    "EmergencyRevokeRequired",
    "StaleControlCommand",
]

"""Atomic event-driven replacement of single-task heuristic controllers."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from threading import RLock
from typing import TypeVar

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    ControlEvent,
    ControlMode,
    ControlOwner,
    ControlTask,
    OperationMode,
    StopReason,
)
from src.control.common.factory import ControlFactory
from src.control.common.ownership import ControlLease, ControlOwnership
from src.control.heuristic.base import HeuristicControllerBase


EVENT_TRANSITIONS = {
    "target_found": OperationMode.TRACK,
    "target_lost": OperationMode.COVERAGE,
    "civilian_released": OperationMode.COVERAGE,
    "target_departed": OperationMode.COVERAGE,
    "search_complete": OperationMode.HOLDING,
    "task_failed": OperationMode.HOLDING,
}


@dataclass(frozen=True)
class TaskTransition:
    consumed: bool
    previous_lease: ControlLease
    current_lease: ControlLease
    controller: ControllerBase
    task: ControlTask | None
    request_assignment: bool = False

    @classmethod
    def unchanged(
        cls,
        lease: ControlLease,
        controller: ControllerBase,
        task: ControlTask | None,
    ) -> "TaskTransition":
        return cls(False, lease, lease, controller, task)


_T = TypeVar("_T")
AtomicBoundary = Callable[[Callable[[], _T]], _T]


class HeuristicTaskFlow:
    def __init__(
        self,
        ownership: ControlOwnership,
        factory: ControlFactory,
        controller_registry: MutableMapping[str, ControllerBase],
        pending_tasks: MutableMapping[str, ControlTask],
        *,
        atomic: AtomicBoundary | None = None,
    ) -> None:
        self._ownership = ownership
        self._factory = factory
        self._controllers = controller_registry
        self._pending_tasks = pending_tasks
        self._saved_coverage_tasks: dict[str, ControlTask] = {}
        self._lock = RLock()
        self._atomic = atomic or self._under_lock

    def handle(
        self,
        event: ControlEvent,
        lease: ControlLease,
    ) -> TaskTransition:
        if event.event_type in EVENT_TRANSITIONS and event.uav_id is None:
            raise ValueError("task transition event requires a uav_id")
        if event.uav_id not in (None, lease.uav_id):
            raise ValueError(
                f"event for {event.uav_id} cannot transition {lease.uav_id}"
            )
        controller = self._controllers[lease.uav_id]
        current_task = self._active_task(
            controller, self._pending_tasks.get(lease.uav_id)
        )
        if (
            lease.owner is not ControlOwner.HEURISTIC
            or event.event_type not in EVENT_TRANSITIONS
        ):
            return TaskTransition.unchanged(lease, controller, current_task)

        replacement_task, request_assignment = self._replacement_task(
            event, current_task
        )
        replacement = self._factory.create_heuristic(
            lease.uav_id, replacement_task
        )
        self._validate_replacement(replacement, replacement_task)
        new_owner = (
            ControlOwner.SYSTEM
            if replacement_task.task_type is OperationMode.HOLDING
            else ControlOwner.HEURISTIC
        )

        def commit() -> ControlLease:
            if new_owner is ControlOwner.SYSTEM:
                current_lease = self._ownership.release_to_system(
                    lease, event.timestamp_min
                )
            else:
                current_lease = self._ownership.replace(
                    lease,
                    ControlOwner.HEURISTIC,
                    self._controller_id(replacement_task),
                    event.timestamp_min,
                )
            self._controllers[lease.uav_id] = replacement
            self._pending_tasks[lease.uav_id] = replacement_task
            self._update_saved_coverage(event, current_task)
            return current_lease

        current_lease = self._atomic(commit)
        controller.stop_task(self._stop_reason(event.event_type))
        return TaskTransition(
            True,
            lease,
            current_lease,
            replacement,
            replacement_task,
            request_assignment,
        )

    def _replacement_task(
        self,
        event: ControlEvent,
        current_task: ControlTask | None,
    ) -> tuple[ControlTask, bool]:
        if event.event_type == "target_found":
            contact_id = event.payload.get("contact_id")
            if not isinstance(contact_id, str) or not contact_id:
                raise ValueError("target_found requires a non-empty contact_id")
            return (
                ControlTask(
                    f"track:{contact_id}",
                    OperationMode.TRACK,
                    target_contact_id=contact_id,
                ),
                False,
            )
        if event.event_type in {
            "target_lost",
            "civilian_released",
            "target_departed",
        }:
            saved = self._saved_coverage_tasks.get(event.uav_id or "")
            if saved is not None:
                return saved, False
        del current_task
        return (
            ControlTask(
                f"holding:{event.uav_id}:{event.sequence}",
                OperationMode.HOLDING,
            ),
            True,
        )

    def _update_saved_coverage(
        self, event: ControlEvent, previous_task: ControlTask | None
    ) -> None:
        uav_id = event.uav_id
        assert uav_id is not None
        if (
            event.event_type == "target_found"
            and previous_task is not None
            and previous_task.task_type is OperationMode.COVERAGE
        ):
            self._saved_coverage_tasks[uav_id] = previous_task
        elif event.event_type in {
            "target_lost",
            "civilian_released",
            "target_departed",
        }:
            self._saved_coverage_tasks.pop(uav_id, None)

    @staticmethod
    def _active_task(
        controller: ControllerBase, fallback: ControlTask | None
    ) -> ControlTask | None:
        context = getattr(controller, "context", None)
        task = getattr(context, "task", None)
        return task if isinstance(task, ControlTask) else fallback

    @staticmethod
    def _controller_id(task: ControlTask) -> str:
        prefix = "tracking" if task.task_type is OperationMode.TRACK else task.task_type.value
        return f"{prefix}:{task.task_id}"

    @staticmethod
    def _validate_replacement(
        controller: ControllerBase, task: ControlTask
    ) -> None:
        if not isinstance(controller, HeuristicControllerBase):
            raise TypeError("heuristic task replacement must use a heuristic controller")
        if controller.control_mode is not ControlMode.HEURISTIC:
            raise ValueError("heuristic task replacement has the wrong control mode")
        if controller.operation_mode is not task.task_type:
            raise ValueError("replacement controller and task operation modes differ")

    @staticmethod
    def _stop_reason(event_type: str) -> StopReason:
        if event_type == "search_complete":
            return StopReason.COMPLETED
        if event_type == "task_failed":
            return StopReason.FAILED
        return StopReason.PREEMPTED

    def _under_lock(self, commit: Callable[[], _T]) -> _T:
        with self._lock:
            return commit()


__all__ = ["EVENT_TRANSITIONS", "HeuristicTaskFlow", "TaskTransition"]

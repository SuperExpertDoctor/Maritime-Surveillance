from __future__ import annotations

from collections.abc import Callable

import pytest

from src.control.common.contracts import (
    ActionSpec,
    ControlEvent,
    ControlOwner,
    ControlTask,
    ControllerContext,
    ObservationSpec,
    OperationMode,
)
from src.control.common.factory import ControlFactory
from src.control.common.ownership import ControlOwnership
from src.control.heuristic.coverage import CoverageController
from src.control.heuristic.return_to_base import SystemHoldingController
from src.control.heuristic.task_flow import EVENT_TRANSITIONS, HeuristicTaskFlow
from src.control.heuristic.tracking import TrackingController
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox


def _event(event_type: str, sequence: int = 1, **payload) -> ControlEvent:
    return ControlEvent(
        sequence=sequence,
        timestamp_min=float(sequence),
        event_type=event_type,
        source="test",
        uav_id="UAV-1",
        payload=payload,
    )


@pytest.fixture
def factory() -> ControlFactory:
    config = ConfigLoader.load().control
    return ControlFactory(
        config,
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=ActionSpec(-2.0, 2.0, 0.5, 1.0),
    )


@pytest.fixture
def coverage_task() -> ControlTask:
    return ControlTask(
        "S1",
        OperationMode.COVERAGE,
        region_bbox=BBox(5, 5, 10, 10),
    )


def _heuristic_flow(factory, coverage_task, *, atomic=None):
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire(
        "UAV-1", ControlOwner.HEURISTIC, "coverage:S1", 0.0
    )
    controller = factory.create_heuristic("UAV-1", coverage_task)
    controllers = {"UAV-1": controller}
    pending_tasks = {"UAV-1": coverage_task}
    flow = HeuristicTaskFlow(
        ownership,
        factory,
        controllers,
        pending_tasks,
        atomic=atomic,
    )
    return flow, ownership, lease, controllers, pending_tasks, controller


def test_event_transitions_are_exact_and_exclude_work_range_exhausted():
    assert EVENT_TRANSITIONS == {
        "target_found": OperationMode.TRACK,
        "target_lost": OperationMode.COVERAGE,
        "civilian_released": OperationMode.COVERAGE,
        "target_departed": OperationMode.COVERAGE,
        "search_complete": OperationMode.HOLDING,
        "task_failed": OperationMode.HOLDING,
    }
    assert "work_range_exhausted" not in EVENT_TRANSITIONS


def test_target_found_atomically_replaces_coverage_with_unstarted_tracking(
    factory, coverage_task
):
    boundary_calls = []
    state = {}

    def atomic(commit: Callable[[], object]):
        boundary_calls.append("enter")
        assert state["controllers"]["UAV-1"] is state["old_controller"]
        current = commit()
        assert state["ownership"].current("UAV-1") == current
        assert isinstance(state["controllers"]["UAV-1"], TrackingController)
        assert state["pending_tasks"]["UAV-1"].task_type is OperationMode.TRACK
        boundary_calls.append("exit")
        return current

    flow, ownership, lease, controllers, pending_tasks, old_controller = (
        _heuristic_flow(factory, coverage_task, atomic=atomic)
    )
    state.update(
        ownership=ownership,
        controllers=controllers,
        pending_tasks=pending_tasks,
        old_controller=old_controller,
    )
    stop_calls = []

    def stop_task(reason):
        stop_calls.append((reason, controllers["UAV-1"]))

    old_controller.stop_task = stop_task

    transition = flow.handle(_event("target_found", contact_id="C1"), lease)

    assert transition.consumed
    assert transition.previous_lease is lease
    assert transition.previous_lease.generation + 1 == transition.current_lease.generation
    assert transition.current_lease.owner is ControlOwner.HEURISTIC
    assert transition.current_lease.controller_id.startswith("tracking:")
    assert isinstance(transition.controller, TrackingController)
    assert transition.controller.operation_mode is OperationMode.TRACK
    assert transition.controller.task is None
    assert transition.task == ControlTask(
        "track:C1", OperationMode.TRACK, target_contact_id="C1"
    )
    assert not transition.request_assignment
    assert boundary_calls == ["enter", "exit"]
    assert stop_calls and stop_calls[0][1] is transition.controller


@pytest.mark.parametrize(
    "event_type", ["target_lost", "civilian_released", "target_departed"]
)
def test_tracking_exit_restores_the_saved_coverage_task(
    factory, coverage_task, event_type
):
    flow, _, coverage_lease, controllers, pending_tasks, _ = _heuristic_flow(
        factory, coverage_task
    )
    tracking = flow.handle(
        _event("target_found", sequence=1, contact_id="C1"), coverage_lease
    )

    restored = flow.handle(_event(event_type, sequence=2), tracking.current_lease)

    assert restored.consumed
    assert restored.previous_lease.generation + 1 == restored.current_lease.generation
    assert restored.current_lease.owner is ControlOwner.HEURISTIC
    assert isinstance(restored.controller, CoverageController)
    assert restored.controller.operation_mode is OperationMode.COVERAGE
    assert restored.controller.task is None
    assert restored.task is coverage_task
    assert controllers["UAV-1"] is restored.controller
    assert pending_tasks["UAV-1"] is coverage_task
    assert not restored.request_assignment


def test_target_found_recovers_active_coverage_after_pending_task_is_popped(
    factory, coverage_task
):
    flow, _, coverage_lease, controllers, pending_tasks, coverage_controller = (
        _heuristic_flow(factory, coverage_task)
    )
    coverage_controller.reset(
        ControllerContext(
            "UAV-1",
            1.0,
            coverage_controller.observation_spec,
            coverage_controller.action_spec,
            "UAV-1:1",
            coverage_task,
        )
    )
    pending_tasks.pop("UAV-1")

    tracking = flow.handle(
        _event("target_found", sequence=1, contact_id="C1"), coverage_lease
    )
    restored = flow.handle(
        _event("target_lost", sequence=2), tracking.current_lease
    )

    assert restored.task is coverage_task
    assert isinstance(restored.controller, CoverageController)
    assert pending_tasks["UAV-1"] is coverage_task
    assert controllers["UAV-1"] is restored.controller


@pytest.mark.parametrize("event_type", ["search_complete", "task_failed"])
def test_terminal_event_discards_context_saved_coverage_before_a_later_track_lease(
    factory, coverage_task, event_type
):
    flow, ownership, coverage_lease, controllers, pending_tasks, coverage_controller = (
        _heuristic_flow(factory, coverage_task)
    )
    coverage_controller.reset(
        ControllerContext(
            "UAV-1",
            1.0,
            coverage_controller.observation_spec,
            coverage_controller.action_spec,
            "UAV-1:1",
            coverage_task,
        )
    )
    pending_tasks.pop("UAV-1")

    tracking = flow.handle(
        _event("target_found", sequence=1, contact_id="C1"), coverage_lease
    )
    flow.handle(_event(event_type, sequence=2), tracking.current_lease)

    later_task = ControlTask(
        "track:C2", OperationMode.TRACK, target_contact_id="C2"
    )
    later_lease = ownership.acquire(
        "UAV-1", ControlOwner.HEURISTIC, "tracking:track:C2", 3.0
    )
    controllers["UAV-1"] = factory.create_heuristic("UAV-1", later_task)
    pending_tasks["UAV-1"] = later_task

    lost = flow.handle(_event("target_lost", sequence=4), later_lease)

    assert lost.current_lease.owner is ControlOwner.SYSTEM
    assert isinstance(lost.controller, SystemHoldingController)
    assert lost.task.task_type is OperationMode.HOLDING
    assert lost.request_assignment


def test_explicit_lifecycle_cleanup_discards_context_saved_coverage_before_revoke(
    factory, coverage_task
):
    flow, ownership, coverage_lease, controllers, pending_tasks, coverage_controller = (
        _heuristic_flow(factory, coverage_task)
    )
    coverage_controller.reset(
        ControllerContext(
            "UAV-1",
            1.0,
            coverage_controller.observation_spec,
            coverage_controller.action_spec,
            "UAV-1:1",
            coverage_task,
        )
    )
    pending_tasks.pop("UAV-1")
    tracking = flow.handle(
        _event("target_found", sequence=1, contact_id="C1"), coverage_lease
    )

    flow.clear_saved_coverage("UAV-1")
    flow.clear_saved_coverage("UAV-1")
    unchanged = flow.handle(
        _event("work_range_exhausted", sequence=2), tracking.current_lease
    )
    assert not unchanged.consumed
    assert unchanged.current_lease is tracking.current_lease

    ownership.release_to_system(tracking.current_lease, 2.0)
    later_task = ControlTask(
        "track:C2", OperationMode.TRACK, target_contact_id="C2"
    )
    later_lease = ownership.acquire(
        "UAV-1", ControlOwner.HEURISTIC, "tracking:track:C2", 3.0
    )
    controllers["UAV-1"] = factory.create_heuristic("UAV-1", later_task)
    pending_tasks["UAV-1"] = later_task

    lost = flow.handle(_event("target_lost", sequence=4), later_lease)

    assert lost.current_lease.owner is ControlOwner.SYSTEM
    assert isinstance(lost.controller, SystemHoldingController)
    assert lost.task.task_type is OperationMode.HOLDING
    assert lost.request_assignment


@pytest.mark.parametrize("event_type", ["search_complete", "task_failed"])
def test_terminal_task_event_releases_to_system_holding(
    factory, coverage_task, event_type
):
    flow, ownership, lease, controllers, pending_tasks, _ = _heuristic_flow(
        factory, coverage_task
    )

    transition = flow.handle(_event(event_type), lease)

    assert transition.consumed
    assert transition.current_lease == ownership.current("UAV-1")
    assert transition.current_lease.owner is ControlOwner.SYSTEM
    assert transition.current_lease.generation == lease.generation + 1
    assert isinstance(transition.controller, SystemHoldingController)
    assert transition.controller.operation_mode is OperationMode.HOLDING
    assert transition.controller.task is None
    assert transition.task is pending_tasks["UAV-1"]
    assert transition.task.task_type is OperationMode.HOLDING
    assert controllers["UAV-1"] is transition.controller
    assert transition.request_assignment


def test_tracking_exit_without_saved_coverage_requests_assignment_and_holds(factory):
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire(
        "UAV-1", ControlOwner.HEURISTIC, "track:C1", 0.0
    )
    task = ControlTask("track:C1", OperationMode.TRACK, target_contact_id="C1")
    controller = factory.create_heuristic("UAV-1", task)
    controllers = {"UAV-1": controller}
    pending_tasks = {"UAV-1": task}
    flow = HeuristicTaskFlow(ownership, factory, controllers, pending_tasks)

    transition = flow.handle(_event("target_lost"), lease)

    assert transition.current_lease.owner is ControlOwner.SYSTEM
    assert isinstance(transition.controller, SystemHoldingController)
    assert transition.request_assignment


@pytest.mark.parametrize("event_type", [*EVENT_TRANSITIONS, "route_blocked"])
def test_learning_task_events_remain_unconsumed_and_do_not_replace_the_lease(
    factory, event_type
):
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire("UAV-1", ControlOwner.LEARNING, "rl:UAV-1", 0.0)
    controller = object()
    task = ControlTask("sortie-1", OperationMode.COVERAGE)
    controllers = {"UAV-1": controller}
    pending_tasks = {"UAV-1": task}
    flow = HeuristicTaskFlow(ownership, factory, controllers, pending_tasks)

    transition = flow.handle(_event(event_type, contact_id="C1"), lease)

    assert not transition.consumed
    assert transition.previous_lease is lease
    assert transition.current_lease is lease
    assert transition.controller is controller
    assert transition.task is task
    assert ownership.current("UAV-1") is lease
    assert controllers["UAV-1"] is controller
    assert pending_tasks["UAV-1"] is task


def test_work_range_exhausted_is_not_consumed_for_a_heuristic_lease(
    factory, coverage_task
):
    flow, ownership, lease, controllers, pending_tasks, controller = (
        _heuristic_flow(factory, coverage_task)
    )

    transition = flow.handle(_event("work_range_exhausted"), lease)

    assert not transition.consumed
    assert transition.current_lease is lease
    assert transition.controller is controller
    assert transition.task is coverage_task
    assert ownership.current("UAV-1") is lease
    assert controllers["UAV-1"] is controller
    assert pending_tasks["UAV-1"] is coverage_task


def test_invalid_successor_is_rejected_before_any_state_mutation(
    factory, coverage_task
):
    flow, ownership, lease, controllers, pending_tasks, controller = (
        _heuristic_flow(factory, coverage_task)
    )
    stop_calls = []
    controller.stop_task = stop_calls.append

    with pytest.raises(ValueError, match="contact_id"):
        flow.handle(_event("target_found"), lease)

    assert ownership.current("UAV-1") is lease
    assert controllers["UAV-1"] is controller
    assert pending_tasks["UAV-1"] is coverage_task
    assert stop_calls == []


def test_mapped_global_event_is_rejected_before_any_state_mutation(
    factory, coverage_task
):
    flow, ownership, lease, controllers, pending_tasks, controller = (
        _heuristic_flow(factory, coverage_task)
    )
    event = ControlEvent(
        sequence=1,
        timestamp_min=1.0,
        event_type="search_complete",
        source="test",
        uav_id=None,
        payload={},
    )

    with pytest.raises(ValueError, match="uav_id"):
        flow.handle(event, lease)

    assert ownership.current("UAV-1") is lease
    assert controllers["UAV-1"] is controller
    assert pending_tasks["UAV-1"] is coverage_task

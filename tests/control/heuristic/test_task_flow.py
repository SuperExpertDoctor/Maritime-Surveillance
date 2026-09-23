from __future__ import annotations


import pytest

from src.control.common.contracts import (
    ActionSpec,
    ControlEvent,
    ControlOwner,
    ControlTask,
    ObservationSpec,
    OperationMode,
)
from src.control.common.factory import ControlFactory
from src.control.common.ownership import ControlOwnership
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
            "type_ii_confirmed": OperationMode.TRACK,
            "type_i_released": OperationMode.HOLDING,
            "target_lost": OperationMode.HOLDING,
        "duplicate_task_cancelled": OperationMode.HOLDING,
        "target_departed": OperationMode.HOLDING,
        "search_complete": OperationMode.HOLDING,
        "task_failed": OperationMode.HOLDING,
    }
    assert "target_found" not in EVENT_TRANSITIONS


def test_target_found_does_not_take_over_an_existing_coverage_task(factory, coverage_task):
    flow, ownership, lease, controllers, pending_tasks, controller = _heuristic_flow(
        factory, coverage_task
    )

    transition = flow.handle(_event("target_found", contact_id="C1"), lease)

    assert not transition.consumed
    assert ownership.current("UAV-1") is lease
    assert controllers["UAV-1"] is controller
    assert pending_tasks["UAV-1"] is coverage_task


def test_type_ii_confirmation_replaces_an_active_probe_with_tracking(factory, coverage_task):
    flow, _, lease, controllers, pending_tasks, controller = _heuristic_flow(
        factory, coverage_task
    )
    pending_tasks["UAV-1"] = ControlTask(
        "probe:C1", OperationMode.PROBE, target_contact_id="C1", probe_id="P1"
    )

    transition = flow.handle(
        _event(
            "type_ii_confirmed", contact_id="C1", vessel_class="type_ii", probe_id="P1"
        ),
        lease,
    )

    assert transition.consumed
    assert transition.current_lease.owner is ControlOwner.HEURISTIC
    assert transition.current_lease.generation == lease.generation + 1
    assert isinstance(transition.controller, TrackingController)
    assert transition.task == ControlTask("track:C1", OperationMode.TRACK, target_contact_id="C1")
    assert controllers["UAV-1"] is transition.controller
    assert controller is not transition.controller


def test_stale_type_ii_confirmation_cannot_replace_the_active_probe(
    factory, coverage_task
):
    flow, ownership, lease, controllers, pending_tasks, controller = _heuristic_flow(
        factory, coverage_task
    )
    active_probe = ControlTask(
        "probe:C1", OperationMode.PROBE, target_contact_id="C1", probe_id="P2"
    )
    pending_tasks["UAV-1"] = active_probe

    transition = flow.handle(
        _event(
            "type_ii_confirmed", contact_id="C1", vessel_class="type_ii", probe_id="P1"
        ),
        lease,
    )

    assert not transition.consumed
    assert transition.current_lease is lease
    assert ownership.current("UAV-1") is lease
    assert controllers["UAV-1"] is controller
    assert pending_tasks["UAV-1"] is active_probe


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


def test_tracking_exit_restores_saved_coverage_without_new_assignment(factory, coverage_task):
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire(
        "UAV-1", ControlOwner.HEURISTIC, "tracking:C1", 0.0
    )
    task = ControlTask("track:C1", OperationMode.TRACK, target_contact_id="C1")
    controller = factory.create_heuristic("UAV-1", task)
    controllers = {"UAV-1": controller}
    pending_tasks = {"UAV-1": task}
    flow = HeuristicTaskFlow(ownership, factory, controllers, pending_tasks)
    flow.save_coverage_task("UAV-1", coverage_task, generation=1)

    transition = flow.handle(_event("target_lost"), lease)

    assert transition.consumed
    assert transition.current_lease.owner is ControlOwner.HEURISTIC
    assert transition.task == coverage_task
    assert not transition.request_assignment
    assert "UAV-1" not in flow._saved_coverage_tasks


def test_saved_coverage_round_trip_restores_generation_and_route(factory, coverage_task):
    flow, *_ = _heuristic_flow(factory, coverage_task)
    route = ((5.0, 5.0, 0.0), (9.0, 5.0, 0.0))

    flow.save_coverage_task(
        "UAV-1", coverage_task, generation=7, route=route,
    )
    restored = flow.restore_coverage_task("UAV-1", generation=7)

    assert restored is not None
    assert restored.task == coverage_task
    assert restored.generation == 7
    assert restored.route == route
    assert "UAV-1" not in flow._saved_coverage_tasks


def test_type_i_release_replaces_active_tracking_with_holding(factory):
    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire(
        "UAV-1", ControlOwner.HEURISTIC, "track:C1", 0.0
    )
    task = ControlTask("track:C1", OperationMode.TRACK, target_contact_id="C1")
    controller = factory.create_heuristic("UAV-1", task)
    controllers = {"UAV-1": controller}
    pending_tasks = {"UAV-1": task}
    flow = HeuristicTaskFlow(ownership, factory, controllers, pending_tasks)

    transition = flow.handle(
        _event("type_i_released", contact_id="C1", vessel_class="type_i"),
        lease,
    )

    assert transition.consumed
    assert transition.current_lease.owner is ControlOwner.SYSTEM
    assert transition.request_assignment
    assert transition.task is not None
    assert transition.task.task_type is OperationMode.HOLDING


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


def test_invalid_assessment_does_not_take_over_before_any_state_mutation(
    factory, coverage_task
):
    flow, ownership, lease, controllers, pending_tasks, controller = (
        _heuristic_flow(factory, coverage_task)
    )
    stop_calls = []
    controller.stop_task = stop_calls.append

    transition = flow.handle(_event("type_ii_confirmed", vessel_class="type_ii"), lease)

    assert ownership.current("UAV-1") is lease
    assert controllers["UAV-1"] is controller
    assert pending_tasks["UAV-1"] is coverage_task
    assert stop_calls == []
    assert not transition.consumed


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


@pytest.mark.parametrize("region_state", ["reassigned", "completed", "missing"])
def test_tracking_exit_does_not_restore_unavailable_saved_search(factory, coverage_task, region_state):
    from types import SimpleNamespace
    from src.schedule.datatypes import Region

    ownership = ControlOwnership(["UAV-1"])
    lease = ownership.acquire("UAV-1", ControlOwner.HEURISTIC, "tracking:C1", 0.0)
    task = ControlTask("track:C1", OperationMode.TRACK, target_contact_id="C1")
    controller = factory.create_heuristic("UAV-1", task)
    region = Region(
        coverage_task.task_id, coverage_task.region_bbox, "search",
        status="completed" if region_state == "completed" else "active",
        assigned_uav_id="UAV-2" if region_state == "reassigned" else None,
    )
    state = SimpleNamespace(
        get_search_regions=lambda: [] if region_state == "missing" else [region],
    )
    flow = HeuristicTaskFlow(
        ownership, factory, {"UAV-1": controller}, {"UAV-1": task},
        state_manager=state,
    )
    flow.save_coverage_task("UAV-1", coverage_task, generation=1)

    transition = flow.handle(_event("target_lost"), lease)

    assert transition.task.task_type is OperationMode.HOLDING
    assert transition.request_assignment
    assert "UAV-1" not in flow._saved_coverage_tasks
    assert region.assigned_uav_id == ("UAV-2" if region_state == "reassigned" else None)

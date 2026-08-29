from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import math
from typing import Any

import numpy as np
import pytest

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    ActionSpec,
    BaseObservation,
    ControlCommand,
    ControlDecision,
    ControlEvent,
    ControlMode,
    ControlOwner,
    ControlTask,
    ControllerEventRequest,
    ObservationSpec,
    OperationMode,
    RecoveryPlan,
    SensorMode,
    StopReason,
)
from src.control.common.coordinator import (
    ControlCoordinator,
    ControlCoordinatorError,
    ControlTickResult,
    EmergencyRevokeRequired,
    StaleControlCommand,
)
from src.control.common.executor import UAVDynamicsExecutor
from src.control.common.factory import ControlFactory
from src.control.common.observation import ObservationProvider
from src.control.common.operation_registry import OperationRegistry
from src.control.common.ownership import ControlOwnership
from src.control.common.safety import InvalidControlCommand, SafetyEnvelope
from src.control.heuristic.base import HeuristicControllerBase
from src.control.heuristic.return_to_base import ReturnToBaseController
from src.env.uav_entity import UAVEntity
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord
from src.schedule.state_manager import StateManager


OBSERVATION_SPEC = ObservationSpec("control-observation/v1", 11)
ACTION_SPEC = ActionSpec(-0.5, 0.5, 0.1, 1.0)


def command(
    operation_mode: OperationMode = OperationMode.COVERAGE,
    *,
    speed: float = 0.2,
    turn_rate: float = 0.0,
    sensor_mode: SensorMode = SensorMode.OFF,
    target_contact_id: str | None = None,
    schema_version: str = "control-command/v1",
) -> ControlCommand:
    return ControlCommand(
        turn_rate,
        speed,
        sensor_mode,
        operation_mode,
        target_contact_id,
        schema_version,
    )


class DeterministicController(ControllerBase):
    def __init__(
        self,
        mode: ControlMode,
        commands: tuple[ControlCommand, ...] = (command(),),
        *,
        event_batches: tuple[tuple[ControllerEventRequest, ...], ...] = (),
    ) -> None:
        self._mode = mode
        self.commands = commands
        self.event_batches = event_batches
        self.observations = []
        self.calls: list[str] = []
        self.act_hook = None

    @property
    def control_mode(self) -> ControlMode:
        return self._mode

    @property
    def observation_spec(self) -> ObservationSpec:
        return OBSERVATION_SPEC

    @property
    def action_spec(self) -> ActionSpec:
        return ACTION_SPEC

    def reset(self, context) -> None:
        super().reset(context)
        self.calls.append("reset")

    def act(self, observation) -> ControlDecision:
        index = len(self.observations)
        self.observations.append(observation)
        self.calls.append("act")
        if self.act_hook is not None:
            self.act_hook()
        selected = self.commands[min(index, len(self.commands) - 1)]
        events = (
            self.event_batches[index]
            if index < len(self.event_batches)
            else ()
        )
        return ControlDecision(selected, events)


class DeterministicHeuristicController(HeuristicControllerBase):
    def __init__(self, task: ControlTask) -> None:
        self.task_definition = task
        self.observations = []
        self.calls: list[str] = []
        self.started_task: ControlTask | None = None
        self.start_observation = None
        self.stop_reasons: list[StopReason] = []

    @property
    def observation_spec(self) -> ObservationSpec:
        return OBSERVATION_SPEC

    @property
    def action_spec(self) -> ActionSpec:
        return ACTION_SPEC

    @property
    def operation_mode(self) -> OperationMode:
        return self.task_definition.task_type

    def reset(self, context) -> None:
        super().reset(context)
        self.calls.append("reset")

    def start_task(self, task: ControlTask, observation) -> None:
        self.started_task = task
        self.start_observation = observation
        self.calls.append("start")

    def act(self, observation) -> ControlDecision:
        self.observations.append(observation)
        self.calls.append("act")
        return ControlDecision(
            command(
                self.operation_mode,
                target_contact_id=self.task_definition.target_contact_id,
            )
        )

    def is_complete(self, observation) -> bool:
        del observation
        return False

    def stop_task(self, reason: StopReason) -> None:
        self.stop_reasons.append(reason)
        self.calls.append("stop")


class DeterministicFactory(ControlFactory):
    def __init__(self, control_config) -> None:
        super().__init__(
            control_config,
            observation_spec=OBSERVATION_SPEC,
            action_spec=ACTION_SPEC,
        )
        self.heuristic_creations: list[
            tuple[str, ControlTask, DeterministicHeuristicController]
        ] = []

    def create_heuristic(
        self, uav_id: str, task: ControlTask
    ) -> ControllerBase:
        if task.task_type is OperationMode.RETURN:
            return super().create_heuristic(uav_id, task)
        controller = DeterministicHeuristicController(task)
        self.heuristic_creations.append((uav_id, task, controller))
        return controller


def make_uav(uav_id: str, col: int = 10, row: int = 10) -> UAVEntity:
    entity = UAVEntity(
        uav_id,
        GridCoord(col, row),
        endurance_h=8.0,
        cruise_speed_kmh=60.0,
    )
    entity.heading_rad = 0.0
    return entity


def make_runtime(
    modes: dict[str, ControlMode],
    controllers: dict[str, DeterministicController] | None = None,
    *,
    factory: ControlFactory | None = None,
    bases: tuple[BaseObservation, ...] = (),
):
    config = ConfigLoader.load()
    config.uav.count_max = len(modes)
    state_manager = StateManager(config)
    state_manager.set_environment_obstacles(
        [], np.zeros(config.grid.resolution, dtype=bool)
    )
    ownership = ControlOwnership(tuple(modes))
    resolved_factory = factory or ControlFactory(
        config.control,
        observation_spec=OBSERVATION_SPEC,
        action_spec=ACTION_SPEC,
    )
    if controllers:
        by_mode: dict[ControlMode, dict[str, DeterministicController]] = {}
        for uav_id, controller in controllers.items():
            by_mode.setdefault(controller.control_mode, {})[uav_id] = controller
        for mode, mode_controllers in by_mode.items():
            def provider(
                uav_id: str,
                *,
                values: dict[str, DeterministicController] = mode_controllers,
            ) -> DeterministicController:
                return values[uav_id]

            resolved_factory.register(mode, provider)
    registry = OperationRegistry(state_manager)
    coordinator = ControlCoordinator(
        config=config.control,
        state_manager=state_manager,
        ownership=ownership,
        observations=ObservationProvider(config),
        safety=SafetyEnvelope(ACTION_SPEC),
        executor=UAVDynamicsExecutor(),
        factory=resolved_factory,
        operation_registry=registry,
        configured_modes=modes,
        bases=bases,
    )
    return coordinator, ownership, state_manager, registry, resolved_factory


def start_learning(
    coordinator: ControlCoordinator,
    uav_id: str = "UAV-1",
    *,
    sortie_number: int = 1,
) -> None:
    coordinator.start_work(
        uav_id,
        sortie_number=sortie_number,
        current_time=0.0,
        dt_min=1.0,
    )


def coverage_task(task_id: str = "S1") -> ControlTask:
    return ControlTask(
        task_id,
        OperationMode.COVERAGE,
        region_bbox=BBox(6, 6, 14, 14),
    )


def start_heuristic(
    coordinator: ControlCoordinator,
    task: ControlTask | None = None,
    *,
    sortie_number: int = 1,
) -> None:
    coordinator.start_work(
        "UAV-1",
        sortie_number=sortie_number,
        current_time=0.0,
        dt_min=1.0,
        task=task or coverage_task(),
    )


def test_control_tick_result_is_frozen_with_exact_fields():
    assert [field.name for field in fields(ControlTickResult)] == [
        "lease",
        "observation",
        "decision",
        "safety",
        "execution",
        "emitted_events",
    ]

    controller = DeterministicController(ControlMode.BC)
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    start_learning(coordinator)
    result = coordinator.step_uav(make_uav("UAV-1"), current_time=1.0)

    with pytest.raises(FrozenInstanceError):
        result.lease = result.lease


def test_each_uav_is_acted_once_per_tick_and_duplicate_tick_is_rejected():
    controllers = {
        "UAV-1": DeterministicController(ControlMode.BC),
        "UAV-2": DeterministicController(ControlMode.BC),
    }
    coordinator, *_ = make_runtime(
        {uav_id: ControlMode.BC for uav_id in controllers}, controllers
    )
    uavs = [make_uav("UAV-1"), make_uav("UAV-2", 12, 12)]
    for uav in uavs:
        start_learning(coordinator, uav.id, sortie_number=4)

    results = [
        coordinator.step_uav(uav, current_time=1.0) for uav in uavs
    ]

    assert [len(controller.observations) for controller in controllers.values()] == [
        1,
        1,
    ]
    assert [controller.context.episode_id for controller in controllers.values()] == [
        "UAV-1:4",
        "UAV-2:4",
    ]
    assert [result.lease.uav_id for result in results] == ["UAV-1", "UAV-2"]
    with pytest.raises(ControlCoordinatorError, match="already stepped"):
        coordinator.step_uav(uavs[0], current_time=1.0)
    assert len(controllers["UAV-1"].observations) == 1


def test_queued_events_are_ordered_visible_once_and_delayed_one_tick():
    controller = DeterministicController(ControlMode.BC)
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)

    first = coordinator.step_uav(uav, current_time=1.0)
    coordinator.queue_event(
        ControlEvent(3, 1.5, "third", "env", uav.id, {})
    )
    coordinator.queue_event(
        ControlEvent(2, 1.5, "second", "env", uav.id, {})
    )
    second = coordinator.step_uav(uav, current_time=2.0)
    third = coordinator.step_uav(uav, current_time=3.0)

    assert first.observation.events == ()
    assert [event.event_type for event in second.observation.events] == [
        "second",
        "third",
    ]
    assert third.observation.events == ()


def test_heuristic_transition_replaces_before_act_and_suppresses_consumed_event():
    config = ConfigLoader.load()
    factory = DeterministicFactory(config.control)
    coordinator, _, state_manager, _, resolved_factory = make_runtime(
        {"UAV-1": ControlMode.HEURISTIC}, factory=factory
    )
    uav = make_uav("UAV-1")
    start_heuristic(coordinator)
    first = coordinator.step_uav(uav, current_time=1.0)
    old_controller = resolved_factory.heuristic_creations[0][2]
    state_manager.record_target_observation(
        "C1", GridCoord(12, 10), "UAV-1", observed_at=1.0
    )
    coordinator.queue_event(
        ControlEvent(
            1,
            1.0,
            "target_found",
            "sensor",
            "UAV-1",
            {"contact_id": "C1"},
        )
    )

    second = coordinator.step_uav(uav, current_time=2.0)
    new_controller = resolved_factory.heuristic_creations[1][2]

    assert len(old_controller.observations) == 1
    assert first.observation.self_state.operation_mode is OperationMode.IDLE
    assert second.observation.self_state.operation_mode is OperationMode.COVERAGE
    assert old_controller.stop_reasons == [StopReason.PREEMPTED]
    assert new_controller.calls == ["reset", "start", "act"]
    assert new_controller.start_observation is second.observation
    assert new_controller.observations == [second.observation]
    assert second.observation.events == ()
    assert second.lease.owner is ControlOwner.HEURISTIC
    assert second.lease.generation == first.lease.generation + 1
    assert second.lease.controller_id.startswith("tracking:")
    assert coordinator._operation_modes["UAV-1"] is OperationMode.TRACK


def test_learning_task_events_are_delivered_untouched_without_replacing_lease():
    controller = DeterministicController(ControlMode.RL)
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.RL}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)
    original = coordinator.current_lease("UAV-1")
    coordinator.queue_event(
        ControlEvent(
            1,
            0.5,
            "target_found",
            "sensor",
            "UAV-1",
            {"contact_id": "C1"},
        )
    )
    coordinator.queue_event(
        ControlEvent(2, 0.5, "search_complete", "controller", "UAV-1", {})
    )

    result = coordinator.step_uav(uav, current_time=1.0)

    assert result.lease is original
    assert coordinator.current_lease("UAV-1") is original
    assert [event.event_type for event in result.observation.events] == [
        "target_found",
        "search_complete",
    ]
    assert coordinator._controllers["UAV-1"] is controller


def test_stale_lease_is_rejected_after_act_before_safety_or_execution():
    controller = DeterministicController(ControlMode.BC)
    coordinator, ownership, _, registry, _ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)
    original = coordinator.current_lease("UAV-1")
    controller.act_hook = lambda: ownership.replace(
        original,
        ControlOwner.LEARNING,
        "bc:replacement",
        1.0,
    )
    before_pose = uav.pose
    before_range = uav.remaining_range_cells

    with pytest.raises(StaleControlCommand, match="expected generation 1.*current generation 2"):
        coordinator.step_uav(uav, current_time=1.0)

    assert uav.pose == before_pose
    assert uav.remaining_range_cells == before_range
    assert uav.last_applied_command is None
    assert registry._track_bindings == {}


def test_controller_event_requests_receive_monotonic_sequences_for_next_tick():
    requested = (
        ControllerEventRequest("task_progress", {"step": 1}),
        ControllerEventRequest("task_complete", {"step": 2}),
    )
    controller = DeterministicController(
        ControlMode.BC,
        event_batches=(requested,),
    )
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)

    first = coordinator.step_uav(uav, current_time=1.0)
    second = coordinator.step_uav(uav, current_time=2.0)

    assert first.observation.events == ()
    assert [event.sequence for event in first.emitted_events] == [1, 2]
    assert [event.event_type for event in first.emitted_events] == [
        "task_progress",
        "task_complete",
    ]
    assert all(event.timestamp_min == 1.0 for event in first.emitted_events)
    assert all(event.source == "controller" for event in first.emitted_events)
    assert second.observation.events == first.emitted_events


def test_safety_result_execution_audit_and_previous_intervention_state_are_recorded():
    controller = DeterministicController(
        ControlMode.BC,
        commands=(
            command(speed=2.0),
            command(speed=0.2),
            command(speed=0.2),
        ),
    )
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)

    clipped = coordinator.step_uav(uav, current_time=1.0)
    clean = coordinator.step_uav(uav, current_time=2.0)
    after_clean = coordinator.step_uav(uav, current_time=3.0)

    assert [item.kind for item in clipped.safety.interventions] == [
        "speed_clipped"
    ]
    assert clipped.execution.requested_command is clipped.decision.command
    assert clipped.execution.applied_command is clipped.safety.applied_command
    assert clipped.execution.interventions == clipped.safety.interventions
    assert uav.last_requested_command is after_clean.decision.command
    assert clean.observation.self_state.safety_intervened
    assert not after_clean.observation.self_state.safety_intervened
    assert coordinator._invalid_streaks["UAV-1"] == 0


def test_third_consecutive_intervention_raises_before_world_or_registry_mutation():
    controller = DeterministicController(
        ControlMode.BC,
        commands=(command(speed=2.0),) * 3,
    )
    coordinator, _, state_manager, registry, _ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)
    coordinator.step_uav(uav, current_time=1.0)
    coordinator.step_uav(uav, current_time=2.0)
    state_uav = state_manager.get_uav("UAV-1")
    assert state_uav is not None
    before = {
        "pose": uav.pose,
        "range": uav.remaining_range_cells,
        "sensor": uav.sensor_mode,
        "last_applied": uav.last_applied_command,
        "state_sensor": state_uav.sensor_mode,
        "bindings": dict(registry._track_bindings),
        "operation": coordinator._operation_modes["UAV-1"],
    }

    with pytest.raises(EmergencyRevokeRequired) as captured:
        coordinator.step_uav(uav, current_time=3.0)

    assert captured.value.uav_id == "UAV-1"
    assert captured.value.invalid_streak == 3
    assert uav.pose == before["pose"]
    assert uav.remaining_range_cells == before["range"]
    assert uav.sensor_mode == before["sensor"]
    assert uav.last_applied_command is before["last_applied"]
    assert state_uav.sensor_mode == before["state_sensor"]
    assert registry._track_bindings == before["bindings"]
    assert coordinator._operation_modes["UAV-1"] is before["operation"]


def test_one_clean_command_resets_the_consecutive_invalid_streak():
    controller = DeterministicController(
        ControlMode.BC,
        commands=(
            command(speed=2.0),
            command(speed=2.0),
            command(speed=0.2),
            command(speed=2.0),
            command(speed=2.0),
        ),
    )
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)

    results = [
        coordinator.step_uav(uav, current_time=float(tick))
        for tick in range(1, 6)
    ]

    assert len(results) == 5
    assert coordinator._invalid_streaks["UAV-1"] == 2


def test_schema_mask_and_nonfinite_rejections_share_the_invalid_threshold():
    controller = DeterministicController(
        ControlMode.BC,
        commands=(
            command(schema_version="control-command/v0"),
            command(OperationMode.RETURN),
            command(speed=math.nan),
        ),
    )
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)
    initial_pose = uav.pose
    initial_range = uav.remaining_range_cells

    with pytest.raises(InvalidControlCommand, match="schema"):
        coordinator.step_uav(uav, current_time=1.0)
    with pytest.raises(InvalidControlCommand, match="operation mode"):
        coordinator.step_uav(uav, current_time=2.0)
    with pytest.raises(EmergencyRevokeRequired) as captured:
        coordinator.step_uav(uav, current_time=3.0)

    assert captured.value.invalid_streak == 3
    assert uav.pose == initial_pose
    assert uav.remaining_range_cells == initial_range
    assert uav.last_applied_command is None


def test_assign_task_is_rejected_for_configured_learning_mode():
    controller = DeterministicController(ControlMode.BC)
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    start_learning(coordinator)
    original = coordinator.current_lease("UAV-1")

    with pytest.raises(ControlCoordinatorError, match="configured heuristic"):
        coordinator.assign_task(
            "UAV-1", coverage_task(), current_time=0.5
        )

    assert coordinator.current_lease("UAV-1") is original
    assert coordinator._controllers["UAV-1"] is controller


def test_assign_task_replaces_heuristic_source_but_starts_it_on_next_tick():
    config = ConfigLoader.load()
    factory = DeterministicFactory(config.control)
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.HEURISTIC}, factory=factory
    )
    uav = make_uav("UAV-1")
    start_heuristic(coordinator)
    first = coordinator.step_uav(uav, current_time=1.0)
    old_controller = factory.heuristic_creations[0][2]

    assigned = coordinator.assign_task(
        "UAV-1", coverage_task("S2"), current_time=1.5
    )
    new_controller = factory.heuristic_creations[1][2]

    assert assigned.generation == first.lease.generation + 1
    assert new_controller.calls == []
    assert old_controller.stop_reasons == [StopReason.PREEMPTED]
    result = coordinator.step_uav(uav, current_time=2.0)
    assert new_controller.calls == ["reset", "start", "act"]
    assert new_controller.start_observation is result.observation


def valid_recovery_plan(uav: UAVEntity, reservation_id: str = "R1") -> RecoveryPlan:
    start = uav.pose
    destination = (start[0] + 1.0, start[1], start[2])
    return RecoveryPlan(
        base_id="B1",
        base_position=destination[:2],
        reservation_id=reservation_id,
        path=(start, destination),
        path_length_cells=1.0,
        reserve_cells=0.5,
        planning_map_version=0,
    )


def test_reserved_validated_return_atomically_installs_system_controller_and_preserves_mode():
    learning = DeterministicController(ControlMode.BC)
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": learning}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator, sortie_number=7)
    plan = valid_recovery_plan(uav)

    returned = coordinator.revoke_for_return(
        "UAV-1", plan, current_time=0.5
    )

    assert returned.owner is ControlOwner.SYSTEM
    assert returned.controller_id == "return:R1"
    assert coordinator.current_lease("UAV-1") is returned
    assert isinstance(coordinator._controllers["UAV-1"], ReturnToBaseController)
    assert coordinator._pending_tasks["UAV-1"].recovery_plan is plan
    assert coordinator._configured_modes["UAV-1"] is ControlMode.BC
    result = coordinator.step_uav(uav, current_time=1.0)
    assert result.lease is returned
    assert result.observation.self_state.control_mode is ControlMode.BC
    assert result.observation.self_state.control_owner is ControlOwner.SYSTEM
    assert result.observation.self_state.operation_mode is OperationMode.RETURN


@pytest.mark.parametrize(
    "plan_update, message",
    [
        ({"reservation_id": ""}, "reservation"),
        ({"path_length_cells": 2.0}, "path_length"),
        ({"base_position": (20.0, 20.0)}, "base_position"),
    ],
)
def test_invalid_or_unreserved_recovery_plan_is_rejected_before_revocation(
    plan_update: dict[str, Any], message: str
):
    learning = DeterministicController(ControlMode.RL)
    coordinator, *_ = make_runtime(
        {"UAV-1": ControlMode.RL}, {"UAV-1": learning}
    )
    uav = make_uav("UAV-1")
    start_learning(coordinator)
    original_lease = coordinator.current_lease("UAV-1")
    original_controller = coordinator._controllers["UAV-1"]
    values = {
        field.name: getattr(valid_recovery_plan(uav), field.name)
        for field in fields(RecoveryPlan)
    }
    values.update(plan_update)
    plan = RecoveryPlan(**values)

    with pytest.raises(ControlCoordinatorError, match=message):
        coordinator.revoke_for_return(
            "UAV-1", plan, current_time=0.5
        )

    assert coordinator.current_lease("UAV-1") is original_lease
    assert coordinator._controllers["UAV-1"] is original_controller
    assert "UAV-1" not in coordinator._pending_tasks


def test_new_sortie_clears_saved_coverage_before_work_begins():
    config = ConfigLoader.load()
    factory = DeterministicFactory(config.control)
    coordinator, ownership, state_manager, *_ = make_runtime(
        {"UAV-1": ControlMode.HEURISTIC}, factory=factory
    )
    uav = make_uav("UAV-1")
    start_heuristic(coordinator, sortie_number=1)
    coordinator.step_uav(uav, current_time=1.0)
    state_manager.record_target_observation(
        "C1", GridCoord(12, 10), "UAV-1", observed_at=1.0
    )
    coordinator.queue_event(
        ControlEvent(
            1,
            1.0,
            "target_found",
            "sensor",
            "UAV-1",
            {"contact_id": "C1"},
        )
    )
    coordinator.step_uav(uav, current_time=2.0)
    tracking_lease = coordinator.current_lease("UAV-1")
    ownership.release_to_system(tracking_lease, 2.5)

    tracking_task = ControlTask(
        "track:C1", OperationMode.TRACK, target_contact_id="C1"
    )
    coordinator.start_work(
        "UAV-1",
        sortie_number=2,
        current_time=2.6,
        dt_min=1.0,
        task=tracking_task,
    )
    coordinator.queue_event(
        ControlEvent(2, 2.7, "target_lost", "sensor", "UAV-1", {})
    )
    result = coordinator.step_uav(uav, current_time=3.0)

    assert result.lease.owner is ControlOwner.SYSTEM
    assert result.observation.self_state.operation_mode is OperationMode.HOLDING
    assert coordinator._pending_tasks == {}


def test_external_return_revocation_clears_saved_coverage_without_consuming_lifecycle_event():
    config = ConfigLoader.load()
    factory = DeterministicFactory(config.control)
    coordinator, _, state_manager, *_ = make_runtime(
        {"UAV-1": ControlMode.HEURISTIC}, factory=factory
    )
    uav = make_uav("UAV-1")
    start_heuristic(coordinator)
    coordinator.step_uav(uav, current_time=1.0)
    state_manager.record_target_observation(
        "C1", GridCoord(12, 10), "UAV-1", observed_at=1.0
    )
    coordinator.queue_event(
        ControlEvent(
            1,
            1.0,
            "target_found",
            "sensor",
            "UAV-1",
            {"contact_id": "C1"},
        )
    )
    coordinator.step_uav(uav, current_time=2.0)
    assert "UAV-1" in coordinator._task_flow._saved_coverage_tasks
    coordinator.queue_event(
        ControlEvent(
            2,
            2.0,
            "work_range_exhausted",
            "lifecycle",
            "UAV-1",
            {},
        )
    )

    coordinator.revoke_for_return(
        "UAV-1", valid_recovery_plan(uav), current_time=2.5
    )
    result = coordinator.step_uav(uav, current_time=3.0)

    assert "UAV-1" not in coordinator._task_flow._saved_coverage_tasks
    assert [event.event_type for event in result.observation.events] == [
        "work_range_exhausted"
    ]
    assert result.lease.owner is ControlOwner.SYSTEM

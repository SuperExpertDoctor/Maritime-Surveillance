import math
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import numpy as np
import pytest

from src.control.common.contracts import (
    ActionMask,
    ActionSpec,
    BaseObservation,
    ContactObservation,
    ControlMode,
    ControlObservation,
    ControlOwner,
    ControlTask,
    HazardObservation,
    ObservationSpec,
    OperationMode,
    RecoveryPlan,
    SensorMode,
    StopReason,
    UAVObservation,
)
from src.control.heuristic.navigation import PathNotFoundError
from src.control.heuristic.return_to_base import (
    NoSafeRecoveryPath,
    RecoveryPlanner,
    ReturnToBaseController,
    SystemHoldingController,
)
from src.control.heuristic.tracking import TrackingController, TrackingPhase
from src.control.common.safety import SafetyEnvelope
from src.control.return_path import return_to_base
from src.control.track_orbit import generate_orbit_waypoints, update_orbit_center
from src.schedule.datatypes import GridCoord
from src.utils.storm_avoider import ThreatAssessment, ThreatLevel


class TrackingNavigatorSpy:
    def __init__(self, *, fail=False, fail_on_calls=(), paths=()):
        self.fail = fail
        self.fail_on_calls = set(fail_on_calls)
        self.paths = list(paths)
        self.plan_arguments = []

    @property
    def last_target(self):
        return self.plan_arguments[-1][1]

    def plan_to_standoff(
        self,
        start_pose,
        target,
        radius,
        obstacle_mask,
        r_min,
        planning_map_version=0,
    ):
        self.plan_arguments.append(
            (
                tuple(start_pose),
                tuple(target),
                radius,
                obstacle_mask.copy(),
                r_min,
                planning_map_version,
            )
        )
        if self.fail or len(self.plan_arguments) in self.fail_on_calls:
            raise PathNotFoundError(
                tuple(start_pose),
                "tracking standoff",
                planning_map_version,
            )
        if self.paths:
            return list(self.paths.pop(0))
        goal = (float(target[0]) - radius, float(target[1]))
        heading = math.atan2(goal[1] - start_pose[1], goal[0] - start_pose[0])
        return [tuple(start_pose), (*goal, heading)]


class TrackerSpy:
    def __init__(self, *, turn_rate=0.25, speed=0.75, entry_paths=()):
        self.turn_rate = turn_rate
        self.speed = speed
        self.entry_paths = list(entry_paths)
        self.entry_arguments = []
        self.guidance_arguments = []

    def plan_entry(self, uav_pose, target_position, radius):
        self.entry_arguments.append(
            (tuple(uav_pose), tuple(target_position), radius)
        )
        if self.entry_paths:
            return SimpleNamespace(waypoints=list(self.entry_paths.pop(0)))
        endpoint = (
            float(uav_pose[0]) + 0.1 * math.cos(float(uav_pose[2])),
            float(uav_pose[1]) + 0.1 * math.sin(float(uav_pose[2])),
            float(uav_pose[2]),
        )
        return SimpleNamespace(waypoints=[tuple(uav_pose), endpoint])

    def compute_guidance(
        self,
        uav_pose,
        target_position,
        radius,
        nominal_speed,
        storm_zones=(),
        storm_safety_margin=1.0,
    ):
        self.guidance_arguments.append(
            (
                tuple(uav_pose),
                tuple(target_position),
                radius,
                nominal_speed,
                tuple(storm_zones),
                storm_safety_margin,
            )
        )
        return self.turn_rate, self.speed


class StormAvoiderSpy:
    def __init__(self, level=ThreatLevel.CLEAR, avoidance_path=()):
        self.level = level
        self.avoidance_path = list(avoidance_path)
        self.detect_arguments = []
        self.avoidance_arguments = []

    def detect_threat(
        self,
        uav_pose,
        target_position,
        storms,
        speed_cells_min=0.0,
        dt_min=1.0,
        standoff_radius=1.8,
    ):
        storms = tuple(storms)
        self.detect_arguments.append(
            (
                tuple(uav_pose),
                tuple(target_position),
                storms,
                speed_cells_min,
                dt_min,
                standoff_radius,
            )
        )
        storm = storms[0] if storms else None
        return ThreatAssessment(self.level, storm)

    def plan_avoidance(
        self,
        uav_pose,
        target_position,
        storms,
        r_min,
        *,
        standoff_radius=2.3,
    ):
        self.avoidance_arguments.append(
            (
                tuple(uav_pose),
                tuple(target_position),
                tuple(storms),
                r_min,
                standoff_radius,
            )
        )
        return list(self.avoidance_path)


class RecoveryNavigatorSpy:
    def __init__(self, paths=None, *, failure=None):
        self.paths = paths or {}
        self.failure = failure
        self.plan_arguments = []

    def plan_grid(
        self,
        start,
        goals,
        obstacle_mask,
        r_min,
        planning_map_version=0,
    ):
        goal = min(goals)
        self.plan_arguments.append(
            (
                tuple(start),
                frozenset(goals),
                obstacle_mask.copy(),
                r_min,
                planning_map_version,
            )
        )
        if self.failure is not None:
            raise self.failure
        return list(self.paths[goal])


@pytest.fixture
def action_spec():
    return ActionSpec(-2.0, 2.0, 0.5, 1.0)


@pytest.fixture
def observation():
    arrays = np.zeros((30, 30), dtype=bool)
    contact = ContactObservation(
        contact_id="contact:G1",
        group_id="G1",
        estimated_position=(12.0, 13.0),
        estimated_velocity=(0.1, 0.0),
        source="UAV-reporting",
        observed_at_min=4.0,
        age_min=1.0,
        confidence=0.8,
    )
    return ControlObservation(
        schema_version="control-observation/v1",
        timestamp_min=5.0,
        dt_min=1.0,
        self_state=UAVObservation(
            uav_id="uav-1",
            position=(2.0, 10.0),
            heading_rad=0.0,
            speed_cells_min=0.75,
            remaining_range_cells=100.0,
            control_mode=ControlMode.HEURISTIC,
            control_owner=ControlOwner.HEURISTIC,
            operation_mode=OperationMode.TRACK,
            sensor_mode=SensorMode.OFF,
            safety_intervened=False,
        ),
        local_info=arrays,
        local_value=arrays,
        obstacle_mask=arrays,
        searchable_mask=np.ones((30, 30), dtype=bool),
        planning_obstacle_mask=arrays,
        planning_map_version=1,
        contacts=(contact,),
        hazards=(),
        bases=(),
        shared_uavs=(),
        events=(),
        action_mask=ActionMask(
            (SensorMode.OFF, SensorMode.EO),
            (OperationMode.TRACK, OperationMode.RETURN, OperationMode.HOLDING),
            ("contact:G1",),
        ),
    )


@pytest.fixture
def tracking_components():
    return TrackingNavigatorSpy(), TrackerSpy(), StormAvoiderSpy()


@pytest.fixture
def controller(action_spec, tracking_components):
    navigator, tracker, storm_avoider = tracking_components
    return TrackingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=navigator,
        tracker=tracker,
        storm_avoider=storm_avoider,
        eo_range_cells=2.5,
        standoff_radius_cells=1.8,
        r_min=1.0,
        nominal_speed_cells_min=0.75,
    )


def _start_tracking(controller, observation):
    controller.start_task(
        ControlTask(
            "T1", OperationMode.TRACK, target_contact_id="contact:G1"
        ),
        observation,
    )


def _with_pose(observation, pose, **changes):
    return replace(
        observation,
        self_state=replace(
            observation.self_state,
            position=pose[:2],
            heading_rad=pose[2],
            **changes,
        ),
    )


def _return_observation(observation, *, remaining_range=100.0):
    return replace(
        observation,
        self_state=replace(
            observation.self_state,
            position=(2.0, 5.0),
            heading_rad=0.0,
            speed_cells_min=1.0,
            remaining_range_cells=remaining_range,
            control_owner=ControlOwner.SYSTEM,
            operation_mode=OperationMode.RETURN,
        ),
        contacts=(),
        action_mask=ActionMask(
            (SensorMode.OFF,),
            (OperationMode.RETURN, OperationMode.HOLDING),
            (),
        ),
    )


def _recovery_plan():
    return RecoveryPlan(
        base_id="base-A",
        base_position=(8.0, 5.0),
        reservation_id="reservation-1",
        path=(
            (2.0, 5.0, 0.0),
            (4.0, 5.0, 0.0),
            (8.0, 5.0, 0.0),
        ),
        path_length_cells=6.0,
        reserve_cells=2.0,
        planning_map_version=1,
    )


def _return_controller(action_spec, navigator, released):
    return ReturnToBaseController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=navigator,
        release_reservation=released.append,
        r_min=1.0,
    )


def test_tracking_uses_reported_contact_not_environment_truth(
    controller, observation
):
    _start_tracking(controller, observation)

    decision = controller.act(observation)

    assert controller.navigator.last_target == (12.0, 13.0)
    assert decision.command.target_contact_id == "contact:G1"


def test_tracking_uses_astar_before_lgvf_entry_and_guidance(
    controller, observation, tracking_components
):
    navigator, tracker, _ = tracking_components
    _start_tracking(controller, observation)

    approach = controller.act(observation)

    assert navigator.plan_arguments[-1][2] == 2.5
    assert controller.phase is TrackingPhase.APPROACH_ASTAR
    assert approach.command.sensor_mode is SensorMode.OFF

    near_target = _with_pose(observation, (10.0, 13.0, 0.0))
    guidance = controller.act(near_target)

    assert tracker.entry_arguments[-1][1:] == ((12.0, 13.0), 1.8)
    assert tracker.guidance_arguments[-1][1] == (12.0, 13.0)
    assert controller.phase is TrackingPhase.TRACKING
    assert guidance.command.turn_rate_rad_min == 0.25
    assert guidance.command.speed_cells_min == 0.75
    assert guidance.command.sensor_mode is SensorMode.EO
    assert guidance.command.operation_mode is OperationMode.TRACK


def test_tracking_replans_for_only_a_newer_contact_update(
    controller, observation
):
    _start_tracking(controller, observation)
    controller.act(observation)
    newer = replace(
        observation.contacts[0],
        estimated_position=(15.0, 14.0),
        observed_at_min=5.0,
    )

    controller.act(replace(observation, contacts=(newer,)))

    assert controller.navigator.last_target == (15.0, 14.0)
    assert len(controller.navigator.plan_arguments) == 2

    older = replace(
        newer,
        estimated_position=(25.0, 25.0),
        observed_at_min=4.5,
    )
    controller.act(replace(observation, contacts=(older,)))

    assert controller.target_position == (15.0, 14.0)
    assert len(controller.navigator.plan_arguments) == 2


def test_tracking_replans_an_approach_route_invalidated_by_a_new_map_version(
    action_spec, observation
):
    initial_route = (
        (2.0, 10.0, 0.0),
        (6.0, 10.0, 0.0),
        (9.5, 13.0, 0.0),
    )
    replanned_route = (
        (2.0, 10.0, 0.0),
        (2.0, 6.0, -math.pi / 2.0),
        (10.0, 6.0, 0.0),
        (9.5, 13.0, 0.75),
    )
    navigator = TrackingNavigatorSpy(paths=(initial_route, replanned_route))
    controller = TrackingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=navigator,
        tracker=TrackerSpy(),
        storm_avoider=StormAvoiderSpy(),
    )
    _start_tracking(controller, observation)
    controller.act(observation)
    blocked = np.array(observation.planning_obstacle_mask, copy=True)
    blocked[6, 10] = True
    changed = replace(
        observation,
        planning_obstacle_mask=blocked,
        planning_map_version=2,
    )

    decision = controller.act(changed)

    assert controller.phase is TrackingPhase.APPROACH_ASTAR
    assert controller.planning_map_version == 2
    assert controller.route == replanned_route
    assert navigator.plan_arguments[-1][0] == (2.0, 10.0, 0.0)
    assert navigator.plan_arguments[-1][-1] == 2
    assert decision.command.turn_rate_rad_min < 0.0


def test_tracking_loses_task_when_map_change_blocks_approach_and_replanning_fails(
    action_spec, observation
):
    initial_route = (
        (2.0, 10.0, 0.0),
        (6.0, 10.0, 0.0),
        (9.5, 13.0, 0.0),
    )
    navigator = TrackingNavigatorSpy(paths=(initial_route,), fail_on_calls=(2,))
    controller = TrackingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=navigator,
        tracker=TrackerSpy(),
        storm_avoider=StormAvoiderSpy(),
    )
    _start_tracking(controller, observation)
    controller.act(observation)
    blocked = np.array(observation.planning_obstacle_mask, copy=True)
    blocked[6, 10] = True

    decision = controller.act(
        replace(
            observation,
            planning_obstacle_mask=blocked,
            planning_map_version=2,
        )
    )

    assert controller.phase is TrackingPhase.LOST
    assert [event.event_type for event in decision.events] == ["task_failed"]
    assert len(navigator.plan_arguments) == 2


def test_tracking_replans_an_orbit_entry_invalidated_by_a_new_map_version(
    action_spec, observation
):
    initial_route = ((10.0, 13.0, 0.0), (8.0, 13.0, math.pi))
    replanned_route = ((10.0, 13.0, 0.0), (10.0, 9.0, -math.pi / 2.0))
    tracker = TrackerSpy(entry_paths=(initial_route, replanned_route))
    controller = TrackingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=TrackingNavigatorSpy(),
        tracker=tracker,
        storm_avoider=StormAvoiderSpy(),
    )
    near_target = _with_pose(observation, (10.0, 13.0, 0.0))
    _start_tracking(controller, near_target)
    controller.act(near_target)
    blocked = np.array(near_target.planning_obstacle_mask, copy=True)
    blocked[8, 13] = True

    decision = controller.act(
        replace(
            near_target,
            planning_obstacle_mask=blocked,
            planning_map_version=2,
        )
    )

    assert controller.phase is TrackingPhase.ORBIT_ENTRY
    assert controller.planning_map_version == 2
    assert controller.route == replanned_route
    assert len(tracker.entry_arguments) == 2
    assert decision.command.turn_rate_rad_min < 0.0


def test_tracking_converts_hazard_observations_for_storm_safety(
    controller, observation, tracking_components
):
    _, tracker, storm_avoider = tracking_components
    hazard = HazardObservation(
        hazard_id="storm-1",
        hazard_type="thunderstorm",
        center=(9.0, 13.0),
        half_extent_cells=0.5,
        velocity_cells_min=(0.1, 0.0),
        intensity=0.7,
    )
    near_target = replace(
        _with_pose(observation, (10.0, 13.0, 0.0)), hazards=(hazard,)
    )
    _start_tracking(controller, near_target)

    controller.act(near_target)

    geometry = storm_avoider.detect_arguments[-1][2][0]
    assert not isinstance(geometry, HazardObservation)
    assert geometry.center == (9.0, 13.0)
    assert geometry.half_extent == 0.5
    assert geometry.contains((9.0, 13.0), safety_margin=1.0)
    assert geometry.distance_to_boundary((11.0, 13.0)) == 1.5
    with pytest.raises(FrozenInstanceError):
        geometry.center = (0.0, 0.0)
    assert tracker.guidance_arguments[-1][4] == (geometry,)


def test_tracking_delegates_immediate_storm_threat_to_safe_detour(
    action_spec, observation
):
    start = (10.0, 13.0, 0.0)
    detour = (start, (10.5, 13.5, math.pi / 4.0))
    avoider = StormAvoiderSpy(ThreatLevel.LEVEL_2, detour)
    controller = TrackingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=TrackingNavigatorSpy(),
        tracker=TrackerSpy(),
        storm_avoider=avoider,
    )
    hazard = HazardObservation(
        "storm-1", "thunderstorm", (11.0, 13.0), 0.5, (0.0, 0.0), 0.8
    )
    near_target = replace(_with_pose(observation, start), hazards=(hazard,))
    _start_tracking(controller, near_target)

    decision = controller.act(near_target)

    assert avoider.avoidance_arguments
    assert controller.avoidance_route == detour
    assert decision.command.operation_mode is OperationMode.TRACK
    assert decision.command.sensor_mode is SensorMode.EO


def test_tracking_reports_internal_route_failure(action_spec, observation):
    controller = TrackingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=TrackingNavigatorSpy(fail=True),
        tracker=TrackerSpy(),
        storm_avoider=StormAvoiderSpy(),
    )
    _start_tracking(controller, observation)

    decision = controller.act(observation)

    assert controller.phase is TrackingPhase.LOST
    assert [event.event_type for event in decision.events] == ["task_failed"]
    assert decision.events[0].payload["task_id"] == "T1"


def test_tracking_failure_command_respects_a_restricted_action_mask(
    action_spec, observation
):
    controller = TrackingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        navigator=TrackingNavigatorSpy(fail=True),
        tracker=TrackerSpy(),
        storm_avoider=StormAvoiderSpy(),
    )
    _start_tracking(controller, observation)
    restricted = replace(
        observation,
        action_mask=ActionMask((SensorMode.OFF,), (OperationMode.HOLDING,), ()),
    )

    decision = controller.act(restricted)
    repeated = controller.act(restricted)
    validated = SafetyEnvelope(action_spec).apply(
        decision.command, restricted, restricted.dt_min
    )

    assert validated.applied_command == decision.command
    assert decision.command.operation_mode is OperationMode.HOLDING
    assert decision.command.sensor_mode is SensorMode.OFF
    assert decision.command.target_contact_id is None
    assert [event.event_type for event in decision.events] == ["task_failed"]
    assert repeated.events == ()


def test_tracking_constructor_has_no_ground_truth_input(action_spec):
    with pytest.raises(TypeError):
        TrackingController(
            observation_spec=ObservationSpec("control-observation/v1", 11),
            action_spec=action_spec,
            ship=object(),
        )


def test_recovery_planner_evaluates_non_full_bases_and_sorts_actual_lengths():
    start = (1.0, 1.0, 0.0)
    paths = {
        (7.0, 1.0): [start, (7.0, 1.0, 0.0)],
        (1.0, 7.0): [start, (1.0, 7.0, math.pi / 2.0)],
        (20.0, 1.0): [start, (20.0, 1.0, 0.0)],
    }
    navigator = RecoveryNavigatorSpy(paths)
    bases = (
        BaseObservation("base-B", (1.0, 7.0), 2, 0),
        BaseObservation("base-full", (3.0, 1.0), 1, 1),
        BaseObservation("base-A", (7.0, 1.0), 2, 1),
        BaseObservation("base-far", (20.0, 1.0), 3, 0),
    )

    candidates = RecoveryPlanner(navigator=navigator).evaluate(
        start,
        remaining_range_cells=10.0,
        bases=bases,
        planning_obstacle_mask=np.zeros((30, 30), dtype=bool),
        planning_map_version=9,
        r_min=1.0,
        reserve_cells=2.0,
    )

    assert [candidate.base.base_id for candidate in candidates] == [
        "base-A",
        "base-B",
    ]
    assert [candidate.path_length_cells for candidate in candidates] == [6.0, 6.0]
    assert all(candidate.reserve_cells == 2.0 for candidate in candidates)
    assert all(candidate.planning_map_version == 9 for candidate in candidates)
    assert [min(call[1]) for call in navigator.plan_arguments] == [
        (1.0, 7.0),
        (7.0, 1.0),
        (20.0, 1.0),
    ]
    assert bases[0].reserved_load == 0


def test_recovery_planner_skips_a_base_when_hybrid_astar_has_no_safe_path():
    start = (1.0, 1.0, 0.0)
    failure = PathNotFoundError(start, "grid goals=1", 3)
    navigator = RecoveryNavigatorSpy(failure=failure)

    candidates = RecoveryPlanner(navigator=navigator).evaluate(
        start,
        100.0,
        (BaseObservation("base-A", (8.0, 1.0), 1, 0),),
        np.zeros((12, 12), dtype=bool),
        3,
        1.0,
        2.0,
    )

    assert candidates == ()
    assert len(navigator.plan_arguments) == 1


def test_recovery_planner_rejects_a_path_to_a_different_base():
    start = (1.0, 1.0, 0.0)
    navigator = RecoveryNavigatorSpy(
        {(8.0, 1.0): [start, (4.0, 1.0, 0.0)]}
    )

    candidates = RecoveryPlanner(navigator=navigator).evaluate(
        start,
        100.0,
        (BaseObservation("base-A", (8.0, 1.0), 1, 0),),
        np.zeros((12, 12), dtype=bool),
        3,
        1.0,
        2.0,
    )

    assert candidates == ()


def test_return_requires_a_reserved_recovery_plan(action_spec, observation):
    controller = _return_controller(action_spec, RecoveryNavigatorSpy(), [])
    return_observation = _return_observation(observation)

    with pytest.raises(ValueError, match="RecoveryPlan"):
        controller.start_task(
            ControlTask("R1", OperationMode.RETURN), return_observation
        )
    with pytest.raises(ValueError, match="RETURN"):
        controller.start_task(
            ControlTask(
                "R1", OperationMode.COVERAGE, recovery_plan=_recovery_plan()
            ),
            return_observation,
        )


def test_return_follows_only_the_reserved_plan_and_emits_system_return(
    action_spec, observation
):
    navigator = RecoveryNavigatorSpy()
    controller = _return_controller(action_spec, navigator, [])
    plan = _recovery_plan()
    return_observation = _return_observation(observation)

    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=plan),
        return_observation,
    )
    decision = controller.act(return_observation)

    assert controller.route == plan.path
    assert controller.follower.poses == plan.path
    assert navigator.plan_arguments == []
    assert decision.command.operation_mode is OperationMode.RETURN
    assert decision.command.sensor_mode is SensorMode.OFF
    assert controller.lease_owner is ControlOwner.SYSTEM


def test_return_stop_releases_an_unused_reservation_once(action_spec, observation):
    released = []
    controller = _return_controller(action_spec, RecoveryNavigatorSpy(), released)
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=_recovery_plan()),
        _return_observation(observation),
    )

    controller.stop_task(StopReason.CANCELLED)
    controller.stop_task(StopReason.CANCELLED)

    assert released == ["reservation-1"]


def test_return_arrival_consumes_the_reservation(action_spec, observation):
    released = []
    controller = _return_controller(action_spec, RecoveryNavigatorSpy(), released)
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=_recovery_plan()),
        _return_observation(observation),
    )
    at_base = _with_pose(_return_observation(observation), (8.0, 5.0, 0.0))

    assert controller.is_complete(at_base)
    controller.stop_task(StopReason.COMPLETED)

    assert released == []


def test_return_accepts_an_unblocked_suffix_after_a_map_change(
    action_spec, observation
):
    navigator = RecoveryNavigatorSpy()
    controller = _return_controller(action_spec, navigator, [])
    return_observation = _return_observation(observation)
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=_recovery_plan()),
        return_observation,
    )

    controller.act(replace(return_observation, planning_map_version=2))

    assert controller.planning_map_version == 2
    assert controller.route == _recovery_plan().path
    assert navigator.plan_arguments == []


def test_return_replans_to_only_the_reserved_base_and_retains_reservation(
    action_spec, observation
):
    detour = [
        (2.0, 5.0, 0.0),
        (2.0, 7.0, math.pi / 2.0),
        (8.0, 7.0, 0.0),
        (8.0, 5.0, -math.pi / 2.0),
    ]
    navigator = RecoveryNavigatorSpy({(8.0, 5.0): detour})
    released = []
    controller = _return_controller(action_spec, navigator, released)
    return_observation = _return_observation(observation)
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=_recovery_plan()),
        return_observation,
    )
    blocked = np.array(return_observation.planning_obstacle_mask, copy=True)
    blocked[4, 5] = True
    changed = replace(
        return_observation,
        planning_obstacle_mask=blocked,
        planning_map_version=2,
    )

    decision = controller.act(changed)

    assert navigator.plan_arguments[-1][1] == frozenset({(8.0, 5.0)})
    assert controller.route == tuple(detour)
    assert controller.reservation_id == "reservation-1"
    assert controller.planning_map_version == 2
    assert released == []
    assert decision.command.operation_mode is OperationMode.RETURN


@pytest.mark.parametrize("failure_kind", ["no_path", "range"])
def test_return_replan_failure_keeps_reservation_and_never_executes_old_path(
    action_spec, observation, failure_kind
):
    return_observation = _return_observation(
        observation, remaining_range=5.0 if failure_kind == "range" else 100.0
    )
    if failure_kind == "no_path":
        failure = PathNotFoundError((2.0, 5.0, 0.0), "grid goals=1", 2)
        navigator = RecoveryNavigatorSpy(failure=failure)
    else:
        navigator = RecoveryNavigatorSpy(
            {
                (8.0, 5.0): [
                    (2.0, 5.0, 0.0),
                    (2.0, 9.0, math.pi / 2.0),
                    (8.0, 9.0, 0.0),
                    (8.0, 5.0, -math.pi / 2.0),
                ]
            }
        )
    released = []
    controller = _return_controller(action_spec, navigator, released)
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=_recovery_plan()),
        return_observation,
    )
    old_follower = controller.follower
    old_route = controller.route
    blocked = np.array(return_observation.planning_obstacle_mask, copy=True)
    blocked[4, 5] = True
    changed = replace(
        return_observation,
        planning_obstacle_mask=blocked,
        planning_map_version=2,
    )

    with pytest.raises(NoSafeRecoveryPath, match="base-A"):
        controller.act(changed)

    assert navigator.plan_arguments[-1][1] == frozenset({(8.0, 5.0)})
    assert controller.follower is old_follower
    assert controller.follower.index == 0
    assert controller.route == old_route
    assert controller.planning_map_version == 1
    assert controller.reservation_id == "reservation-1"
    assert released == []


@pytest.mark.parametrize(
    "replanned",
    [
        [],
        [
            (2.0, 5.0, 0.0),
            (2.0, 7.0, math.pi / 2.0),
            (6.0, 7.0, 0.0),
        ],
    ],
    ids=["empty", "different-base"],
)
def test_return_rejects_an_invalid_replan_without_replacing_the_old_route(
    action_spec, observation, replanned
):
    navigator = RecoveryNavigatorSpy({(8.0, 5.0): replanned})
    controller = _return_controller(action_spec, navigator, [])
    return_observation = _return_observation(observation)
    controller.start_task(
        ControlTask("R1", OperationMode.RETURN, recovery_plan=_recovery_plan()),
        return_observation,
    )
    old_follower = controller.follower
    old_route = controller.route
    blocked = np.array(return_observation.planning_obstacle_mask, copy=True)
    blocked[4, 5] = True
    changed = replace(
        return_observation,
        planning_obstacle_mask=blocked,
        planning_map_version=2,
    )

    with pytest.raises(NoSafeRecoveryPath, match="base-A"):
        controller.act(changed)

    assert controller.follower is old_follower
    assert controller.route == old_route
    assert controller.planning_map_version == 1
    assert controller.reservation_id == "reservation-1"


def test_system_holding_emits_safety_checked_fixed_wing_orbit_until_stopped(
    action_spec, observation
):
    tracker = TrackerSpy(turn_rate=0.0, speed=1.0)
    controller = SystemHoldingController(
        observation_spec=ObservationSpec("control-observation/v1", 11),
        action_spec=action_spec,
        tracker=tracker,
        orbit_radius_cells=2.0,
        nominal_speed_cells_min=1.0,
    )
    holding = replace(
        _with_pose(_return_observation(observation), (10.0, 10.0, 0.0)),
        self_state=replace(
            _with_pose(
                _return_observation(observation), (10.0, 10.0, 0.0)
            ).self_state,
            operation_mode=OperationMode.HOLDING,
        ),
    )
    blocked = np.array(holding.planning_obstacle_mask, copy=True)
    blocked[11, 10] = True
    holding = replace(holding, planning_obstacle_mask=blocked)
    controller.start_task(ControlTask("H1", OperationMode.HOLDING), holding)

    first = controller.act(holding)
    second = controller.act(holding)

    assert first.command.operation_mode is OperationMode.HOLDING
    assert first.command.sensor_mode is SensorMode.OFF
    assert first.command.speed_cells_min == action_spec.min_speed_cells_min
    assert not controller._safety._motion_blocked(
        first.command, holding, holding.dt_min
    )
    assert second.command.operation_mode is OperationMode.HOLDING
    assert controller.lease_owner is ControlOwner.SYSTEM
    assert not controller.is_complete(holding)
    assert tracker.guidance_arguments[0][1] == (10.0, 12.0)
    assert len(tracker.guidance_arguments) == 2


def test_legacy_orbit_wrappers_warn_and_preserve_grid_coordinate_results():
    with pytest.warns(DeprecationWarning):
        waypoints = generate_orbit_waypoints(
            GridCoord(10, 10), standoff_cells=3.0, num_points=4
        )
    with pytest.warns(DeprecationWarning):
        shifted = update_orbit_center(waypoints, (1.0, -2.0))

    assert all(isinstance(point, GridCoord) for point in waypoints)
    assert shifted == [
        GridCoord(point.col + 1, point.row - 2) for point in waypoints
    ]


def test_legacy_return_wrapper_warns_and_preserves_endpoint_shape():
    current = GridCoord(2, 3)
    base = GridCoord(8, 9)

    with pytest.warns(DeprecationWarning):
        path = return_to_base(current, base)

    assert path == [current, base]

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from scripts.evaluate_mixed_maritime import _FixtureGateway
from scripts.replay_restoration_scenarios import build_scenario
from src.control.common.contracts import ControlOwner, ControlTask, OperationMode
from src.control.heuristic.return_to_base import NoSafeRecoveryPath
from src.env.obstacle import Thunderstorm
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox


def _ready_weather_engine():
    engine = build_scenario("weather-replan", seed=42, transport="fixture")
    engine.step()
    engine.step()
    return engine


def test_weather_replan_changes_controller_route_and_preserves_unaffected_task():
    engine = _ready_weather_engine()
    controlled = [
        (uav, engine.control_coordinator.route_snapshot(uav.id))
        for uav in engine.uavs
        if engine.control_coordinator.route_snapshot(uav.id).route.status == "ready"
        and engine.control_coordinator.route_snapshot(uav.id).route.task_type == "coverage"
    ]
    affected, before = next(
        (uav, snapshot)
        for uav, snapshot in controlled
        if any(
            point[0] > 8.0
            and math.dist(uav.float_position, point[:2]) > 3.0
            for point in snapshot.route.route[snapshot.route.next_index:]
        )
    )
    unaffected, unaffected_before = next(
        (uav, snapshot)
        for uav, snapshot in controlled
        if uav.id != affected.id
        and not engine.obstacle_avoider.path_conflicts(
            snapshot.route.route[snapshot.route.next_index:],
            engine.obstacle_mask,
        )
    )
    blocked_point = next(
        point
        for point in before.route.route[before.route.next_index:]
        if point[0] > 8.0 and math.dist(affected.float_position, point[:2]) > 3.0
    )
    storm = Thunderstorm(
        blocked_point[:2], 1, move_vector=(0.0, 0.0), lifetime=-1.0,
        id="storm-t12-weather",
    )
    old_revision = before.route.route_revision
    route_start_position = affected.float_position

    engine.obstacles.append(storm)
    engine._update_obstacles()
    engine.allocator.sm.current_time = 3.0
    engine._step_controlled_uav(affected, 3.0)

    after = engine.control_coordinator.route_snapshot(affected.id)
    unaffected_after = engine.control_coordinator.route_snapshot(unaffected.id)
    assert after.route.route_revision > old_revision
    assert after.route.planning_map_version == engine.allocator.sm.obstacle_version
    assert after.route.route[0][:2] == pytest.approx(route_start_position)
    assert engine.obstacle_avoider.is_path_safe(after.route.route, engine.obstacle_mask)
    assert after.route.task_id == before.route.task_id
    assert unaffected_after.route.task_id == unaffected_before.route.task_id


def test_path_conflict_event_reaches_controller_and_causes_real_replan():
    engine = _ready_weather_engine()
    engine._detect_and_resolve_path_conflicts(3.0)
    conflict_events = [
        event for event in engine.allocator.sm.get_recent_events(0.0)
        if event["type"] == "route_blocked"
        and event["data"].get("reason") == "path_conflict"
    ]
    assert conflict_events
    yielding_id = next(
        event["data"]["uav_id"]
        for event in conflict_events
        if engine.control_coordinator.active_task(event["data"]["uav_id"]) is not None
        and engine.control_coordinator.active_task(event["data"]["uav_id"]).task_type
        is OperationMode.COVERAGE
    )
    before = engine.control_coordinator.route_snapshot(yielding_id)
    route_start_position = next(
        uav for uav in engine.uavs if uav.id == yielding_id
    ).float_position
    engine.allocator.sm.current_time = 3.0
    engine._step_controlled_uav(
        next(uav for uav in engine.uavs if uav.id == yielding_id),
        3.0,
    )
    after = engine.control_coordinator.route_snapshot(yielding_id)

    assert after.generation == before.generation
    assert after.route.task_id == before.route.task_id
    assert after.route.route_revision > before.route.route_revision
    assert after.route.route[0][:2] == pytest.approx(route_start_position)


def _recovery_engine() -> SimulationEngine:
    config = ConfigLoader.load()
    config = replace(
        config,
        environment=replace(
            config.environment,
            base_count=1,
            base_capacity=1,
            island_count_min=0,
            island_count_max=0,
            thunderstorm_count_min=0,
            thunderstorm_count_max=0,
        ),
        uav=replace(config.uav, count_max=2),
    )
    return SimulationEngine(
        config,
        seed=42,
        llm_gateway=_FixtureGateway(),
        episode_id="t12-base-recovery",
    )


def test_base_recovery_uses_controlled_return_refuel_reset_and_redispatch():
    engine = _recovery_engine()
    uav = engine.uavs[0]
    uav._col, uav._row = 15.0, 15.0
    engine.allocator.sm.update_uav_status(uav.id, "idle", uav.position)
    task = ControlTask(
        "coverage:t12-recovery",
        OperationMode.COVERAGE,
        region_bbox=BBox(15, 15, 19, 19),
    )
    engine.control_coordinator.start_work(
        uav.id, sortie_number=1, current_time=0.0, dt_min=1.0, task=task,
    )
    uav.fuel_remaining_pct = 0.8
    before_position = uav.float_position

    assert engine._maybe_revoke_for_range(uav, 0.0, force=True)
    assert uav.status == "returning"
    assert engine.control_coordinator.current_lease(uav.id).owner is ControlOwner.SYSTEM
    assert engine._return_base_by_uav[uav.id] is engine.base

    arrival_time = None
    for current_time in range(1, 160):
        engine.allocator.sm.current_time = float(current_time)
        engine._step_controlled_uav(uav, float(current_time))
        if uav.status == "refueling":
            arrival_time = current_time
            break
    assert arrival_time is not None
    assert uav.float_position != pytest.approx(before_position)
    fuel_at_arrival = uav.fuel_remaining_pct
    engine._process_refuelling(float(arrival_time))
    assert uav.status == "refueling"
    assert uav.fuel_remaining_pct == pytest.approx(fuel_at_arrival)

    for current_time in range(arrival_time + 1, arrival_time + 4):
        engine._process_refuelling(float(current_time))
    assert uav.status == "idle"
    assert uav.fuel_remaining_pct == pytest.approx(1.0)
    assert engine.base.refuel_count == 1

    previous_episode = engine.episode_id
    engine.reset(seed=43)
    assert engine.episode_id != previous_episode
    assert engine.clock.time == 0
    assert all(item.status == "idle" for item in engine.uavs)
    result = engine.step()
    assert any(
        event["type"] == "mission_assignment_committed"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )
    assert result["trigger_type"] in {"heavy", "light"}


def test_full_recovery_base_reports_failure_without_instant_refuel():
    engine = _recovery_engine()
    first, second = engine.uavs

    for index, uav in enumerate((first, second), start=1):
        uav._col, uav._row = 15.0, 15.0
        uav.fuel_remaining_pct = 0.8
        engine.allocator.sm.update_uav_status(uav.id, "idle", uav.position)
        task = ControlTask(
            f"coverage:t12-capacity-{index}",
            OperationMode.COVERAGE,
            region_bbox=BBox(15, 15, 19, 19),
        )
        engine.control_coordinator.start_work(
            uav.id, sortie_number=1, current_time=0.0, dt_min=1.0, task=task,
        )

    assert engine._maybe_revoke_for_range(first, 0.0, force=True)
    assert engine._return_base_by_uav[first.id] is engine.base

    engine.allocator.sm.current_time = 1.0
    engine._step_controlled_uav(second, 1.0)

    assert engine._emergency_failures[second.id] == "no_safe_recovery_path"
    assert second.status == "idle"
    assert second.fuel_remaining_pct == pytest.approx(0.8)
    assert second.id not in engine._return_base_by_uav
    assert any(
        event["type"] == "no_safe_recovery_path"
        and event["data"]["uav_id"] == second.id
        for event in engine.allocator.sm.get_recent_events(0.0)
    )
    assert not any(
        event["type"] == "return_reserved"
        and event["data"]["uav_id"] == second.id
        for event in engine.allocator.sm.get_recent_events(0.0)
    )


def test_low_fuel_warning_is_emitted_before_controlled_return():
    engine = _ready_weather_engine()
    uav = next(
        item for item in engine.uavs
        if engine.control_coordinator.active_task(item.id) is not None
    )
    uav.fuel_remaining_pct = 0.085
    uav.fuel_warning_sent = False

    engine.step()

    warnings = [
        event for event in engine.allocator.sm.get_recent_events(0.0)
        if event["type"] == "uav_fuel_low_warning"
        and event["data"]["uav_id"] == uav.id
    ]
    assert warnings
    assert warnings[-1]["data"]["fuel_pct"] == pytest.approx(0.085)
    assert uav.status in {"returning", "refueling", "holding"}
    assert any(
        event["type"] == "return_reserved"
        and event["data"]["uav_id"] == uav.id
        for event in engine.allocator.sm.get_recent_events(0.0)
    )


def test_v07_real_eo_samples_classify_then_handoff_and_eo_lock():
    engine = build_scenario("V07", seed=42, transport="fixture")
    engine.step()
    probe = engine.allocator.sm.get_probe_sessions()[0]
    task = engine.control_coordinator.active_task(probe.uav_id)
    assert task is not None and task.probe_id == probe.probe_id
    uav = next(item for item in engine.uavs if item.id == probe.uav_id)
    contact = engine.allocator.sm.contacts.snapshot(probe.contact_id)
    ship = next(
        item for item in engine.ships
        if probe.contact_id in engine._vessel_contact_ids[item.id]
    )
    target = ship.float_position
    uav._col, uav._row = target[0] - 1.0, target[1]
    uav.heading_rad = 0.0
    engine.allocator.sm.update_uav_status(
        uav.id, uav.status, uav.position, assigned_region_id=task.task_id,
    )

    assessment_time = None
    for current_time in range(2, 30):
        now = float(current_time)
        engine.allocator.sm.current_time = now
        engine._update_ships(now)
        engine._refresh_ais_signals(now)
        engine._expire_contacts(now)
        engine._step_controlled_uav(uav, now)
        engine._update_sensors_and_detections(now)
        engine._advance_probe_sessions(now)
        if any(
            event["type"] == "assessment_applied"
            and event["data"].get("vessel_class") == "type_ii"
            for event in engine.allocator.sm.get_recent_events(0.0)
        ):
            assessment_time = now
            break

    assert assessment_time is not None
    samples = engine.allocator.sm.contacts.snapshot(probe.contact_id).samples
    assert sum(sample.source == "eo" and sample.source_id == uav.id for sample in samples) >= 8
    assert any(
        event["type"] == "assessment_applied"
        and event["data"]["vessel_class"] == "type_ii"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )

    engine.allocator.sm.current_time = assessment_time + 1.0
    engine._step_controlled_uav(uav, assessment_time + 1.0)
    track_task = engine.control_coordinator.active_task(uav.id)
    assert track_task is not None
    assert track_task.task_type is OperationMode.TRACK
    assert uav.target_group_id == probe.contact_id

    source_report = engine.allocator.sm.get_target_report(probe.contact_id)
    assert source_report is not None
    attempt = engine._require_handoff(uav, source_report, assessment_time + 2.0)
    assert attempt is not None
    assert attempt.state == "required"
    assert attempt.required_at_min == pytest.approx(assessment_time + 2.0)
    successor = next(item for item in engine.uavs if item.id != uav.id)
    committed = engine.handoff_manager.commit_assignment(
        attempt.handoff_id, successor.id, assessment_time + 3.0,
    )
    assert committed.state == "pending"
    assert committed.assignment_committed_at_min == pytest.approx(assessment_time + 3.0)

    successor_target = engine.allocator.sm.contact_position(
        probe.contact_id, assessment_time + 3.0,
    )
    assert successor_target is not None
    successor._col, successor._row = successor_target[0] - 1.0, successor_target[1]
    successor.heading_rad = 0.0
    successor_task = ControlTask(
        f"handoff-track:{probe.contact_id}",
        OperationMode.TRACK,
        target_contact_id=probe.contact_id,
    )
    engine.control_coordinator.assign_task(
        successor.id, successor_task, current_time=assessment_time + 3.0,
    )
    engine.allocator.sm.current_time = assessment_time + 4.0
    engine._update_ships(assessment_time + 4.0)
    engine._refresh_ais_signals(assessment_time + 4.0)
    engine._expire_contacts(assessment_time + 4.0)
    engine._step_controlled_uav(successor, assessment_time + 4.0)
    engine._update_sensors_and_detections(assessment_time + 4.0)
    assert successor.last_applied_command is not None
    assert successor.last_applied_command.operation_mode is OperationMode.TRACK
    assert successor.last_applied_command.sensor_mode.value == "eo"
    final = engine.handoff_manager.attempts()[0]
    assert final.state == "succeeded"
    assert final.eo_lock_acquired_at_min == pytest.approx(assessment_time + 4.0)
    assert any(
        event["type"] == "handoff_eo_lock_acquired"
        and event["data"]["handoff_id"] == attempt.handoff_id
        for event in engine.allocator.sm.get_recent_events(0.0)
    )

"""Contact lifecycle regressions through real control validation and execution."""
from dataclasses import replace
import math

import numpy as np
import pytest

from src.control.common.contracts import ControlOwner, ControlTask, OperationMode
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord
from tests.mission.test_contact_store import ais, visual


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "t06-offline-fixture")
    config = ConfigLoader.load()
    population = replace(
        config.ship.population,
        type_i_ratio=1.0,
        type_ii_ratio=0.0,
    )
    config.ship = replace(
        config.ship,
        population=population,
        type_ii_ais_on_probability=0.0,
    )
    engine = SimulationEngine(config, seed=41)
    engine.ships = []
    engine.obstacles = []
    engine.obstacle_mask = np.zeros(config.grid.resolution, dtype=bool)
    engine.allocator.sm.set_environment_obstacles([], engine.obstacle_mask)
    monkeypatch.setattr(engine, "_update_obstacles", lambda: None)
    # No model decisions are needed to exercise contact/control lifecycle.
    monkeypatch.setattr(engine.allocator, "step", lambda t: {"trigger_type": "none"})
    return engine


@pytest.fixture
def stage_engine(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "t05-stage-fixture")
    engine = SimulationEngine(ConfigLoader.load(), seed=43)
    monkeypatch.setattr(engine, "_update_obstacles", lambda: None)
    monkeypatch.setattr(engine.allocator, "step", lambda t: {"trigger_type": "none"})
    return engine


def test_sar_detection_and_contact_loss_publish_type_ii_stage_changes(stage_engine):
    ship = next(item for item in stage_engine.ships if item.vessel_class == "type_ii")
    uav = stage_engine.uavs[0]

    contact_id = stage_engine._handle_detection(uav, ship, 1.0)
    detected = stage_engine.surveillance_stages.snapshot(ship.id)
    assert detected.stage == "detected"
    assert detected.revision == 1
    assert any(
        event["type"] == "surveillance_stage_changed"
        and event["data"]["vessel_id"] == ship.id
        and event["data"]["stage"] == "detected"
        for event in stage_engine.allocator.sm.get_recent_events(0)
    )

    stage_engine._release_target_group(contact_id, 2.0, "target_lost")
    cleared = stage_engine.surveillance_stages.snapshot(ship.id)
    assert cleared.stage == "undetected"
    assert cleared.revision == 2


def start_tracking(engine, uav, cid, *, execute=True, reserve=True, current_time=.1):
    sm = engine.allocator.sm
    uav._col, uav._row = 12., 10.
    uav.heading_rad = math.pi / 2
    if reserve:
        sm.contacts.reserve(sm.resolve_contact_id(cid), uav.id, None)
    task = ControlTask(f"track:{cid}", OperationMode.TRACK, target_contact_id=cid)
    engine.control_coordinator.start_work(
        uav.id, sortie_number=1, current_time=current_time, dt_min=.1, task=task)
    if execute:
        tick = engine.control_coordinator.step_uav(uav, current_time=current_time, dt_min=.1)
        engine._record_control_tick(uav, tick)
        assert tick.execution.applied_command.operation_mode is OperationMode.TRACK
    return task


@pytest.mark.parametrize("legacy", [False, True])
def test_contact_merge_migrates_active_alias_task_and_track_bindings(engine, legacy):
    sm = engine.allocator.sm
    if legacy:
        sm.record_target_observation("legacy-visual", GridCoord(10, 10), "UAV-1", 0)
        vid = sm.resolve_contact_id("legacy-visual")
    else:
        vid = sm.contacts.ingest_visual(visual())
    aid = sm.contacts.ingest_ais(ais(), 0)
    uav = engine.uavs[0]
    task = start_tracking(engine, uav, "legacy-visual" if legacy else vid)
    track = sm.get_track_region_for_group(vid if not legacy else "legacy-visual")
    sm.contacts.ingest_ais(ais(1), 1)
    engine._publish_contact_events(1)

    assert sm.contacts.resolve(vid) == aid
    assert sm.get_track_regions() == [track]
    assert track.target_group_id == aid
    assert track.assigned_uav_id == uav.id
    assert uav.target_group_id == sm.get_uav(uav.id).target_group_id == aid
    assert sm.get_track_region_for_group(vid) is track
    active = engine.control_coordinator.active_task(uav.id)
    assert active.task_id == task.task_id
    assert active.target_contact_id == aid
    tick = engine.control_coordinator.step_uav(uav, current_time=1.1, dt_min=.1)
    engine._record_control_tick(uav, tick)
    assert aid in tick.observation.action_mask.target_contact_ids
    assert tick.execution.applied_command.target_contact_id == aid
    assert sm.get_track_regions() == [track]
    assert sm.contacts.snapshot(aid).assigned_uav_id == uav.id
    assert engine.control_coordinator.current_lease(uav.id).owner is ControlOwner.HEURISTIC
    if legacy:
        assert sm.resolve_contact_id("legacy-visual") == aid
        assert sm.get_track_region_for_group("legacy-visual") is track


@pytest.mark.parametrize("reserve,execute_survivor", [(False, True), (True, True), (True, False), (False, False)])
@pytest.mark.parametrize("execute_duplicate", [False, True])
def test_contact_merge_cancels_duplicate_task_without_releasing_survivor(
        engine, reserve, execute_survivor, execute_duplicate):
    sm = engine.allocator.sm
    vid = sm.contacts.ingest_visual(visual())
    aid = sm.contacts.ingest_ais(ais(), 0)
    duplicate, survivor = engine.uavs[:2]
    start_tracking(engine, duplicate, vid, reserve=reserve, execute=execute_duplicate)
    start_tracking(engine, survivor, aid, reserve=reserve, execute=execute_survivor)
    retained_track = sm.get_track_region_for_group(aid)
    sm.contacts.ingest_ais(ais(1), 1)
    engine._publish_contact_events(1)

    # Without reservations, an already executing alias can be the survivor.
    if not reserve and execute_duplicate and not execute_survivor:
        duplicate, survivor = survivor, duplicate
        retained_track = sm.get_track_region_for_group(aid)
    assert sm.get_track_regions() == ([retained_track] if retained_track else [])
    for uav in (duplicate, survivor):
        tick = engine.control_coordinator.step_uav(uav, current_time=1.1, dt_min=.1)
        engine._record_control_tick(uav, tick)
    assert duplicate.target_group_id is None
    assert sm.get_uav(duplicate.id).assigned_region_id is None
    assert engine.control_coordinator.active_task(duplicate.id).task_type is OperationMode.HOLDING
    assert survivor.target_group_id == aid
    active_track = sm.get_track_region_for_group(aid)
    assert active_track.assigned_uav_id == survivor.id
    if retained_track is not None:
        assert active_track is retained_track
    assert sm.contacts.snapshot(aid).assigned_uav_id == survivor.id
    assert len(sm.get_track_regions()) == 1
    events = sm.get_recent_events(0)
    assert any(e["type"] == "duplicate_task_cancelled" for e in events)
    assert any(e["type"] == "contact_merged" for e in events)


@pytest.mark.parametrize("reuse_task_id", [False, True])
@pytest.mark.parametrize("reserve", [False, True])
def test_queued_duplicate_cancellation_ignores_replacement_canonical_task(
        engine, reuse_task_id, reserve):
    sm = engine.allocator.sm
    coordinator = engine.control_coordinator
    vid = sm.contacts.ingest_visual(visual())
    aid = sm.contacts.ingest_ais(ais(), 0)
    duplicate, survivor = engine.uavs[:2]
    old_task = start_tracking(engine, duplicate, vid, reserve=reserve, execute=False)
    start_tracking(engine, survivor, aid, reserve=reserve, execute=False)
    old_lease = coordinator.current_lease(duplicate.id)
    sm.contacts.ingest_ais(ais(1), 1)
    engine._publish_contact_events(1)
    assert sm.contacts.resolve(vid) == aid
    assert sm.contacts.snapshot(aid).assigned_uav_id == survivor.id

    # Release the surviving reservation and reassign before the queued event
    # reaches the duplicate controller's first tick. IDs may be reused.
    sm.release_contact_reservation(aid, survivor.id, 1.05, "handoff")
    sm.contacts.reserve(aid, duplicate.id, None)
    replacement = ControlTask(
        old_task.task_id if reuse_task_id else f"replacement:{aid}",
        OperationMode.TRACK, target_contact_id=aid)
    new_lease = coordinator.assign_task(duplicate.id, replacement, current_time=1.05)
    assert new_lease.generation > old_lease.generation
    for t in (1.1, 1.2):
        tick = coordinator.step_uav(duplicate, current_time=t, dt_min=.1)
        engine._record_control_tick(duplicate, tick)
        assert tick.execution.applied_command.operation_mode is OperationMode.TRACK
        assert coordinator.active_task(duplicate.id) == replacement
        assert coordinator.current_lease(duplicate.id) == new_lease
        assert sm.contacts.snapshot(aid).assigned_uav_id == duplicate.id
        assert sm.get_track_region_for_group(aid).assigned_uav_id == duplicate.id


@pytest.mark.parametrize("exit_kind", ["task_failed", "route_failure", "return", "release"])
@pytest.mark.parametrize("execute", [False, True])
def test_merged_legacy_reservation_is_released_once_on_task_exit(engine, exit_kind, execute):
    from src.control.common.contracts import RecoveryPlan

    sm = engine.allocator.sm
    vid = sm.contacts.ingest_visual(visual())
    aid = sm.contacts.ingest_ais(ais(), 0)
    uav = engine.uavs[0]
    start_tracking(engine, uav, vid, reserve=False, execute=execute)
    sm.contacts.ingest_ais(ais(1), 1)
    engine._publish_contact_events(1)
    assert sm.contacts.snapshot(aid).assigned_uav_id == uav.id
    # A region can already have been retired before the controller exits.
    track = sm.get_track_region_for_group(aid)
    if track:
        sm.release_track_region(track.id, create_marker=False)
    coordinator = engine.control_coordinator
    if exit_kind == "task_failed":
        engine._queue_control_event("task_failed", uav.id, 1.1, {"contact_id": vid})
    elif exit_kind == "route_failure":
        # No standoff goal is reachable, but the UAV can still hold safely.
        uav._col = 18.
        blocked = np.zeros(engine.config.grid.resolution, dtype=bool)
        blocked[7:14, 7:14] = True
        sm.set_environment_obstacles([], blocked)
    elif exit_kind == "return":
        start = uav.pose
        end = (start[0], start[1] + 1, start[2])
        coordinator.revoke_for_return(uav.id, RecoveryPlan(
            base_id="B1", base_position=end[:2], reservation_id="return-test",
            path=(start, end), path_length_cells=1, reserve_cells=.5,
            planning_map_version=sm.obstacle_version), current_time=1.1)
        engine._prepare_return_state(uav, 1.1)
    else:
        engine._release_target_group(vid, 1.1, "target_lost")
    for t in (1.2, 1.3):
        tick = coordinator.step_uav(uav, current_time=t, dt_min=.1)
        engine._record_control_tick(uav, tick)
        assert tick.execution.applied_command.operation_mode is not OperationMode.TRACK
        if exit_kind == "route_failure" and t == 1.2:
            assert [e.event_type for e in tick.emitted_events] == ["task_failed"]
    assert sm.contacts.snapshot(aid).assigned_uav_id is None
    releases = [e for e in sm.contacts.events
                if e["type"] == "contact_released" and e["contact_id"] == aid]
    assert len(releases) == 1
    other = sm.contacts.ingest_ais(ais(2, (20, 20), mmsi="987654321"), 2)
    sm.contacts.reserve(other, uav.id, None)
    assert sm.contacts.snapshot(other).assigned_uav_id == uav.id


def test_return_cleanup_of_duplicate_alias_preserves_canonical_owner(engine):
    sm = engine.allocator.sm
    vid = sm.contacts.ingest_visual(visual())
    aid = sm.contacts.ingest_ais(ais(), 0)
    duplicate, survivor = engine.uavs[:2]
    start_tracking(engine, duplicate, vid)
    start_tracking(engine, survivor, aid)
    sm.contacts.ingest_ais(ais(1), 1)
    # The returning entity still carries an alias when the store has merged.
    engine._prepare_return_state(duplicate, 1.1)
    assert sm.contacts.snapshot(aid).assigned_uav_id == survivor.id
    assert not any(e["type"] == "contact_released" for e in sm.contacts.events)


def test_retained_alias_command_records_canonical_entity_target(engine):
    sm = engine.allocator.sm
    vid = sm.contacts.ingest_visual(visual())
    sm.contacts.ingest_ais(ais(), 0)
    uav = engine.uavs[0]
    start_tracking(engine, uav, vid)
    aid = sm.contacts.ingest_ais(ais(1), 1)
    # A controller may finish an existing alias command before task migration.
    sm.publish_contact_events()
    tick = engine.control_coordinator.step_uav(uav, current_time=1.1, dt_min=.1)
    assert tick.execution.applied_command.target_contact_id == vid
    engine._record_control_tick(uav, tick)
    assert uav.target_group_id == sm.get_uav(uav.id).target_group_id == aid


def test_merge_into_cleared_contact_releases_alias_tracking(engine):
    from src.mission.contracts import Assessment

    sm = engine.allocator.sm
    aid = sm.contacts.ingest_ais(ais(position=(20, 10)), 0)
    sm.contacts.ingest_visual(visual("clear-1", 0, (20, 10)))
    sm.contacts.ingest_visual(visual("clear-2", 1, (20, 10)))
    sm.contacts.reserve(aid, "UAV-2", "P-clear")
    c = sm.contacts.snapshot(aid)
    sm.contacts.apply_assessment(Assessment(
        "A1", aid, "P-clear", c.revision, 1, "civilian", .9,
        ("clear-1", "clear-2"), ("validated",), (), "call1"))
    engine._publish_contact_events(1)
    vid = sm.contacts.ingest_visual(visual("new-visual", 2, (10, 10)))
    uav = engine.uavs[0]
    start_tracking(engine, uav, vid, current_time=2.1)
    sm.contacts.ingest_ais(ais(3, (10, 10)), 3)
    sm.contacts.ingest_ais(ais(4, (10, 10)), 4)
    engine._publish_contact_events(4)

    tick = engine.control_coordinator.step_uav(uav, current_time=4.1, dt_min=.1)
    engine._record_control_tick(uav, tick)
    assert sm.contacts.snapshot(vid).state == "cleared"
    assert sm.contacts.snapshot(vid).assigned_uav_id is None
    assert sm.get_track_regions() == []
    assert uav.target_group_id is None
    assert engine.control_coordinator.active_task(uav.id).task_type is OperationMode.HOLDING


@pytest.mark.parametrize("execute", [False, True])
def test_stale_contact_is_released_before_first_controller_tick_past_limit(engine, execute):
    sm = engine.allocator.sm
    cid = sm.contacts.ingest_visual(visual())
    uav = engine.uavs[0]
    start_tracking(engine, uav, cid, execute=execute)
    engine.clock.time = engine.config.mission.contact.stale_after_min - .01
    engine.clock.dt_min = .02
    engine.step()

    assert sm.contacts.snapshot(cid).state == "lost"
    assert sm.contacts.snapshot(cid).assigned_uav_id is None
    assert sm.get_track_regions() == []
    assert uav.target_group_id is None
    assert sm.get_uav(uav.id).target_group_id is None
    assert engine.control_coordinator.active_task(uav.id).task_type is OperationMode.HOLDING
    events = sm.get_recent_events(0)
    assert any(e["type"] == "target_lost" for e in events)
    assert not any(e["type"] in {"control_fault", "emergency_failure", "task_failed"}
                   for e in events)
    assert not engine._emergency_failures

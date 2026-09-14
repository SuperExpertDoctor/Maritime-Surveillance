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
    config.ship = replace(config.ship, target_ship_count=config.ship.initial_ship_count,
                          target_ais_on_probability=0)
    engine = SimulationEngine(config, seed=41)
    engine.ships = []
    engine.obstacles = []
    engine.obstacle_mask = np.zeros(config.grid.resolution, dtype=bool)
    engine.allocator.sm.set_environment_obstacles([], engine.obstacle_mask)
    monkeypatch.setattr(engine, "_update_obstacles", lambda: None)
    # No model decisions are needed to exercise contact/control lifecycle.
    monkeypatch.setattr(engine.allocator, "step", lambda t: {"trigger_type": "none"})
    return engine


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


@pytest.mark.parametrize("reserve,execute_survivor", [(False, True), (True, True), (True, False)])
def test_contact_merge_cancels_duplicate_task_without_releasing_survivor(engine, reserve, execute_survivor):
    sm = engine.allocator.sm
    vid = sm.contacts.ingest_visual(visual())
    aid = sm.contacts.ingest_ais(ais(), 0)
    duplicate, survivor = engine.uavs[:2]
    start_tracking(engine, duplicate, vid, reserve=reserve)
    start_tracking(engine, survivor, aid, reserve=reserve, execute=execute_survivor)
    retained_track = sm.get_track_region_for_group(aid)
    sm.contacts.ingest_ais(ais(1), 1)
    engine._publish_contact_events(1)

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

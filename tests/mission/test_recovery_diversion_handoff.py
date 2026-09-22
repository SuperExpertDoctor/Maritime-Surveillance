from dataclasses import replace
import math

import numpy as np
import pytest

from tests.mission.test_feature_control_integration import _recovery_engine
from tests.mission.test_task_catalog import _contact, CandidateSource
from src.control.common.contracts import ControlTask, OperationMode, RecoveryPlan
from src.control.heuristic.navigation import AStarNavigator
from src.env.base_station import BaseStation
from src.mission.handoff import HandoffManager
from src.mission.task_catalog import TaskCatalog
from src.schedule.datatypes import GridCoord, BBox


def _returning_engine(monkeypatch):
    engine = _recovery_engine()
    uav = engine.uavs[0]
    uav._col, uav._row, uav.heading_rad = 10., 10., 0.
    engine.bases = [BaseStation(GridCoord(14, 10), refuel_time_min=3, capacity=1, base_id='old'),
                    BaseStation(GridCoord(10, 14), refuel_time_min=3, capacity=1, base_id='alternate')]
    engine.obstacle_mask[:] = False
    engine.allocator.sm.obstacle_mask[:] = False
    plan = RecoveryPlan('old', (14., 10.), 'original', ((10.,10.,0.), (14.,10.,0.)), 4., 1., engine.allocator.sm.obstacle_version)
    engine.control_coordinator.start_work(uav.id, sortie_number=1, current_time=0., dt_min=1., task=ControlTask('search', OperationMode.COVERAGE, region_bbox=BBox(10,10,15,15)))
    engine.control_coordinator.revoke_for_return(uav.id, plan, current_time=0.)
    engine._return_base_by_uav[uav.id] = engine.bases[0]
    uav.plan_return(plan.path)
    calls = []
    def plan_grid(self, start, goals, mask, r_min, version):
        goal = next(iter(goals))
        calls.append(goal)
        return [start, (*goal, math.atan2(goal[1]-start[1],goal[0]-start[0]))]
    monkeypatch.setattr(AStarNavigator, 'plan_grid', plan_grid)
    engine.obstacle_mask[14,10] = True
    engine.allocator.sm.obstacle_mask[14,10] = True
    return engine, uav, calls


def test_blocked_reserved_base_diverts_before_failing(monkeypatch):
    engine, uav, calls = _returning_engine(monkeypatch)
    engine._step_controlled_uav(uav, 1.)
    assert uav.id not in engine._emergency_failures
    assert engine._return_base_by_uav[uav.id].id == 'alternate'
    assert engine._base_maintenance_load(engine.bases[0]) == 0
    assert engine._base_maintenance_load(engine.bases[1]) == 1
    assert (14.,10.) in calls and (10.,14.) in calls


def test_handoff_offers_search_instead_of_unobserved_direct_track():
    engine = _recovery_engine()
    sm = engine.allocator.sm
    contact = _contact('C1', vessel_class='type_ii', position=(12.,12.))
    sm.handoff_manager.require('C1', source_uav_id=engine.uavs[0].id, required_at_min=10., last_position=(12.,12.), last_observed_at_min=8.)
    catalog = TaskCatalog(candidate_extractor=CandidateSource([]))
    tasks = catalog.build(sm, (contact,), (), 10.)
    assert not any(task.kind == 'track' for task in tasks)
    search = next(task for task in tasks if task.kind == 'investigation')
    assert search.contact_id == 'C1'
    assert search.bbox[0] <= 12 < search.bbox[2]
    assert search.bbox[1] <= 12 < search.bbox[3]
    assert engine.uavs[0].id not in search.feasible_uav_ids


@pytest.mark.parametrize('inbound', [False, True])
def test_diversion_skips_full_nearer_alternate(monkeypatch, inbound):
    engine, uav, _ = _returning_engine(monkeypatch)
    near = BaseStation(GridCoord(10,12), 3, 1, 'full')
    engine.bases.append(near)
    if inbound:
        engine._return_base_by_uav['other'] = near
    else:
        assert near.land_uav('other')
    assert engine._divert_blocked_return(uav, 1.)
    assert engine._return_base_by_uav[uav.id].id == 'alternate'


def test_full_alternate_retains_holding_fallback(monkeypatch):
    engine, uav, _ = _returning_engine(monkeypatch)
    assert engine.bases[1].land_uav('other')
    assert engine._divert_blocked_return(uav, 1.)
    engine._land_for_refuelling(uav)
    assert uav.status == 'holding'
    assert engine.bases[1].occupancy == 1


def test_diversion_fails_closed_when_fuel_cannot_cover_path_and_reserve(monkeypatch):
    engine, uav, _ = _returning_engine(monkeypatch)
    uav.fuel_remaining_pct = 4.5 / (uav.remaining_range_cells / uav.fuel_remaining_pct)
    engine._step_controlled_uav(uav, 1.)
    assert engine._emergency_failures[uav.id] == 'no_safe_recovery_path'
    assert uav.id not in engine._return_base_by_uav


def test_diversion_rolls_back_reservation_if_install_rejected(monkeypatch):
    engine, uav, _ = _returning_engine(monkeypatch)
    previous = engine.control_coordinator.active_task(uav.id)
    def reject(*args, **kwargs):
        raise RuntimeError('install rejected')
    monkeypatch.setattr(engine.control_coordinator, 'assign_system_task', reject)
    with pytest.raises(RuntimeError, match='install rejected'):
        engine._divert_blocked_return(uav, 1.)
    assert engine._return_base_by_uav[uav.id].id == 'old'
    assert engine.control_coordinator.active_task(uav.id) == previous


def _handoff_engine():
    engine = _recovery_engine()
    source, successor = engine.uavs
    ship = engine.ships[0]
    ship.position = GridCoord(12, 12)
    cid = engine._handle_detection(source, ship, 0.)
    sm = engine.allocator.sm
    sm.contacts._contacts[cid] = replace(sm.contacts.snapshot(cid), vessel_class='type_ii')
    source.target_group_id = cid
    before = sm.get_last_scan_matrix().copy()
    attempt = engine._require_handoff(source, sm.get_target_report(cid), 0.)
    np.testing.assert_array_equal(before, sm.get_last_scan_matrix())
    return engine, source, successor, ship, cid, attempt


def test_handoff_search_assignment_then_actual_redetection_enables_track():
    from src.mission.contracts import Assignment, AssignmentBatch
    engine, source, successor, ship, cid, attempt = _handoff_engine()
    snapshot = engine.allocator.build_mission_snapshot(0.)
    candidate = next(task for task in snapshot.candidates if task.task_id.startswith('handoff-search:'))
    batch = AssignmentBatch(snapshot.snapshot_id, (Assignment(candidate.task_id, successor.id,
        dict(snapshot.uav_generations)[successor.id], None),), 'fixture-handoff')
    assert engine.apply_assignment_batch(batch)
    assert engine.handoff_manager.latest_for_contact(cid).state == 'pending'
    assert engine.control_coordinator.active_task(successor.id).task_type is OperationMode.COVERAGE
    assert engine.handoff_manager.latest_for_contact(cid).successor_uav_id == successor.id
    contact = engine.allocator.sm.contacts.snapshot(cid)
    catalog = engine.allocator.task_catalog
    assert not any(task.kind == 'track' and task.contact_id == cid
                   for task in catalog.build(engine.allocator.sm, (contact,), (), 0.))
    # No truth lookup in the handoff: only the actual sensor adapter releases a fix.
    assert engine._handle_detection(successor, ship, 0.) == cid
    tasks = catalog.build(engine.allocator.sm, (engine.allocator.sm.contacts.snapshot(cid),), (), 0.)
    track = next(task for task in tasks if task.kind == 'track')
    assert track.feasible_uav_ids == (successor.id,)


def test_seamless_handoff_requires_successors_own_recent_visual_sample():
    engine, source, successor, ship, cid, attempt = _handoff_engine()
    engine._handle_detection(successor, ship, 0.)
    contact = engine.allocator.sm.contacts.snapshot(cid)
    catalog = TaskCatalog(candidate_extractor=CandidateSource([]))
    tasks = catalog.build(engine.allocator.sm, (contact,), (), 0.)
    assert [task.kind for task in tasks] == ['track']
    assert tasks[0].feasible_uav_ids == (successor.id,)
    tasks = catalog.build(engine.allocator.sm, (contact,), (), 2.)
    assert not any(task.kind == 'track' for task in tasks)


def test_full_coverage_floor_defers_handoff_until_explicit_deadline():
    from src.mission.mission_scheduler import validate_selection, SELECTION_SCHEMA
    engine, _, _, _, cid, attempt = _handoff_engine()
    snapshot = engine.allocator.build_mission_snapshot(0.)
    task = next(t for t in snapshot.candidates if t.task_id.startswith('handoff-search:'))
    payload = dict(schema_version=SELECTION_SCHEMA, snapshot_id=snapshot.snapshot_id,
                   selected_task_ids=[task.task_id], preempt_uav_ids=[], defer_reason=None, notes='')
    assert any(error.startswith('coverage_floor_not_met') for error in validate_selection(payload, snapshot))
    assert engine.handoff_manager.latest_for_contact(cid).state == 'required'
    failed = engine.handoff_manager.advance(attempt.assignment_deadline_min + .1)
    assert failed[0].failure_reason == 'assignment_deadline'


def test_scheduler_selects_handoff_with_coverage_headroom_and_sensor_redetects():
    from src.mission.contracts import AssignmentBatch
    from src.mission.mission_scheduler import pair_selected_tasks, SELECTION_SCHEMA
    engine, source, successor, ship, cid, attempt = _handoff_engine()
    sm = engine.allocator.sm
    engine.allocator.task_catalog.extractor = CandidateSource([dict(bbox=(18,16,22,20), cell_count=16, avg_info=0., total_value=16.)])
    # Fixture history of actual SAR coverage provides capacity; evidence alone cannot.
    sm.coverage_metrics.record_sar(tuple(tuple(map(int, cell)) for cell in np.argwhere(sm.coverage_metrics.fixed_mask)), at_min=0.)
    snapshot = engine.allocator.build_mission_snapshot(0.)
    handoff = next(t for t in snapshot.candidates if t.task_id.startswith('handoff-search:'))
    paired = None
    for ordinary in snapshot.candidates:
        if ordinary.kind != 'search':
            continue
        payload = dict(schema_version=SELECTION_SCHEMA, snapshot_id=snapshot.snapshot_id,
                       selected_task_ids=[handoff.task_id, ordinary.task_id], preempt_uav_ids=[], defer_reason=None, notes='')
        result = pair_selected_tasks(payload, snapshot)
        if result.is_valid:
            paired = result
            break
    assert paired is not None
    assert engine.apply_assignment_batch(AssignmentBatch(snapshot.snapshot_id, paired.assignments, 'fixture'))
    engine._step_controlled_uav(successor, 1.)
    successor._col, successor._row, successor.heading_rad = 12.5, 11., 0.
    successor.status, successor.sensor_mode = 'searching', 'sar'
    successor.sar_imaging = True
    successor.sar_look_direction = 'right'
    successor.sar_sensor.detection_probability = 1.
    engine.clock.time = sm.current_time = 5.
    engine._update_sensors_and_detections(5.)
    assert any(sample.source == 'sar' and sample.source_id == successor.id
               for sample in sm.contacts.snapshot(cid).samples)
    assert any(event['type'] == 'handoff_redetected' for event in sm.get_recent_events(0.))
    assert engine._mission_task_records[handoff.task_id].status != 'completed'
    assert successor.status == 'holding'
    engine._sync_state_from_entities()
    snapshot = engine.allocator.build_mission_snapshot(5., active_tasks=tuple(engine._mission_task_records.values()))
    track = next(t for t in snapshot.candidates if t.kind == 'track' and t.contact_id == cid)
    payload = dict(schema_version=SELECTION_SCHEMA, snapshot_id=snapshot.snapshot_id,
                   selected_task_ids=[track.task_id], preempt_uav_ids=[], defer_reason=None, notes='')
    paired = pair_selected_tasks(payload, snapshot)
    assert paired.is_valid, (paired.errors, snapshot.resources, snapshot.preemptible_uav_ids, engine._emergency_failures)
    assert engine.apply_assignment_batch(AssignmentBatch(snapshot.snapshot_id, paired.assignments, 'fixture-track'))
    assert engine.control_coordinator.active_task(successor.id).task_type is OperationMode.TRACK
    successor._col, successor._row, successor.heading_rad = 11., 12., 0.
    successor.status, successor.sensor_mode = 'tracking', 'eo'
    engine._update_sensors_and_detections(5.)
    assert engine.handoff_manager.latest_for_contact(cid).state == 'succeeded'


def test_enroute_diversion_chooses_shortest_safe_path_not_closest_base(monkeypatch):
    engine, uav, _ = _returning_engine(monkeypatch)
    engine.obstacle_mask[14, 10] = False
    engine.allocator.sm.obstacle_mask[14, 10] = False
    engine._step_controlled_uav(uav, .5)
    assert engine.control_coordinator.route_snapshot(uav.id).route.status == 'ready'
    engine.obstacle_mask[14, 10] = True
    engine.allocator.sm.obstacle_mask[14, 10] = True
    near = BaseStation(GridCoord(11, 12), 3, 1, 'near-detour')
    engine.bases.append(near)
    def routes(self, start, goals, mask, r_min, version):
        goal = next(iter(goals))
        if goal == (11., 12.):
            return [start, (16., 8., 0.), (16., 12., 0.), (*goal, 0.)]
        return [start, (*goal, 0.)]
    monkeypatch.setattr(AStarNavigator, 'plan_grid', routes)
    engine._step_controlled_uav(uav, 1.)
    assert uav.id not in engine._emergency_failures
    assert engine._return_base_by_uav[uav.id].id == 'alternate'

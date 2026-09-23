"""Available airframes must not be stranded by coverage floors or stale ticks."""
from dataclasses import replace

import pytest

from src.mission.contracts import CoverageConstraint, TaskRecord
from src.mission.mission_scheduler import validate_selection
from src.schedule.task_allocator import TaskAllocator
from src.schedule.trigger_manager import TriggerDecision
from src.schedule.config_loader import ConfigLoader
from tests.mission.test_feature_episode_integration import (
    _ReviewerDouble, _allocator_with_selection_provider,
    _coverage_capacity_satisfied_snapshot,
)
from tests.mission.test_mission_scheduler import (
    _task, _resource, _edge, _snapshot, _selection,
)


def test_coverage_floor_does_not_leave_available_aircraft_unused():
    snapshot = _snapshot(
        [_task('S1'), _task('S2')], [_resource('U1'), _resource('U2')],
        [_edge('S1', 'U1', 1.), _edge('S2', 'U2', 1.)],
    )
    snapshot = replace(snapshot, coverage_constraint=CoverageConstraint(1, 1, 0, (), ()))
    assert validate_selection(_selection(snapshot, ['S1', 'S2']), snapshot) == ()
    assert 'underutilized_feasible_work:S2' in validate_selection(
        _selection(snapshot, ['S1']), snapshot,
    )


@pytest.mark.parametrize('trigger', ['none', 'light', 'heavy'])
def test_idle_aircraft_get_new_work_without_waiting_for_periodic_cycle(monkeypatch, trigger):
    allocator = _allocator_with_selection_provider()
    allocator.reviewer = _ReviewerDouble()
    allocator.trigger_manager.check = lambda _time: TriggerDecision(trigger)
    snapshot = _coverage_capacity_satisfied_snapshot()
    monkeypatch.setattr(allocator, 'build_mission_snapshot', lambda *_a, **_kw: snapshot)
    result, batch = allocator.mission_step(10.)
    assert result['action'] == 'mission_selection_approved'
    assert batch is not None
    assert [(a.uav_id, a.task_id) for a in batch.assignments] == [('U1', 'search:spare')]


def test_light_pairing_refreshes_refuelled_aircraft_resources(monkeypatch):
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    fresh = _coverage_capacity_satisfied_snapshot()
    pending = TaskRecord('search:spare', 'search', 'approved', (10, 10, 14, 14),
                         None, (), None, None, 0., 0., None, None)
    fresh = replace(fresh, candidates=(), active_tasks=(pending,), coverage_constraint=None)
    allocator._last_mission_snapshot = replace(
        fresh, available_uav_ids=(), resources=(replace(fresh.resources[0], operation='refueling'),),
    )
    monkeypatch.setattr(allocator, 'build_mission_snapshot', lambda *_a, **_kw: fresh)
    _, batch = allocator._handle_light_mission_trigger(10., TriggerDecision('light'), (pending,))
    assert batch is not None
    assert batch.assignments[0].uav_id == 'U1'


def test_no_feasible_work_does_not_poll_model_each_idle_tick(monkeypatch):
    allocator = _allocator_with_selection_provider()
    allocator.reviewer = _ReviewerDouble()
    allocator.trigger_manager.check = lambda _time: TriggerDecision('none')
    snapshot = replace(_coverage_capacity_satisfied_snapshot(), feasible_edges=())
    monkeypatch.setattr(allocator, 'build_mission_snapshot', lambda *_a, **_kw: snapshot)
    monkeypatch.setattr(allocator.mission_scheduler, 'decide',
                        lambda *_a, **_kw: pytest.fail('no safe work to select'))
    for tick in (10., 11.):
        result, batch = allocator.mission_step(tick)
        assert result['trigger_type'] == 'none'
        assert batch is None

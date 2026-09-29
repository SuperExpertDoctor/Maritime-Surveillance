import numpy as np

from src.mission.coverage_partition import partition_search_mask


def _union(shape, boxes):
    counts = np.zeros(shape, dtype=int)
    for x0, y0, x1, y1 in boxes:
        counts[x0:x1, y0:y1] += 1
    assert counts.max(initial=0) <= 1
    return counts.astype(bool)


def test_ten_aircraft_partition_whole_domain_into_ten_large_regions():
    mask = np.ones((30, 30), dtype=bool)
    boxes = partition_search_mask(mask, 10)
    assert len(boxes) == 10
    assert min((x1-x0)*(y1-y0) for x0,y0,x1,y1 in boxes) >= 60
    np.testing.assert_array_equal(_union(mask.shape, boxes), mask)
    assert boxes == partition_search_mask(mask, 10)


def test_running_reservations_remain_untouched():
    mask = np.ones((30, 30), dtype=bool)
    mask[:10, :] = False
    boxes = partition_search_mask(mask, 6)
    assert len(boxes) == 6
    np.testing.assert_array_equal(_union(mask.shape, boxes), mask)


def test_weather_holes_never_become_task_area():
    mask = np.ones((30, 30), dtype=bool)
    mask[12:17, 8:13] = False
    boxes = partition_search_mask(mask, 10)
    assert len(boxes) == 10
    np.testing.assert_array_equal(_union(mask.shape, boxes), mask)
    assert partition_search_mask(mask, 0) == ()


def test_bounded_partitions_keep_clear_weather_areas_without_oversized_boxes():
    mask = np.ones((30, 30), dtype=bool)
    mask[12:17, 8:13] = False
    boxes = partition_search_mask(mask, 10, min_area=20, max_area=110)

    assert len(boxes) == 10
    assert all(20 <= (x1 - x0) * (y1 - y0) <= 110 for x0, y0, x1, y1 in boxes)
    covered = _union(mask.shape, boxes)
    assert not np.any(covered & ~mask)
    assert np.count_nonzero(covered) >= 600


def test_default_map_exposes_feasible_large_search_tasks():
    from src.env.simulation import SimulationEngine
    from src.schedule.config_loader import ConfigLoader

    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=object())
    snapshot = engine.allocator.build_mission_snapshot(0.)
    ids = {task_id for task_id in snapshot.prompt_task_ids if task_id.startswith("partition:")}
    feasible = {edge.task_id for edge in snapshot.feasible_edges} & ids

    assert len(feasible) >= 5
    boxes = [task.bbox for task in snapshot.candidates if task.task_id in feasible]
    assert np.count_nonzero(_union((30, 30), boxes)) >= 300
    feasible_search = [task.bbox for task in snapshot.candidates
                       if task.task_id in snapshot.prompt_task_ids
                       and task.task_id in {edge.task_id for edge in snapshot.feasible_edges}
                       and task.kind == 'search']
    all_search = np.zeros((30, 30), dtype=bool)
    for x0, y0, x1, y1 in feasible_search:
        all_search[x0:x1, y0:y1] = True
    assert np.count_nonzero(all_search) >= 400
    assert snapshot.coverage_constraint.required_new_search_count == 10


def test_production_candidate_window_keeps_large_partition_boundaries():
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.task_allocator import TaskAllocator
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    sm = allocator.sm
    mask = np.ones(sm.obstacle_mask.shape, dtype=bool)
    sm.configure_coverage_metrics(mask, 'partition-test')
    tasks = allocator.task_catalog.build(sm, (), (), 0.)
    window = allocator._coverage_prompt_window(tasks, 0.)
    partitions = [task for task in window.tasks if task.task_id.startswith('partition:')]
    assert len(partitions) == len(sm.get_available_uavs())
    np.testing.assert_array_equal(_union(mask.shape, [task.bbox for task in partitions]), mask)


def test_pairing_minimizes_total_flight_range_not_only_transit():
    from dataclasses import replace
    from src.mission.mission_scheduler import _minimum_cost_matching
    from tests.mission.test_mission_scheduler import _resource, _edge
    resources = {uid: _resource(uid) for uid in ('U1', 'U2')}
    options = {'S1': (
        replace(_edge('S1', 'U1', 1.), mission_range_cells=60.),
        replace(_edge('S1', 'U2', 3.), mission_range_cells=10.),
    )}
    chosen = _minimum_cost_matching(('S1',), options, resources)
    assert chosen['S1'].uav_id == 'U2'


def test_real_engine_launches_every_available_aircraft_into_larger_regions():
    from tests.mission.test_coverage_scan_integration import _engine
    engine = _engine(uav_count=10, ship_count=0)
    assert engine.ships == []
    assert engine.allocator.sm.contacts.list_snapshots() == ()
    result = engine.step()
    assert result['action'] == 'mission_selection_approved'
    assert engine.runtime_status == 'running'
    assert engine.allocator.sm.get_available_uavs() == []
    regions = engine.allocator.sm.get_assigned_search_regions()
    assert len(regions) == 10
    assert all(uav.status == 'transit' for uav in engine.uavs)
    area = sum((r.bbox.col_end-r.bbox.col_start)*(r.bbox.row_end-r.bbox.row_start)
               for r in regions)
    assert area > 10 * engine.config.grid.search_max_cells


def test_default_map_dispatches_all_ten_aircraft_despite_weather_and_obstacles():
    from scripts.evaluate_mixed_maritime import _FixtureGateway
    from src.env.simulation import SimulationEngine
    from src.schedule.config_loader import ConfigLoader

    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=_FixtureGateway())
    result = engine.step()

    assert result['action'] == 'mission_selection_approved'
    assert len(engine.allocator.sm.get_assigned_search_regions()) == 10
    assert engine.allocator.sm.get_available_uavs() == []
    assert all(uav.status == 'transit' for uav in engine.uavs)
    stats = engine.allocator.sm.get_persistent_coverage_stats()
    assert next(item for item in stats['windows'] if item['minutes'] == 60)['covered_cells'] == 0

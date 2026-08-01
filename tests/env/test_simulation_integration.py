import math

import numpy as np

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord
from src.schedule.datatypes import Region
from src.utils.coverage_planner import CoveragePlanner


def test_sar_is_off_during_dubins_turns():
    engine = SimulationEngine(ConfigLoader.load())
    uav = engine.uavs[0]
    bbox = BBox(5, 5, 11, 11)
    coverage = CoveragePlanner(sample_step=0.2).plan(bbox, uav.pose, 2, 1)
    scan_ranges = [
        (start, end, swath.look_direction)
        for (start, end), swath in zip(coverage.scan_ranges, coverage.swaths)
    ]
    uav.assign_mission(
        bbox,
        coverage.waypoints,
        transit_end_index=coverage.scan_ranges[0][0],
        scan_ranges=scan_ranges,
    )

    first_end = scan_ranges[0][1]
    second_start = scan_ranges[1][0]
    uav._wp_index = (first_end + second_start) // 2 + 1
    uav._update_scan_direction()
    assert uav.status == "transit"
    assert uav.sensor_mode == "off"

    uav._wp_index = scan_ranges[1][0] + 2
    uav.heading_rad = uav.waypoints[scan_ranges[1][0] + 2][2]
    uav._update_scan_direction()
    assert uav.status == "searching"
    assert uav.sensor_mode == "sar"


def test_dynamic_obstacle_replans_remaining_return_route():
    engine = SimulationEngine(ConfigLoader.load())
    uav = engine.uavs[0]
    uav.position = GridCoord(2, 28)
    uav.heading_rad = 0.0
    base = engine.base.position
    uav.plan_return([
        (2.0, 28.0, 0.0),
        (float(base.col), float(base.row), 0.0),
    ])

    mask = np.zeros(engine.config.grid.resolution, dtype=bool)
    midpoint = (
        int(math.floor((2 + base.col) / 2)),
        int(math.floor((28 + base.row) / 2)) - 1,
    )
    mask[midpoint] = True
    engine.obstacle_mask = mask
    engine.allocator.sm.set_environment_obstacles([], mask)
    engine._replan_conflicting_routes()

    assert engine.obstacle_avoider.is_path_safe(uav.remaining_path, mask)
    land_bases = engine.allocator.sm.get_base_positions()
    assert any(
        math.dist(uav.remaining_path[-1][:2], tuple(land_base)) < 1e-6
        for land_base in land_bases
    )
    events = engine.allocator.sm.get_recent_events(0)
    assert any(event["type"] == "route_replanned" for event in events)


def test_every_initial_candidate_has_a_safe_executable_route():
    engine = SimulationEngine(ConfigLoader.load())
    candidates = engine.allocator.extractor.extract(engine.allocator.sm).candidate_regions
    assert candidates
    for index, candidate in enumerate(candidates):
        region = Region(
            id=f"S{index + 1}",
            bbox=candidate["bbox"],
            type="search",
        )
        uav = engine.uavs[index]
        engine._assign_search_route(uav, region)
        assert engine.obstacle_avoider.is_path_safe(
            uav.planned_path,
            engine.obstacle_mask,
        )


def test_search_stays_off_until_uav_reaches_region_boundary():
    engine = SimulationEngine(ConfigLoader.load(), seed=23)
    candidate = engine.allocator.extractor.extract(engine.allocator.sm).candidate_regions[0]
    region = Region(id="S-boundary", bbox=candidate["bbox"], type="search")
    uav = engine.uavs[0]

    engine._assign_search_route(uav, region)

    assert uav.status == "transit"
    assert uav.sensor_mode == "off"
    assert uav._transit_end_index == uav._scan_ranges[0][0]
    entry = uav.planned_path[uav._transit_end_index]
    bbox = region.bbox
    assert (
        math.isclose(entry[0], bbox.col_start, abs_tol=0.3)
        or math.isclose(entry[0], bbox.col_end, abs_tol=0.3)
        or math.isclose(entry[1], bbox.row_start, abs_tol=0.3)
        or math.isclose(entry[1], bbox.row_end, abs_tol=0.3)
    )

    uav._wp_index = uav._transit_end_index
    uav._update_scan_direction()
    assert uav.sensor_mode == "off"
    uav._wp_index = uav._transit_end_index + 2
    uav.heading_rad = uav.waypoints[uav._transit_end_index + 2][2]
    uav._update_scan_direction()
    assert uav.status == "searching"
    assert uav.sensor_mode == "sar"


def test_sar_requires_stable_straight_heading_before_writing_information():
    engine = SimulationEngine(ConfigLoader.load(), seed=31)
    uav = engine.uavs[0]
    bbox = BBox(10, 8, 16, 14)
    coverage = CoveragePlanner(sample_step=0.2).plan(bbox, uav.pose, 2, 1)
    scan_ranges = [
        (start, end, swath.look_direction)
        for (start, end), swath in zip(coverage.scan_ranges, coverage.swaths)
    ]
    uav.assign_mission(
        bbox,
        coverage.waypoints,
        transit_end_index=coverage.scan_ranges[0][0],
        scan_ranges=scan_ranges,
    )
    start, end, _ = scan_ranges[0]
    uav._wp_index = start + 2
    uav._col, uav._row = uav.waypoints[start + 1][:2]
    uav.heading_rad = uav.waypoints[start + 2][2] + math.radians(9)
    uav._update_scan_direction()

    assert not uav.sar_imaging
    assert uav.sensor_mode == "off"
    engine._update_sensors_and_detections(engine.clock.time)
    assert not np.isfinite(engine.allocator.sm.info_field.last_scan_time).any()

    uav.heading_rad = uav.waypoints[start + 2][2]
    uav._update_scan_direction()
    assert uav.sar_imaging
    engine._update_sensors_and_detections(engine.clock.time)
    assert np.isfinite(engine.allocator.sm.info_field.last_scan_time).any()


def test_simulation_applies_phase_speed_control_to_shared_trackers():
    engine = SimulationEngine(ConfigLoader.load())
    center = engine._group_center("G1")
    first, second = engine.uavs[:2]
    for uav in (first, second):
        uav.status = "tracking"
        uav.target_group_id = "G1"
    first._col, first._row = center[0] + 1.8, center[1]
    second._col = center[0] + 1.8 * math.cos(0.2)
    second._row = center[1] + 1.8 * math.sin(0.2)

    commands = engine._tracking_speed_commands()

    assert set(commands) == {first.id, second.id}
    assert commands[second.id] > commands[first.id]


def test_completed_search_starts_a_real_return_during_lifecycle_rotation():
    engine = SimulationEngine(ConfigLoader.load())
    engine._lifecycle_mode = True
    engine.allocator.sm.lifecycle_mode = True
    uav = engine.uavs[0]
    state = engine.allocator.sm.get_uav(uav.id)
    region = Region(
        id="S-cycle",
        bbox=BBox(13, 24, 17, 29),
        type="search",
        assigned_uav_id=uav.id,
    )
    engine.allocator.sm.set_search_regions([region])
    state.assigned_region_id = region.id
    uav.search_complete_pending = True
    uav.status = "idle"

    engine._process_search_completions(10.0)

    assert uav.status == "returning"
    assert uav.mission_kind == "return"


def test_tracking_dwell_starts_return_without_waiting_for_low_fuel():
    engine = SimulationEngine(ConfigLoader.load())
    engine._lifecycle_mode = True
    engine.allocator.sm.lifecycle_mode = True
    uav = engine.uavs[0]
    uav.status = "tracking"
    uav.target_group_id = "G1"
    engine._sortie_searched[uav.id] = True
    engine._tracking_started_at[uav.id] = -engine.config.uav.lifecycle_search_dwell_min

    engine.step()

    assert uav.status == "returning"
    assert uav.id not in engine._tracking_started_at


def test_lifecycle_rotation_waits_for_time_and_coverage_gate():
    engine = SimulationEngine(ConfigLoader.load())
    cfg = engine.config.uav
    engine.clock.time = cfg.lifecycle_rotation_start_min - 1
    engine._update_lifecycle_mode(engine.clock.time)
    assert not engine._lifecycle_mode

    searchable = engine.allocator.sm.get_searchable_mask()
    cells = list(zip(*searchable.nonzero()))
    required = math.ceil(
        len(cells) * cfg.lifecycle_coverage_threshold_pct / 100
    )
    for col, row in cells[:required]:
        engine.allocator.sm.scan_cell(GridCoord(col, row), engine.clock.time)

    engine.clock.time = cfg.lifecycle_rotation_start_min
    engine._update_lifecycle_mode(engine.clock.time)

    assert engine._lifecycle_mode
    assert engine.allocator.sm.lifecycle_mode


def test_completed_lifecycle_does_not_restart_from_coverage_gate():
    engine = SimulationEngine(ConfigLoader.load())
    cfg = engine.config.uav
    engine._lifecycle_completed = True
    engine.clock.time = cfg.lifecycle_rotation_start_min
    searchable = engine.allocator.sm.get_searchable_mask()
    for col, row in zip(*searchable.nonzero()):
        engine.allocator.sm.scan_cell(GridCoord(col, row), engine.clock.time)

    engine._update_lifecycle_mode(engine.clock.time)

    assert not engine._lifecycle_mode


def test_return_route_uses_nearest_land_recovery_base():
    engine = SimulationEngine(ConfigLoader.load())
    uav = engine.uavs[0]
    expected = engine.bases[-1].position
    uav.position = expected
    uav.heading_rad = engine._inward_heading(expected)

    engine._set_return_route(uav, 10.0)

    assert math.dist(uav.remaining_path[-1][:2], tuple(expected)) < 1e-6


def test_return_route_prefers_nearest_base_over_historical_refuel_count():
    config = ConfigLoader.load()
    config.environment.base_count = 3
    engine = SimulationEngine(config, seed=42)
    uav = engine.uavs[0]
    busiest = engine.bases[0]
    for base in engine.bases:
        base.refuel_count = 0
    busiest.refuel_count = 5
    uav.position = busiest.position
    uav.heading_rad = engine._inward_heading(busiest.position)

    engine._set_return_route(uav, 10.0)

    assert engine._return_base_by_uav[uav.id] is busiest


def test_return_route_skips_base_with_full_reserved_maintenance_capacity():
    config = ConfigLoader.load()
    config.environment.base_count = 3
    engine = SimulationEngine(config, seed=42)
    uav = engine.uavs[0]
    busiest = engine.bases[0]
    uav.position = busiest.position
    uav.heading_rad = engine._inward_heading(busiest.position)
    for assigned in engine.uavs[1:4]:
        engine._return_base_by_uav[assigned.id] = busiest

    engine._set_return_route(uav, 10.0)

    assert engine._return_base_by_uav[uav.id] is not busiest


def test_reserve_return_uses_95_percent_of_remaining_fixed_range():
    engine = SimulationEngine(ConfigLoader.load())
    uav = engine.uavs[0]
    uav.position = GridCoord(15, 15)
    uav.status = "searching"
    base = engine._nearest_available_base(uav.float_position, exclude_uav_id=uav.id)
    assert base is not None
    distance_home = math.dist(
        uav.float_position,
        (base.position.col, base.position.row),
    )

    uav.fuel_remaining_pct = 1.0
    assert not engine._needs_reserve_return(uav)

    uav.fuel_remaining_pct = (
        distance_home / 0.95 - 0.1
    ) / uav.total_range_cells
    assert engine._needs_reserve_return(uav)


def test_reserve_return_starts_early_for_fixed_wing_navigation_margin():
    engine = SimulationEngine(ConfigLoader.load())
    uav = engine.uavs[0]
    uav.position = GridCoord(15, 15)
    uav.status = "searching"
    base = engine._nearest_available_base(uav.float_position, exclude_uav_id=uav.id)
    assert base is not None
    distance_home = math.dist(
        uav.float_position,
        (base.position.col, base.position.row),
    )
    navigation_distance = distance_home + 2.0 * math.pi * uav.R_min + 4.0
    uav.fuel_remaining_pct = (
        navigation_distance / 0.95 - 0.1
    ) / uav.total_range_cells

    assert distance_home <= uav.remaining_range_cells * 0.95
    assert engine._needs_reserve_return(uav)


def test_return_route_uses_next_nearest_base_when_closest_is_full():
    engine = SimulationEngine(ConfigLoader.load())
    uav = engine.uavs[0]
    closest = engine.bases[0]
    uav.position = closest.position
    uav.heading_rad = engine._inward_heading(closest.position)
    for slot in range(closest.capacity):
        assert closest.land_uav(f"maintenance-{slot}")

    expected = engine._nearest_available_base(
        uav.float_position,
        exclude_uav_id=uav.id,
    )
    assert expected is not None

    engine._set_return_route(uav, 10.0)

    assert engine._return_base_by_uav[uav.id] is expected


def test_fixed_range_consumption_uses_actual_route_distance():
    engine = SimulationEngine(ConfigLoader.load())
    uav = engine.uavs[0]
    start_col, start_row = uav.float_position
    uav._set_route([(start_col + 1.0, start_row, 0.0)])
    uav.status = "transit"

    uav.step(3.75)

    assert math.isclose(
        uav.fuel_remaining_pct,
        1.0 - 1.0 / uav.total_range_cells,
        rel_tol=0,
        abs_tol=1e-9,
    )


def test_post_coverage_search_assignment_keeps_stale_revisit_swaths():
    engine = SimulationEngine(ConfigLoader.load(), seed=23)
    candidate = engine.allocator.extractor.extract(
        engine.allocator.sm
    ).candidate_regions[0]
    region = Region(id="S-revisit", bbox=candidate["bbox"], type="search")
    searchable = engine.allocator.sm.get_searchable_mask()
    scan_times = engine.allocator.sm.info_field.last_scan_time
    scan_times[searchable] = 1.0
    uav = engine.uavs[0]

    engine._assign_search_route(uav, region)

    assert uav.status == "transit"
    assert uav._scan_ranges
    assert uav.planned_path


def test_freshness_patrol_assignment_revisits_before_global_coverage_target():
    engine = SimulationEngine(ConfigLoader.load(), seed=23)
    candidate = engine.allocator.extractor.extract(
        engine.allocator.sm
    ).candidate_regions[0]
    region = Region(id="S-early-patrol", bbox=candidate["bbox"], type="search")
    scan_times = engine.allocator.sm.info_field.last_scan_time
    scan_times[region.bbox.col_start:region.bbox.col_end,
               region.bbox.row_start:region.bbox.row_end] = 1.0
    uav = engine.uavs[0]

    engine._assign_search_route(uav, region, allow_revisit=True)

    assert uav.status == "transit"
    assert uav._scan_ranges


def test_search_route_uses_short_dubins_connectors_when_clear(monkeypatch):
    engine = SimulationEngine(ConfigLoader.load(), seed=23)
    engine.obstacle_mask = np.zeros(engine.config.grid.resolution, dtype=bool)
    uav = engine.uavs[0]
    uav._col, uav._row, uav.heading_rad = 9.0, 10.0, 0.0
    region = Region(id="S-clear", bbox=BBox(10, 10, 18, 15), type="search")

    def unexpected_rrt(*_args, **_kwargs):
        raise AssertionError("clear connector should not invoke RRT*")

    monkeypatch.setattr(engine.obstacle_avoider, "plan_path", unexpected_rrt)
    engine._assign_search_route(uav, region)

    assert uav.status == "transit"
    assert uav._scan_ranges


def test_post_coverage_completion_restarts_local_revisit_without_idling():
    engine = SimulationEngine(ConfigLoader.load(), seed=23)
    candidate = engine.allocator.extractor.extract(
        engine.allocator.sm
    ).candidate_regions[0]
    region = Region(
        id="S-patrol",
        bbox=candidate["bbox"],
        type="search",
        assigned_uav_id=engine.uavs[0].id,
    )
    engine.allocator.sm.set_search_regions([region])
    state = engine.allocator.sm.get_uav(engine.uavs[0].id)
    state.assigned_region_id = region.id
    searchable = engine.allocator.sm.get_searchable_mask()
    engine.allocator.sm.info_field.last_scan_time[searchable] = 1.0
    uav = engine.uavs[0]
    uav.status = "idle"
    uav.search_complete_pending = True

    engine._process_search_completions(
        engine.config.uav.freshness_patrol_start_min,
    )

    assert uav.status == "transit"
    assert not uav.search_complete_pending
    assert region.status == "active"
    assert region.assigned_uav_id == uav.id


def test_freshness_patrol_caps_local_revisit_fleet_size():
    engine = SimulationEngine(ConfigLoader.load(), seed=23)
    start = engine.config.uav.freshness_patrol_start_min
    selected = [
        engine._should_continue_freshness_patrol(uav, start)
        for uav in engine.uavs
    ]

    assert sum(selected) == engine.config.uav.freshness_patrol_count
    assert not engine._should_continue_freshness_patrol(
        engine.uavs[-1],
        start - 1,
    )

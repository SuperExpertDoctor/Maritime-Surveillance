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

    uav._wp_index = scan_ranges[1][0] + 1
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
        int(math.floor((28 + base.row) / 2)),
    )
    mask[midpoint] = True
    engine.obstacle_mask = mask
    engine.allocator.sm.set_environment_obstacles([], mask)
    engine._replan_conflicting_routes()

    assert engine.obstacle_avoider.is_path_safe(uav.remaining_path, mask)
    assert math.dist(uav.remaining_path[-1][:2], tuple(base)) < 1e-6
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

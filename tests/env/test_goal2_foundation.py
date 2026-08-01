import math

from src.env.base_station import BaseStation
from src.env.obstacle import Island, Thunderstorm, obstacle_intersects_mask
from src.env.simulation import SimulationEngine
from src.env.ship import ShipType
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord, Region
from src.vis.backend.frame_builder import build_frame


def test_default_engine_has_two_left_mainland_bases_with_three_refuelling_slots_each():
    config = ConfigLoader.load()
    engine = SimulationEngine(config, seed=19)

    assert len(engine.bases) == 2
    assert all(base.capacity == 3 for base in engine.bases)
    assert all(engine.land_mask[base.position.col, base.position.row] for base in engine.bases)
    assert math.dist(engine.bases[0].position, engine.bases[1].position) >= config.environment.base_min_distance_cells


def test_default_open_water_has_at_most_two_islands():
    engine = SimulationEngine(ConfigLoader.load(), seed=23)

    islands = [obstacle for obstacle in engine.obstacles if isinstance(obstacle, Island)]
    assert 0 <= len(islands) <= 2
    assert all(island.size <= 2 for island in islands)
    assert all(not obstacle_intersects_mask(island, engine.land_mask) for island in islands)
    storms = [obstacle for obstacle in engine.obstacles if isinstance(obstacle, Thunderstorm)]
    assert 2 <= len(storms) <= 3
    assert all(storm.size <= 2 for storm in storms)
    assert all(not obstacle_intersects_mask(storm, engine.land_mask, safety_margin=1.0) for storm in storms)
    assert all(
        obstacle.distance_to_boundary((base.position.col + 0.5, base.position.row + 0.5))
        >= engine.config.environment.base_obstacle_clearance_cells
        for obstacle in engine.obstacles
        for base in engine.bases
    )


def test_candidate_regions_keep_thirty_kilometres_clear_of_land_base():
    engine = SimulationEngine(ConfigLoader.load(), seed=23)
    base_positions = engine.allocator.sm.get_base_positions()
    candidates = engine.allocator.extractor.extract(engine.allocator.sm).candidate_regions

    assert candidates
    assert all(
        not engine.land_mask[
            candidate["bbox"].col_start:candidate["bbox"].col_end,
            candidate["bbox"].row_start:candidate["bbox"].row_end,
        ].any()
        for candidate in candidates
    )
    assert all(
        engine.allocator.extractor._distance_to_bases(candidate["bbox"], base_positions)
        >= engine.config.environment.base_task_min_distance_cells
        for candidate in candidates
    )


def test_reset_generates_a_fresh_two_base_coastal_scenario():
    config = ConfigLoader.load()
    engine = SimulationEngine(config, seed=7)
    first_positions = tuple(base.position for base in engine.bases)

    assert len(first_positions) == 2
    assert all(engine.land_mask[position.col, position.row] for position in first_positions)
    assert all(
        math.dist(left, right) >= config.environment.base_min_distance_cells
        for index, left in enumerate(first_positions)
        for right in first_positions[index + 1:]
    )

    engine.reset()
    assert tuple(base.position for base in engine.bases) != first_positions
    assert engine.reset_generation == 1
    assert engine.clock.time == 0
    assert all(uav.status == "idle" and not uav.trail for uav in engine.uavs)
    assert any(event["type"] == "environment_reset" for event in engine.allocator.sm.get_recent_events(0))


def test_explicit_reset_seed_rebuilds_the_same_clean_scenario():
    config = ConfigLoader.load()
    engine = SimulationEngine(config, seed=5)
    engine.step()
    engine.reset(seed=31)
    fresh = SimulationEngine(config, seed=31)

    assert tuple(base.position for base in engine.bases) == tuple(base.position for base in fresh.bases)
    assert [(type(item).__name__, item.center, item.size) for item in engine.obstacles] == [
        (type(item).__name__, item.center, item.size) for item in fresh.obstacles
    ]
    assert engine.summary()["steps"] == 0


def test_frame_exposes_two_bases_and_reset_scenario_metadata():
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    engine.reset(seed=43)
    frame = build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )

    assert len(frame["bases"]) == 2
    assert [base["capacity"] for base in frame["bases"]] == [3, 3]
    assert frame["scenario_seed"] == 43
    assert frame["reset_generation"] == 1
    assert frame["task_area"] == {
        "width_km": 300,
        "height_km": 300,
        "cell_size_km": 10,
    }


def test_unobserved_ships_are_not_exported_to_the_visualization_or_llm_state():
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    contact = engine.ships[0]

    initial_frame = build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )
    assert initial_frame["ships"] == []
    assert engine.allocator.sm.get_target_reports() == []

    engine._handle_detection(engine.uavs[0], contact, 1.0)
    discovered_frame = build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )

    assert [ship["id"] for ship in discovered_frame["ships"]] == [contact.id]
    report = engine.allocator.sm.get_target_report(contact.group_id)
    assert report is not None
    assert report.position == contact.position


def test_target_detection_retires_overlapping_search_and_redirects_uav(monkeypatch):
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    contact = engine.ships[0]
    observer, searcher = engine.uavs[:2]
    col, row = contact.position
    conflict = Region(
        id="S-conflict",
        bbox=BBox(
            max(0, col - 2),
            max(0, row - 2),
            min(30, col + 3),
            min(30, row + 3),
        ),
        type="search",
        assigned_uav_id=searcher.id,
    )
    engine.allocator.sm.set_search_regions([conflict])
    searcher.status = "searching"
    engine.allocator.sm.update_uav_status(
        searcher.id,
        "searching",
        searcher.position,
        assigned_region_id=conflict.id,
    )
    redirected = []
    monkeypatch.setattr(engine, "_resume_search", lambda entity: False)
    monkeypatch.setattr(
        engine,
        "_begin_return",
        lambda entity, current_time: redirected.append((entity.id, current_time)),
    )

    engine._handle_detection(observer, contact, 1.0)

    track = engine.allocator.sm.get_track_region_for_group(contact.group_id)
    assert track is not None
    assert engine.allocator.sm.get_search_regions() == []
    assert engine.allocator.sm.get_uav(searcher.id).assigned_region_id is None
    assert redirected == [(searcher.id, 1.0)]


def test_moving_track_region_retires_newly_overlapping_search(monkeypatch):
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    searcher = engine.uavs[1]
    conflict = Region(
        id="S-moving-conflict",
        bbox=BBox(12, 12, 17, 17),
        type="search",
        assigned_uav_id=searcher.id,
    )
    engine.allocator.sm.set_search_regions([conflict])
    searcher.status = "transit"
    engine.allocator.sm.update_uav_status(
        searcher.id,
        "transit",
        searcher.position,
        assigned_region_id=conflict.id,
    )
    track = engine.allocator.sm.create_track_region("G-moving", GridCoord(25, 25))
    track.assigned_uav_id = engine.uavs[0].id
    engine.allocator.sm.update_track_region_center(track.id, GridCoord(14, 14))
    redirected = []
    monkeypatch.setattr(engine, "_resume_search", lambda entity: False)
    monkeypatch.setattr(
        engine,
        "_begin_return",
        lambda entity, current_time: redirected.append((entity.id, current_time)),
    )

    engine._resolve_search_track_conflicts(5.0)

    assert engine.allocator.sm.get_search_regions() == []
    assert redirected == [(searcher.id, 5.0)]


def test_tracker_return_keeps_only_last_observed_contact_for_handoff():
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    contact = engine.ships[0]
    uav = engine.uavs[0]
    engine._handle_detection(uav, contact, 1.0)

    engine._begin_return(uav, 2.0)

    assert uav.status == "returning"
    assert engine.allocator.sm.get_track_region_for_group(contact.group_id) is None
    report = engine.allocator.sm.get_target_report(contact.group_id)
    assert report is not None
    candidates = engine.allocator.extractor.extract(engine.allocator.sm).candidate_regions
    handoffs = [item for item in candidates if item.get("target_group_id") == contact.group_id]
    assert handoffs


def test_frame_heading_uses_recent_motion_not_only_planned_heading():
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    uav = engine.uavs[0]
    contact = engine.ships[0]
    contact.mark_detected()
    uav.heading_rad = 0.0
    uav.trail = [(4.0, 4.0), (4.0, 5.0)]
    contact.trail = [(12.0, 12.0), (13.0, 12.0)]

    frame = build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )

    assert frame["uavs"][0]["heading_deg"] == 90.0
    assert frame["ships"][0]["heading_deg"] == 0.0


def test_frame_exposes_continuous_sar_and_eo_beam_geometry():
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    sar_uav, eo_uav = engine.uavs[:2]
    sar_uav.status = "searching"
    sar_uav.sensor_mode = "sar"
    sar_uav.sar_footprint = sar_uav.sar_sensor.compute_swath_footprint(
        sar_uav.float_position,
        sar_uav.heading_rad,
        sar_uav.sar_look_direction,
        along_track_cells=sar_uav.sar_along_track_cells,
    )
    eo_uav.status = "tracking"
    eo_uav.sensor_mode = "eo"
    eo_uav.eo_fov = eo_uav.eo_sensor.compute_fov(
        eo_uav.float_position,
        eo_uav.heading_rad,
        (eo_uav.float_position[0] + 1.0, eo_uav.float_position[1]),
    )

    frame = build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )

    sar_frame = next(uav for uav in frame["uavs"] if uav["id"] == sar_uav.id)
    eo_frame = next(uav for uav in frame["uavs"] if uav["id"] == eo_uav.id)
    assert len(sar_frame["sar_beam"]["polygon"]) == 4
    assert sar_frame["sar_beam"]["look_direction"] in {"left", "right"}
    assert len(eo_frame["eo_fov"]["polygon"]) == 3
    assert math.isclose(eo_frame["eo_fov"]["half_angle"], math.radians(4.0))


def test_frame_exposes_search_tasks_as_colored_grid_cell_sets():
    engine = SimulationEngine(ConfigLoader.load(), seed=41)
    engine.allocator.sm.set_search_regions([
        Region(id="S-cells", bbox=BBox(8, 8, 13, 13), type="search"),
    ])
    frame = build_frame(
        engine.allocator.sm,
        cycle=0,
        config=engine.config,
        ships=engine.ships,
        uav_entities=engine.uavs,
        obstacles=engine.obstacles,
        bases=engine.bases,
    )

    cells = frame["search_regions"][0]["cells"]
    assert cells
    assert len(cells) < 25
    assert all(8 <= col < 13 and 8 <= row < 13 for col, row in cells)


def test_base_capacity_sends_fourth_arrival_to_holding_then_refuels():
    config = ConfigLoader.load()
    config.environment.base_count = 1
    engine = SimulationEngine(config, seed=11)
    base = engine.base
    uavs = engine.uavs[:4]
    for uav in uavs:
        uav.position = base.position
        uav.status = "refueling"
        engine._return_base_by_uav[uav.id] = base

    engine._process_refuelling(0.0)

    assert base.occupancy == 3
    assert uavs[3].status == "holding"
    assert base.is_busy

    for _ in range(int(base.refuel_time_min)):
        engine._process_refuelling(1.0)

    assert uavs[3].status == "refueling"
    assert base.is_refueling(uavs[3].id)


def test_base_station_rejects_over_capacity_directly():
    base = BaseStation(GridCoord(0, 0), refuel_time_min=3, capacity=3)
    assert [base.land_uav(f"UAV-{index}") for index in range(1, 4)] == [True, True, True]
    assert not base.land_uav("UAV-4")
    assert base.step(3) == ["UAV-1", "UAV-2", "UAV-3"]
    assert base.can_accept()


def test_carrier_formation_always_has_two_destroyer_escorts():
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    carriers = [ship for ship in engine.ships if ship.ship_type is ShipType.AIRCRAFT_CARRIER]

    assert len(carriers) <= engine.config.ship.carrier_max
    if carriers:
        carrier = carriers[0]
        escorts = [
            ship for ship in engine.ships
            if ship.group_id == carrier.group_id and ship.ship_type is ShipType.DESTROYER
        ]
        assert len(escorts) >= 2
        assert all(ship.base_heading == carrier.base_heading for ship in escorts)


def test_target_ship_changes_course_and_starts_zigzagging_when_tracked():
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    ship = engine.ships[0]
    ship._phase = 0.0
    original_heading = ship.base_heading

    ship.set_tracked(True)

    assert ship.is_evading
    assert ship.base_heading == original_heading
    assert ship.heading_rad == original_heading

    headings = []
    for _ in range(18):
        previous = ship.heading_rad
        ship.step(1.0)
        headings.append(ship.heading_rad)
        delta = abs((ship.heading_rad - previous + math.pi) % (2 * math.pi) - math.pi)
        assert math.degrees(delta) <= engine.config.ship.max_turn_rate_deg_min + 1e-6

    assert any(abs((heading - original_heading + math.pi) % (2 * math.pi) - math.pi) > math.radians(1) for heading in headings)


def test_target_ship_nomoto_response_has_bounded_yaw_and_turn_speed_loss():
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    ship = engine.ships[0]
    ship._col = ship._row = 15.0
    ship._base_heading = ship.heading_rad = 0.0
    ship.set_tracked(True)
    nominal_step = ship.speed_cells_per_min
    positions = [ship.float_position]
    rates = []

    for _ in range(48):
        ship.step(1.0)
        positions.append(ship.float_position)
        rates.append(abs(ship.turn_rate_deg_min))

    distances = [math.dist(start, end) for start, end in zip(positions, positions[1:])]
    assert max(rates) <= engine.config.ship.max_turn_rate_deg_min + 1e-6
    assert max(distances) <= nominal_step + 1e-9
    assert any(distance < nominal_step * 0.999 for distance in distances)


def test_dissipated_storm_is_replaced_at_configured_density():
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    storm = next(item for item in engine.obstacles if hasattr(item, "intensity"))
    storm.lifetime = 0.5
    initial_id = storm.id

    engine._update_obstacles()

    storms = [item for item in engine.obstacles if hasattr(item, "intensity")]
    assert len(storms) == engine._storm_target_count
    assert initial_id not in {item.id for item in storms}
    assert all(storm.size <= 2 for storm in storms)
    assert all(not obstacle_intersects_mask(storm, engine.land_mask, safety_margin=1.0) for storm in storms)
    assert all(
        storm.distance_to_boundary((base.position.col + 0.5, base.position.row + 0.5))
        >= engine.config.environment.base_obstacle_clearance_cells
        for storm in storms
        for base in engine.bases
    )

import math

from src.env.base_station import BaseStation
from src.env.obstacle import Island, Thunderstorm, obstacle_intersects_mask
from src.env.simulation import SimulationEngine
from src.env.ship import ShipType
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord
from src.vis.backend.frame_builder import build_frame


def test_default_engine_has_two_coastal_bases_with_three_refuelling_slots_each():
    config = ConfigLoader.load()
    engine = SimulationEngine(config, seed=19)

    assert len(engine.bases) == 2
    cols, rows = config.grid.resolution
    assert all(base.capacity == 3 for base in engine.bases)
    assert all(
        base.position.col in {0, cols - 1} or base.position.row in {0, rows - 1}
        for base in engine.bases
    )
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
    for position in first_positions:
        assert (
            position.col <= config.environment.base_land_margin
            or position.col >= config.grid.resolution[0] - 1 - config.environment.base_land_margin
            or position.row <= config.environment.base_land_margin
            or position.row >= config.grid.resolution[1] - 1 - config.environment.base_land_margin
        )
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

import math

from src.env.base_station import BaseStation
from src.env.simulation import SimulationEngine
from src.env.ship import ShipType
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord


def test_reset_generates_separated_coastal_bases():
    config = ConfigLoader.load()
    config.environment.base_count = 3
    engine = SimulationEngine(config, seed=7)
    first_positions = tuple(base.position for base in engine.bases)

    assert len(first_positions) == 3
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

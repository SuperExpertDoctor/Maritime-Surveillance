from dataclasses import replace

import numpy as np

from src.control.heuristic.navigation import AStarNavigator
from src.env.ship import create_ship_population
from src.schedule.config_loader import ConfigLoader


def test_population_exposes_vessel_class_separately_from_activity():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(config.ship, initial_ship_count=4, target_ship_count=2),
    )
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True

    ships = create_ship_population(config, 17, mask, AStarNavigator())

    assert sum(ship.vessel_class == "research" for ship in ships) == 2
    assert sum(ship.vessel_class == "civilian" for ship in ships) == 2
    assert all(ship.activity == "unknown" for ship in ships)
    assert all(ship.radar_emitter is not None for ship in ships if ship.vessel_class == "research")
    assert all(ship.radar_emitter is None for ship in ships if ship.vessel_class == "civilian")


def test_public_ship_truth_does_not_expose_activity_schedule():
    config = ConfigLoader.load()
    config = replace(config, ship=replace(config.ship, initial_ship_count=1, target_ship_count=1))
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True
    ship = create_ship_population(config, 21, mask, AStarNavigator())[0]

    assert ship.truth.vessel_class == "research"
    assert ship.truth.activity_schedule
    assert not hasattr(ship, "public_activity_schedule")

from dataclasses import replace

import numpy as np

from src.control.heuristic.navigation import AStarNavigator
from src.env.ship import create_ship_population
from src.schedule.config_loader import ConfigLoader


def test_population_exposes_vessel_class_separately_from_activity():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(
            config.ship,
            population=replace(
                config.ship.population,
                total_count=4,
                type_i_ratio=0.5,
                type_ii_ratio=0.5,
            ),
        ),
    )
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True

    ships = create_ship_population(config, 17, mask, AStarNavigator())

    assert sum(ship.vessel_class == "type_ii" for ship in ships) == 2
    assert sum(ship.vessel_class == "type_i" for ship in ships) == 2
    assert all(ship.activity == "unknown" for ship in ships)
    assert all(ship.radar_emitter is not None for ship in ships if ship.vessel_class == "type_ii")
    assert all(ship.radar_emitter is None for ship in ships if ship.vessel_class == "type_i")


def test_public_ship_truth_does_not_expose_activity_schedule():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(
            config.ship,
            population=replace(
                config.ship.population,
                total_count=1,
                type_i_ratio=0.0,
                type_ii_ratio=1.0,
            ),
        ),
    )
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True
    ship = create_ship_population(config, 21, mask, AStarNavigator())[0]

    assert ship.truth.vessel_class == "type_ii"
    assert ship.truth.activity_schedule
    assert not hasattr(ship, "public_activity_schedule")

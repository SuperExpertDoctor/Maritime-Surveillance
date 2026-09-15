
import numpy as np
import pytest

from src.control.heuristic.navigation import AStarNavigator
from src.env.ship import create_ship_population
from src.env.ship_navigation import reflect_velocity
from src.schedule.config_loader import ConfigLoader


def test_reflection_reverses_only_outward_component():
    assert reflect_velocity((2.0, 1.0), normal=(1.0, 0.0)) == pytest.approx((-2.0, 1.0))


def test_population_uses_closed_patrol_for_actual_motion():
    config = ConfigLoader.load()
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True
    ships = create_ship_population(config, 29, mask, AStarNavigator())

    for ship in ships:
        for _ in range(480):
            actual = ship.step(1.0)
            assert all(
                0.0 <= pose[0] < mask.shape[0]
                and 0.0 <= pose[1] < mask.shape[1]
                for pose in actual
            )
        assert not ship.departed
        assert all(
            0.0 <= pose[0] < mask.shape[0]
            and 0.0 <= pose[1] < mask.shape[1]
            for pose in ship.trail
        )


def test_patrol_route_is_deterministic_and_does_not_depend_on_truth_class():
    config = ConfigLoader.load()
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True
    civilian = create_ship_population(
        config, 31, mask, AStarNavigator()
    )
    config_research = config
    # The compatibility target count changes only hidden class assignment.
    from dataclasses import replace
    config_research = replace(
        config_research,
        ship=replace(config_research.ship, target_ship_count=config.ship.initial_ship_count),
    )
    research = create_ship_population(config_research, 31, mask, AStarNavigator())
    assert [ship.patrol_route for ship in civilian] == [ship.patrol_route for ship in research]

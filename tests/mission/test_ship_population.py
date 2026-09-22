from dataclasses import replace
import math
import random

import numpy as np
import pytest

from src.control.heuristic.navigation import AStarNavigator
from src.env import ship as ship_module
from src.mission.contracts import ship_rng_manifest
from src.schedule.config_loader import ConfigLoader


def _config(initial_count: int, type_ii_count: int, ais_probability: float = 0.5):
    config = ConfigLoader.load()
    return replace(
        config,
        ship=replace(
            config.ship,
            population=replace(
                config.ship.population,
                total_count=initial_count,
                type_i_ratio=(initial_count - type_ii_count) / initial_count
                if initial_count else 1.0,
                type_ii_ratio=type_ii_count / initial_count
                if initial_count else 0.0,
            ),
            type_ii_ais_on_probability=ais_probability,
        ),
    )


def _water_mask() -> np.ndarray:
    mask = np.zeros((30, 30), dtype=bool)
    mask[:5, :] = True
    mask[16:19, 12:17] = True
    return mask


def _create_ship_population(*args, **kwargs):
    assert hasattr(ship_module, "create_ship_population"), "population factory is missing"
    return ship_module.create_ship_population(*args, **kwargs)


def _snapshot(ships):
    return [
        (
            ship.id,
            ship.vessel_class,
            ship.ais_enabled,
            ship.pose,
            ship.speed_kn,
            ship.ship_type,
            ship.normal_route,
        )
        for ship in ships
    ]


@pytest.mark.parametrize(
    ("initial_count", "type_ii_count"),
    ((0, 0), (1, 0), (1, 1), (8, 0), (8, 8), (8, 3)),
)
def test_population_has_exact_configured_class_counts(initial_count, type_ii_count):
    ships = _create_ship_population(
        _config(initial_count, type_ii_count),
        seed=713,
        land_mask=_water_mask(),
        navigator=AStarNavigator(),
    )

    assert len(ships) == initial_count
    assert sum(ship.vessel_class == "type_ii" for ship in ships) == type_ii_count
    assert sum(ship.vessel_class == "type_i" for ship in ships) == initial_count - type_ii_count
    assert [ship.id for ship in ships] == [f"Ship-{index}" for index in range(1, initial_count + 1)]
    assert all("ship" in ship.id.lower() for ship in ships)


def test_population_is_reproducible_and_each_ship_has_a_legal_independent_route():
    mask = _water_mask()
    config = _config(8, 3)

    first = _create_ship_population(config, 991, mask, AStarNavigator())
    repeated = _create_ship_population(config, 991, mask, AStarNavigator())

    assert _snapshot(first) == _snapshot(repeated)
    assert len({ship.normal_route for ship in first}) == len(first)
    assert len({ship.float_position for ship in first}) == len(first)
    for index, ship in enumerate(first):
        col, row = ship.float_position
        assert not mask[math.floor(col), math.floor(row)]
        assert ship.pose == ship.normal_route[0]
        exit_col, exit_row = ship.normal_route[-1][:2]
        assert exit_col in (0.0, mask.shape[0] - 1.0) or exit_row in (0.0, mask.shape[1] - 1.0)
        assert not mask[math.floor(exit_col), math.floor(exit_row)]
        for pose in ship.normal_route:
            assert 0 <= pose[0] < mask.shape[0] and 0 <= pose[1] < mask.shape[1]
            assert not mask[math.floor(pose[0]), math.floor(pose[1])]
        assert all(
            math.dist(ship.float_position, other.float_position) >= 1.0
            for other in first[:index]
        )


@pytest.mark.parametrize("clearance", [.1, 1.2])
def test_mainland_population_starts_with_clearance_and_stopping_room(clearance):
    from src.env.obstacle import mainland_land_mask
    config = _config(8, 3)
    config = replace(config, ship=replace(config.ship, navigation_clearance_cells=clearance))
    mask = mainland_land_mask(config.grid.resolution, config.environment.mainland_width_cells)
    first = _create_ship_population(config, 27, mask, AStarNavigator())
    repeated = _create_ship_population(config, 27, mask, AStarNavigator())
    assert _snapshot(first) == _snapshot(repeated)
    for ship in first:
        planner = ship.navigator
        assert planner.segment_is_safe(ship.pose, ship.pose, mask), ship.pose
        assert planner.can_stop(ship._motion_state(), mask), ship.pose
        assert all(planner.segment_is_safe(a, b, mask)
                   for a, b in zip(ship.normal_route, ship.normal_route[1:]))
        assert planner._exit_gate(mask) is not None
        start = ship.pose
        for _ in range(3):
            ship.step(.1)
        assert ship.pose != start
        assert ship._motion_time_min == pytest.approx(.3)


def test_class_randomness_does_not_change_normal_vessel_state():
    type_i_ships = _create_ship_population(_config(8, 0), 417, _water_mask(), AStarNavigator())
    type_ii_ships = _create_ship_population(_config(8, 8), 417, _water_mask(), AStarNavigator())

    assert [ship.id for ship in type_i_ships] == [ship.id for ship in type_ii_ships]
    assert [ship.pose for ship in type_i_ships] == [ship.pose for ship in type_ii_ships]
    assert [ship.normal_route for ship in type_i_ships] == [ship.normal_route for ship in type_ii_ships]
    assert [ship.speed_kn for ship in type_i_ships] == [ship.speed_kn for ship in type_ii_ships]
    assert [ship.ship_type for ship in type_i_ships] == [ship.ship_type for ship in type_ii_ships]


def test_population_raises_explicit_error_when_vessels_cannot_be_placed():
    mask = np.ones((5, 5), dtype=bool)
    mask[2, 2] = False

    error_type = getattr(ship_module, "PopulationPlacementError", RuntimeError)
    with pytest.raises(error_type) as caught:
        _create_ship_population(_config(2, 1), 8, mask, AStarNavigator())

    assert caught.value.ship_index == 0
    assert caught.value.attempt_count == 200


def test_legacy_group_id_is_only_a_read_only_contact_view():
    ships = _create_ship_population(_config(1, 0), 417, _water_mask(), AStarNavigator())
    ship = ships[0]
    assert ship.group_id == ship.contact_id == ship.ship_id == ship.id
    with pytest.raises(AttributeError):
        ship.group_id = "formation"


def test_recorded_rng_manifest_replays_class_and_ais_selection():
    config = _config(8, 3)
    seed = 417
    manifest = ship_rng_manifest(seed)
    slots = list(range(8))
    random.Random(manifest["ship_class"]).shuffle(slots)
    type_ii_slots = set(slots[:3])
    ais_rng = random.Random(manifest["ship_ais_enabled"])
    expected = [
        ("type_ii", ais_rng.random() < 0.5)
        if index in type_ii_slots else ("type_i", True)
        for index in range(8)
    ]
    ships = _create_ship_population(config, seed, _water_mask(), AStarNavigator())
    assert [(ship.vessel_class, ship.ais_enabled) for ship in ships] == expected


def test_engine_uses_actual_land_and_islands_for_population(monkeypatch):
    from src.env.obstacle import Island, obstacle_grid_mask
    from src.env import simulation as simulation_module
    from src.env.simulation import SimulationEngine

    # Initialization checks credentials but does not call the LLM.
    monkeypatch.setenv("LONGCAT_API_KEY", "t02-offline-test")

    captured = {}
    original_create_ship_population = simulation_module.create_ship_population

    def capture_population(config, seed, land_mask, navigator):
        captured["config"] = config
        captured["seed"] = seed
        captured["land_mask"] = np.asarray(land_mask, dtype=bool).copy()
        captured["navigator"] = navigator
        return original_create_ship_population(config, seed, land_mask, navigator)

    monkeypatch.setattr(
        simulation_module,
        "create_ship_population",
        capture_population,
    )
    engine = SimulationEngine(_config(8, 3), seed=417)
    expected_mask = engine.land_mask | obstacle_grid_mask(
        [obstacle for obstacle in engine.obstacles if isinstance(obstacle, Island)],
        engine.config.grid.resolution, include_islands=True,
    )
    assert captured["config"] is engine.config
    assert captured["seed"] == engine.seed
    assert captured["navigator"] is engine.ship_navigator
    assert np.array_equal(captured["land_mask"], expected_mask)
    assert np.array_equal(captured["land_mask"], engine.ship_land_mask)
    for ship in engine.ships:
        for pose in ship.normal_route:
            assert not captured["land_mask"][math.floor(pose[0]), math.floor(pose[1])]
    # T06: population truth is no longer a scheduling position provider.
    assert not hasattr(engine, "_group_center")
    assert {c.ais_mmsi for c in engine.allocator.sm.contacts.list_snapshots()} == {
        ship.ais_signal.mmsi for ship in engine.ships if ship.ais_signal is not None
    }


def test_population_passes_configured_survey_speed_to_ship():
    config = _config(1, 1)
    object.__setattr__(config.mission, "activity", replace(
        config.mission.activity, survey_command_speed_kn=11.,
    ))
    ship = _create_ship_population(config, 713, _water_mask(), AStarNavigator())[0]
    start, _ = ship.truth.activity_schedule[0]
    ship._set_activity_for_time(start)
    assert ship.activity_speed_kn == 11.
    assert ship.normal_speed_kn == config.ship.speed_kn

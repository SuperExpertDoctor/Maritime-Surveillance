from dataclasses import fields, replace
from types import SimpleNamespace

import numpy as np
import pytest

from src.control.heuristic.navigation import AStarNavigator
from src.env.ais_signal import AISSignal, generate_ais_signal
from src.env import ship as ship_module
from src.env.ship import Ship
from src.schedule.datatypes import GridCoord
from src.schedule.config_loader import ConfigLoader


def _population(*, type_ii_count: int, probability: float = 1.0):
    assert hasattr(ship_module, "create_ship_population"), "population factory is missing"
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(
            config.ship,
            population=replace(
                config.ship.population,
                total_count=8,
                type_i_ratio=(8 - type_ii_count) / 8,
                type_ii_ratio=type_ii_count / 8,
            ),
            type_ii_ais_on_probability=probability,
        ),
    )
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True
    return ship_module.create_ship_population(config, 2026, mask, AStarNavigator())


@pytest.fixture
def population():
    return _population(type_ii_count=3)


def test_type_i_cannot_disable_ais():
    ship = Ship(
        "Ship-I",
        GridCoord(10, 10),
        10.0,
        vessel_class="type_i",
        ais_enabled=True,
    )

    with pytest.raises(ValueError, match="type_i_ais_required"):
        ship.set_ais_enabled(False)
    assert ship.ais_enabled is True


def test_type_ii_signal_follows_runtime_boolean():
    ship = Ship(
        "Ship-II",
        GridCoord(10, 10),
        10.0,
        vessel_class="type_ii",
        ais_enabled=False,
    )

    assert generate_ais_signal(ship, 1.0) is None
    ship.set_ais_enabled(True)
    assert generate_ais_signal(ship, 1.1) is not None


def test_type_i_always_broadcasts(population):
    for ship in population:
        if ship.vessel_class == "type_i":
            assert all(generate_ais_signal(ship, t) is not None for t in (0, 1, 10))


def test_type_ii_ais_uses_configured_bernoulli_extremes():
    always_on = _population(type_ii_count=8, probability=1.0)
    always_silent = _population(type_ii_count=8, probability=0.0)

    assert all(ship.ais_enabled is True for ship in always_on)
    assert all(ship.ais_enabled is False for ship in always_silent)
    assert all(generate_ais_signal(ship, 3.0) is not None for ship in always_on)
    assert all(generate_ais_signal(ship, 3.0) is None for ship in always_silent)


def test_class_permutation_does_not_change_visible_ais_reports():
    type_i_ships = _population(type_ii_count=0)
    type_ii_ships = _population(type_ii_count=8)

    for type_i, type_ii in zip(type_i_ships, type_ii_ships):
        type_ii.set_ais_enabled(True)
        assert generate_ais_signal(type_i, 6.5) == generate_ais_signal(type_ii, 6.5)


def test_visible_ais_uses_generic_names_types_and_configured_noise(population):
    for ship in population:
        signal = generate_ais_signal(ship, 11.0)
        if signal is None:
            continue
        assert signal.ship_name.startswith("MV-")
        assert "CIV" not in signal.ship_name.upper()
        assert "UNKNOWN" not in signal.ship_name.upper()
        assert signal.ship_type == "Cargo"
        assert signal.reported_speed_kn == ship.speed_kn
        assert np.linalg.norm(np.subtract(signal.reported_position, ship.float_position)) <= (
            ship.ais_position_noise_cells + 1e-12
        )


def test_ais_signal_schema_contains_no_environment_fields():
    names = {field.name for field in fields(AISSignal)}

    assert "truth" not in names
    assert "vessel_class" not in names
    assert "ais_enabled" not in names


def test_ais_class_flip_preserves_every_report_field():
    ship = SimpleNamespace(
        id="Ship-12", vessel_class="type_i", ais_enabled=True,
        float_position=(12.5, 8.25), speed_kn=16.0,
        heading_rad=0.7, ais_position_noise_cells=0.05,
    )
    reports = [generate_ais_signal(ship, t) for t in (0, 1, 10)]
    ship.vessel_class = "type_ii"
    assert [generate_ais_signal(ship, t) for t in (0, 1, 10)] == reports
    for signal in reports:
        assert "CIV" not in signal.ship_name.upper()
        assert np.linalg.norm(np.subtract(signal.reported_position, ship.float_position)) <= 0.05 + 1e-12


def test_ais_mmsi_is_unique_and_stable_for_population_ids():
    signals = []
    for index in range(1, 101):
        ship = SimpleNamespace(
            id=f"Ship-{index}", ais_enabled=True, float_position=(12.5, 8.25),
            speed_kn=16.0, heading_rad=0.7, ais_position_noise_cells=0.05,
        )
        signal = generate_ais_signal(ship, 0)
        assert signal.mmsi == generate_ais_signal(ship, 10).mmsi
        assert len(signal.mmsi) == 9 and signal.mmsi.isdigit()
        signals.append(signal)
    assert len({signal.mmsi for signal in signals}) == 100


def test_mixed_population_keeps_type_i_on_with_independent_type_ii_draws():
    ships = _population(type_ii_count=8, probability=0.5)
    assert {ship.ais_enabled for ship in ships} == {True, False}
    mixed = _population(type_ii_count=3, probability=0.0)
    assert all(
        (generate_ais_signal(ship, 0) is not None) == (ship.vessel_class == "type_i")
        for ship in mixed
    )

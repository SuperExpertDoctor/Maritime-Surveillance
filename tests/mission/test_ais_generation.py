from dataclasses import fields, replace
from types import SimpleNamespace

import numpy as np
import pytest

from src.control.heuristic.navigation import AStarNavigator
from src.env.ais_signal import AISSignal, generate_ais_signal
from src.env import ship as ship_module
from src.schedule.config_loader import ConfigLoader


def _population(*, target_count: int, probability: float = 1.0):
    assert hasattr(ship_module, "create_ship_population"), "population factory is missing"
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(
            config.ship,
            initial_ship_count=8,
            target_ship_count=target_count,
            target_ais_on_probability=probability,
        ),
    )
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[: config.environment.mainland_width_cells, :] = True
    return ship_module.create_ship_population(config, 2026, mask, AStarNavigator())


@pytest.fixture
def population():
    return _population(target_count=3)


def test_civilian_always_broadcasts(population):
    for ship in population:
        if ship.truth_identity == "civilian":
            assert all(generate_ais_signal(ship, t) is not None for t in (0, 1, 10))


def test_target_ais_mode_uses_configured_bernoulli_extremes():
    always_on = _population(target_count=8, probability=1.0)
    always_silent = _population(target_count=8, probability=0.0)

    assert all(ship.ais_mode == "civilian" for ship in always_on)
    assert all(ship.ais_mode == "silent" for ship in always_silent)
    assert all(generate_ais_signal(ship, 3.0) is not None for ship in always_on)
    assert all(generate_ais_signal(ship, 3.0) is None for ship in always_silent)


def test_truth_permutation_does_not_change_visible_ais_reports():
    civilians = _population(target_count=0)
    targets = _population(target_count=8)

    for civilian, target in zip(civilians, targets):
        assert civilian.truth_identity == "civilian"
        assert target.truth_identity == "target"
        assert generate_ais_signal(civilian, 6.5) == generate_ais_signal(target, 6.5)


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


def test_ais_signal_schema_contains_no_truth_fields():
    names = {field.name for field in fields(AISSignal)}

    assert "truth" not in names
    assert "truth_identity" not in names
    assert "actual_military" not in names


def test_ais_identity_flip_preserves_every_report_field():
    ship = SimpleNamespace(
        id="Ship-12", truth_identity="civilian", actual_military=False,
        ais_mode="civilian", float_position=(12.5, 8.25), speed_kn=16.0,
        heading_rad=0.7, ais_position_noise_cells=0.05,
    )
    civilian = [generate_ais_signal(ship, t) for t in (0, 1, 10)]
    ship.truth_identity = "target"
    ship.actual_military = True
    assert [generate_ais_signal(ship, t) for t in (0, 1, 10)] == civilian
    for signal in civilian:
        assert "CIV" not in signal.ship_name.upper()
        assert np.linalg.norm(np.subtract(signal.reported_position, ship.float_position)) <= 0.05 + 1e-12


def test_ais_mmsi_is_unique_and_stable_for_population_ids():
    signals = []
    for index in range(1, 101):
        ship = SimpleNamespace(
            id=f"Ship-{index}", ais_mode="civilian", float_position=(12.5, 8.25),
            speed_kn=16.0, heading_rad=0.7, ais_position_noise_cells=0.05,
        )
        signal = generate_ais_signal(ship, 0)
        assert signal.mmsi == generate_ais_signal(ship, 10).mmsi
        assert len(signal.mmsi) == 9 and signal.mmsi.isdigit()
        signals.append(signal)
    assert len({signal.mmsi for signal in signals}) == 100


def test_mixed_population_keeps_civilians_on_with_independent_target_draws():
    ships = _population(target_count=8, probability=0.5)
    assert {ship.ais_mode for ship in ships} == {"civilian", "silent"}
    mixed = _population(target_count=3, probability=0.0)
    assert all(
        (generate_ais_signal(ship, 0) is not None) == (ship.truth_identity == "civilian")
        for ship in mixed
    )

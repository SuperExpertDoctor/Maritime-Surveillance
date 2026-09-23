from collections import Counter

from src.mission.contracts import VesselCommand
from src.schedule.config_loader import ConfigLoader
from tests.mission.test_coverage_scan_integration import _engine


def test_default_population_is_two_with_five_total_and_class_limits():
    config = ConfigLoader.load().ship
    assert config.population.total_count == 2
    assert config.opponent_population.max_active == 5
    assert config.opponent_population.max_active_type_i == 2
    assert config.opponent_population.max_active_type_ii == 3


def test_manual_and_automatic_creates_share_class_and_total_limits():
    engine = _engine()
    assert len(engine.ships) == 2
    initial = Counter(ship.vessel_class for ship in engine.ships)
    assert initial == {'type_i': 1, 'type_ii': 1}
    for index, kind in enumerate(('type_i', 'type_i', 'type_ii', 'type_ii', 'type_ii')):
        command = VesselCommand(f'limit-{index}', engine.episode_id, 'create',
                                vessel_class=kind, position_cells=(10.5 + index * 2, 8.5))
        engine.vessel_commands.enqueue(command)
        result, = engine.apply_pending_vessel_commands()
        assert result.status == ('rejected' if index in (1, 4) else 'applied')
    assert Counter(ship.vessel_class for ship in engine.ships) == {'type_i': 2, 'type_ii': 3}


def test_public_config_exposes_the_same_population_limits():
    from src.vis.backend.config_snapshot import configuration_snapshot
    config = configuration_snapshot(ConfigLoader.load())['ship']
    assert config['population']['total_count'] == 2
    assert config['opponent_population']['max_active'] == 5
    assert config['opponent_population']['max_active_type_i'] == 2
    assert config['opponent_population']['max_active_type_ii'] == 3

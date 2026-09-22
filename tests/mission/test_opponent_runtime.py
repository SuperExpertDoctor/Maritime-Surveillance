from scripts.evaluate_mixed_maritime import _FixtureGateway
from dataclasses import replace
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader


def engine():
    return SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=_FixtureGateway())


def test_departed_detected_ship_is_not_in_commander_snapshot():
    sim = engine()
    ship = next(s for s in sim.ships if s.vessel_class == "type_ii")
    sim.surveillance_stages.set_fact(ship.id, "sar", True, 0., "test-sar")
    ship.departed = True
    sim._prepare_red_decision(0.)
    assert ship.id not in {s.ship_id for s in sim.red_commander._last_snapshot.ships}


def test_new_plan_with_same_parameters_restarts_phase_only_on_installation():
    sim = engine()
    ship = next(s for s in sim.ships if s.vessel_class == "type_ii")
    sim.surveillance_stages.set_fact(ship.id, "sar", True, 0., "test-sar")
    sim._prepare_red_decision(0.)
    assert ship._navigation_params is not None
    sim._prepare_red_decision(.5)
    assert ship._navigation_installed_at_min == 0.
    sim._prepare_red_decision(1.)
    assert ship._navigation_installed_at_min == 1.


def test_no_uavs_does_not_crash_opponent_snapshot():
    sim = engine()
    sim.uavs = []
    sim._prepare_red_decision(0.)
    assert sim.red_commander._last_snapshot.uavs == ()


def test_engine_step_applies_scheduled_arrival_through_public_lifecycle():
    config = ConfigLoader.load()
    arrivals = replace(config.ship.opponent_population, enabled=True,
                       interval_min_min=1., interval_max_min=1., type_i_probability=1.)
    config = replace(config, ship=replace(config.ship, opponent_population=arrivals))
    sim = SimulationEngine(config, seed=42, llm_gateway=_FixtureGateway())
    original_count = len(sim.ships)
    sim.clock.time = sim.allocator.sm.current_time = 1.
    sim.step()
    assert len(sim.ships) == original_count + 1
    result = sim.vessel_command_result("opponent-release-00000001")
    assert result.status == "applied"
    assert sim.surveillance_stages.snapshot(result.vessel_id).stage == "undetected"

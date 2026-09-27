from scripts.evaluate_mixed_maritime import _FixtureGateway
from dataclasses import replace
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.mission.contracts import VesselCommand
import pytest


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


@pytest.mark.parametrize("operation", ["create", "delete"])
@pytest.mark.parametrize("auto_first", [False, True])
def test_manual_count_edit_cancels_same_boundary_arrival(operation, auto_first):
    sim = engine()
    sim.opponent_population.config = replace(sim.opponent_population.config,
        enabled=True, interval_min_min=1., interval_max_min=1.)
    sim.config = replace(sim.config, ship=replace(
        sim.config.ship, opponent_population=sim.opponent_population.config))
    sim.clock.time = 100.
    original = len(sim.ships)
    manual = VesselCommand("opponent-release-manual", sim.episode_id, operation,
        vessel_id=sim.ships[0].id if operation == "delete" else None,
        expected_revision=1 if operation == "delete" else None,
        vessel_class="type_ii" if operation == "create" else None,
        position_cells=(12.5, 8.5) if operation == "create" else None)
    if not auto_first:
        sim.vessel_commands.enqueue(manual)
    queued = sim.opponent_population.tick(sim)
    assert queued is not None
    if auto_first:
        sim.vessel_commands.enqueue(manual)
    sim.apply_pending_vessel_commands()
    assert sim.vessel_command_result(manual.command_id).status == "applied"
    assert len(sim.ships) == original + (1 if operation == "create" else -1)
    assert sim.opponent_population.paused_by_manual_edit
    sim.clock.time += 100.
    assert sim.opponent_population.tick(sim) is None
    assert sim.summary()["opponent_population_paused_by_manual_edit"] is True
    assert "opponent_population_paused" in {
        event["type"] for event in sim.allocator.sm.get_recent_events(0)}
    sim.reset()
    assert not sim.opponent_population.paused_by_manual_edit
    sim.clock.time = 100.
    assert sim.opponent_population.tick(sim) is not None


@pytest.mark.parametrize("operation", ["create", "delete", "set_ais"])
def test_invalid_count_edit_or_ais_edit_does_not_pause_arrivals(operation):
    sim = engine()
    sim.opponent_population.config = replace(sim.opponent_population.config, enabled=True)
    ship = next(s for s in sim.ships if s.vessel_class == "type_ii")
    command = VesselCommand("operator-edit", sim.episode_id, operation,
        vessel_id=ship.id if operation != "create" else None,
        expected_revision=(1 if operation == "set_ais" else 999) if operation != "create" else None,
        ais_enabled=False if operation == "set_ais" else None,
        vessel_class="type_ii" if operation == "create" else None,
        position_cells=(0., 0.) if operation == "create" else None)
    sim.vessel_commands.enqueue(command)
    sim.apply_pending_vessel_commands()
    assert sim.vessel_command_result(command.command_id).status == (
        "applied" if operation == "set_ais" else "rejected")
    assert not sim.opponent_population.paused_by_manual_edit
    sim.clock.time = 100.
    result = sim.opponent_population.tick(sim)
    assert result is not None
    sim.apply_pending_vessel_commands()
    assert sim.vessel_command_result(result.command_id).status == "applied"
    assert not sim.opponent_population.paused_by_manual_edit

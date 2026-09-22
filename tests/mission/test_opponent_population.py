from dataclasses import replace
import random
from types import SimpleNamespace

import numpy as np
import pytest

from src.mission.opponent_population import OpponentPopulation
from src.mission.vessel_commands import VesselCommandQueue, VesselCommandResult
from src.schedule.config_loader import OpponentPopulationConfig


def engine():
    events = []
    return SimpleNamespace(
        clock=SimpleNamespace(time=0.0), episode_id="test", ships=[], uavs=[],
        rng=random.Random(42), vessel_commands=VesselCommandQueue(),
        ship_land_mask=np.zeros((24, 20), dtype=bool),
        obstacle_mask=np.zeros((24, 20), dtype=bool),
        allocator=SimpleNamespace(sm=SimpleNamespace(
            add_event=lambda kind, payload: events.append((kind, payload)))),
        events=events,
    )


def population(**kwargs):
    return OpponentPopulation(OpponentPopulationConfig(
        interval_min_min=2, interval_max_min=2, **kwargs), seed=42)


def test_due_once_queues_create_without_mutating_ships_or_navigation_rng():
    e = engine()
    p = population()
    state = e.rng.getstate()
    assert p.tick(e) is None
    e.clock.time = 2
    result = p.tick(e)
    assert result.status == "queued"
    assert p.tick(e) is None
    command, = e.vessel_commands.drain()
    assert command.operation == "create" and command.episode_id == e.episode_id
    assert e.ships == [] and e.rng.getstate() == state
    assert [name for name, _ in e.events] == ["opponent_vessel_release_queued"]
    # Even drained commands occupy a slot until apply/reject finishes.
    e.clock.time = 100
    assert p.tick(e) is None


def test_seed_reproduces_random_times_types_and_positions():
    def run(seed):
        e = engine()
        p = OpponentPopulation(OpponentPopulationConfig(
            interval_min_min=1, interval_max_min=4), seed=seed)
        trace = []
        for t in range(60):
            e.clock.time = t
            if p.tick(e):
                command, = e.vessel_commands.drain()
                trace.append((t, command.vessel_class, command.position_cells))
                e.vessel_commands.complete(VesselCommandResult(
                    command.command_id, "rejected", None, None, "test"))
        return trace
    assert run(42) == run(42)
    assert run(42) != run(43)
    assert len({t2[0] - t1[0] for t1, t2 in zip(run(42), run(42)[1:])}) > 1


@pytest.mark.parametrize("probability,kind", [(0, "type_ii"), (1, "type_i")])
def test_class_probability_endpoints(probability, kind):
    e = engine()
    p = population(type_i_probability=probability)
    e.clock.time = 2
    p.tick(e)
    assert e.vessel_commands.drain()[0].vessel_class == kind


def test_cap_excludes_departed_and_counts_queued_commands():
    e = engine()
    e.ships = [SimpleNamespace(departed=False, float_position=(10, 10))]
    p = population(max_active=1)
    e.clock.time = 2
    assert p.tick(e) is None
    e.ships[0].departed = True
    e.clock.time = 4
    assert p.tick(e).status == "queued"
    e.clock.time = 6
    assert p.tick(e) is None


def test_positions_respect_masks_spacing_uav_distance_and_prefer_edges():
    e = engine()
    e.ship_land_mask[:5, :] = True
    e.obstacle_mask[:, :4] = True
    e.uavs = [SimpleNamespace(float_position=(22, 18))]
    e.ships = [SimpleNamespace(departed=False, float_position=(22, 5))]
    p = population()
    e.clock.time = 2
    p.tick(e)
    x, y = e.vessel_commands.drain()[0].position_cells
    assert 1 <= x < 23 and 1 <= y < 19
    assert not e.ship_land_mask[int(x), int(y)]
    assert not e.obstacle_mask[int(x), int(y)]
    assert min(x, y, 24-x, 20-y) <= 3
    assert np.hypot(x-22, y-18) >= 5
    assert np.hypot(x-22, y-5) >= 1


@pytest.mark.parametrize("block", ["land", "obstacles", "uavs"])
def test_no_safe_position_skips_instead_of_spawning_near_uav(block):
    e = engine()
    if block == "land":
        e.ship_land_mask[:] = True
    elif block == "obstacles":
        e.obstacle_mask[:] = True
    else:
        e.uavs = [SimpleNamespace(float_position=(x, y))
                  for x in range(0, 24, 3) for y in range(0, 20, 3)]
    e.clock.time = 2
    assert population().tick(e) is None
    assert e.vessel_commands.pending() == () and e.events == []


def test_disabled_and_queue_full_are_safe():
    e = engine()
    e.clock.time = 2
    assert population(enabled=False).tick(e) is None
    first = population()
    assert first.tick(e)
    # An external pending creation reserves the last available slot.
    assert population(max_active=1).tick(e) is None
    command, = e.vessel_commands.drain()
    e.vessel_commands = VesselCommandQueue(maxsize=1)
    e.vessel_commands.enqueue(replace(command, command_id="external-create"))
    assert OpponentPopulation(OpponentPopulationConfig(), seed=3).tick(e) is None
    e.clock.time = 100
    assert OpponentPopulation(OpponentPopulationConfig(), seed=3).tick(e) is None


def test_rejected_command_can_retry_without_success_event():
    e = engine()
    p = population()
    e.clock.time = 2
    p.tick(e)
    command, = e.vessel_commands.drain()
    e.vessel_commands.complete(VesselCommandResult(
        command.command_id, "rejected", None, None, "invalid_position"))
    e.clock.time = 4
    assert p.tick(e).status == "queued"
    assert all(kind == "opponent_vessel_release_queued" for kind, _ in e.events)


@pytest.mark.parametrize("probability,kind", [(0, "type_ii"), (1, "type_i")])
def test_real_engine_applies_existing_create_lifecycle(probability, kind):
    from src.env.simulation import SimulationEngine
    from src.schedule.config_loader import ConfigLoader

    e = SimulationEngine(ConfigLoader.load(), seed=42, episode_id="opponent-test")
    p = population(type_i_probability=probability)
    original_count = len(e.ships)
    rng_state = e.rng.getstate()
    e.clock.time = 2
    queued = p.tick(e)
    assert queued is not None and queued.status == "queued"
    assert len(e.ships) == original_count
    result, = e.apply_pending_vessel_commands()
    assert result.status == "applied"
    assert len(e.ships) == original_count + 1
    vessel = e.ships[-1]
    assert vessel.id == result.vessel_id and vessel.vessel_class == kind
    assert e._vessel_revisions[vessel.id] == 1
    assert e.surveillance_stages.snapshot(vessel.id) is not None
    assert vessel.id in e._ship_position_history
    if kind == "type_ii":
        assert vessel.id in e._emitter_track_ids
    assert e.rng.getstate() == rng_state


def test_late_tick_never_catches_up_with_multiple_arrivals():
    e = engine()
    p = population()
    e.clock.time = 1000
    assert p.tick(e)
    command, = e.vessel_commands.drain()
    e.vessel_commands.complete(VesselCommandResult(
        command.command_id, "applied", "new-vessel", 1))
    assert p.tick(e) is None
    assert e.vessel_commands.pending() == ()


def test_interior_fallback_when_edges_blocked_and_nonzero_start_time():
    e = engine()
    e.ship_land_mask[:4, :] = True
    e.ship_land_mask[-4:, :] = True
    e.ship_land_mask[:, :4] = True
    e.ship_land_mask[:, -4:] = True
    p = OpponentPopulation(OpponentPopulationConfig(
        interval_min_min=2, interval_max_min=2), seed=42, start_time=100)
    e.clock.time = 101
    assert p.tick(e) is None
    e.clock.time = 102
    assert p.tick(e)
    x, y = e.vessel_commands.drain()[0].position_cells
    assert 4 < x < 20 and 4 < y < 16


def test_owns_command_tracks_actual_release_even_after_drain():
    e = engine()
    p = population()
    assert not p.owns_command("opponent-release-00000001")
    e.clock.time = 2
    queued = p.tick(e)
    assert p.owns_command(queued.command_id)
    e.vessel_commands.drain()
    assert p.owns_command(queued.command_id)
    assert not p.owns_command("opponent-release-99999999")
    assert not population().owns_command(queued.command_id)


@pytest.mark.parametrize("depart_before_apply", [False, True])
def test_apply_rechecks_automatic_capacity_after_interleaved_manual_create(
    monkeypatch, depart_before_apply,
):
    from scripts.evaluate_mixed_maritime import _FixtureGateway
    from src.env.simulation import SimulationEngine
    from src.mission.contracts import VesselCommand
    from src.schedule.config_loader import ConfigLoader

    config = ConfigLoader.load()
    limit = config.ship.population.total_count + 1
    config = replace(config, ship=replace(config.ship, opponent_population=replace(
        config.ship.opponent_population, max_active=limit,
        interval_min_min=1., interval_max_min=1.,
    )))
    e = SimulationEngine(config, seed=42, llm_gateway=_FixtureGateway())
    p = e.opponent_population
    original_position = p._position
    auto_position = original_position(e)
    manual_position = next(
        point for _ in range(100)
        if np.linalg.norm(np.array(point := original_position(e)) - auto_position) >= 1
    )

    def interleaved_position(current):
        # The API queues this after tick's capacity check but before its enqueue.
        current.vessel_commands.enqueue(VesselCommand(
            command_id="manual-create", episode_id=current.episode_id,
            operation="create", vessel_id=None, expected_revision=None,
            vessel_class="type_i", position_cells=manual_position,
        ))
        return auto_position

    monkeypatch.setattr(p, "_position", interleaved_position)
    e.clock.time = e.allocator.sm.current_time = 1.
    queued = p.tick(e)
    if depart_before_apply:
        e.ships[0].departed = True
    manual, automatic = e.apply_pending_vessel_commands()
    assert manual.status == "applied"
    assert automatic.command_id == queued.command_id
    assert automatic.status == ("applied" if depart_before_apply else "rejected")
    assert automatic.error_code == (None if depart_before_apply else "opponent_capacity_reached")
    assert sum(not ship.departed for ship in e.ships) == limit
    assert e.vessel_commands.get(queued.command_id) == automatic
    created = [event for event in e.allocator.sm.get_recent_events(0)
               if event["type"] == "vessel_created"]
    assert len(created) == 1 + depart_before_apply

    # Manual commands remain an operator override, even with a release-like ID.
    monkeypatch.setattr(p, "_position", original_position)
    e.vessel_commands.enqueue(VesselCommand(
        command_id="opponent-release-99999999", episode_id=e.episode_id,
        operation="create", vessel_id=None, expected_revision=None,
        vessel_class="type_i", position_cells=original_position(e),
    ))
    override, = e.apply_pending_vessel_commands()
    assert override.status == "applied"
    assert sum(not ship.departed for ship in e.ships) == limit + 1

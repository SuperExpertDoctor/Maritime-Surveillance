from src.mission.contracts import IntentCommand


def test_fixture_factory_covers_all_eight_scenarios(scenario_factory):
    assert set(scenario_factory.SCENARIOS) == {
        "mixed-ais",
        "all-civilian",
        "silent-target",
        "disguised-target",
        "island-confounder",
        "no-resources",
        "intent-overlap",
        "model-failure",
    }
    engines = {
        scenario: scenario_factory.engine(scenario, seed=42)
        for scenario in scenario_factory.SCENARIOS
    }

    assert all(
        len(engine.ships) == engine.config.ship.initial_ship_count
        for engine in engines.values()
    )
    assert all(
        ship.truth_identity == "civilian"
        for ship in engines["all-civilian"].ships
    )
    assert not any(
        ship.ais_signal is not None
        for ship in engines["silent-target"].ships
        if ship.truth_identity == "target"
    )
    assert len(engines["island-confounder"].obstacles) >= 1
    assert not engines["no-resources"].allocator.sm.get_available_uavs()


def test_ais_contacts_start_unknown_and_disguised_target_needs_observation(scenario_factory):
    engine = scenario_factory.engine("disguised-target", seed=42)
    target_count = sum(ship.truth_identity == "target" for ship in engine.ships)
    contacts = engine.allocator.sm.contacts.list_snapshots()

    assert target_count > 0
    assert contacts
    assert all(contact.identity == "unknown" for contact in contacts)
    assert all(contact.last_assessment is None for contact in contacts)


def test_intent_command_is_applied_only_at_the_simulation_boundary(scenario_factory):
    engine = scenario_factory.engine("intent-overlap", seed=42)
    command = IntentCommand(
        "fixture-intent-1",
        engine.episode_id,
        "create",
        None,
        None,
        {
            "label": "fixture focus",
            "bbox": [8, 8, 12, 12],
            "mode": "search_priority",
            "priority": "high",
            "weight": 1.0,
            "valid_duration_min": 30.0,
            "revisit_interval_min": None,
        },
    )
    engine.intent_commands.enqueue(command)

    assert engine.intents.intents() == ()
    engine.apply_pending_intent_commands()
    assert len(engine.intents.intents()) == 1


def test_model_failure_pauses_before_any_clock_or_motion_progress(scenario_factory):
    engine = scenario_factory.engine("model-failure", seed=42)
    target = next(ship for ship in engine.ships if ship.truth_identity == "target")
    target._col = float(engine.uavs[0].float_position[0])
    target._row = float(engine.uavs[0].float_position[1])
    before = tuple(ship.float_position for ship in engine.ships)

    engine.step()

    assert engine.runtime_status == "paused_model"
    assert engine.blocked_role == "red_commander"
    assert engine.clock.time == 0.0
    assert tuple(ship.float_position for ship in engine.ships) == before

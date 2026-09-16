from dataclasses import replace

from scripts.evaluate_mixed_maritime import _FixtureGateway
from scripts.replay_restoration_scenarios import build_scenario
from src.env.emitter import EmitterState
from src.mission.contracts import PassivePosition, VesselCommand
from src.schedule.config_loader import ConfigLoader
from src.env.simulation import SimulationEngine


class _AlwaysOnEmitter:
    def advance(self, _end_min):
        return ()

    def current_burst_at(self, _at_min):
        return EmitterState(True, 99.0, "BURST-INTEGRATION", 0.0)


def _passive_engine(observer_positions):
    config = ConfigLoader.load()
    config = replace(
        config,
        environment=replace(
            config.environment,
            island_count_min=0,
            island_count_max=0,
            thunderstorm_count_min=0,
            thunderstorm_count_max=0,
        ),
        uav=replace(config.uav, count_max=2),
        ship=replace(
            config.ship,
            population=replace(
                config.ship.population,
                total_count=1,
                type_i_ratio=0.0,
                type_ii_ratio=1.0,
            ),
            type_ii_ais_on_probability=0.0,
        ),
        sensor=replace(
            config.sensor,
            passive=replace(
                config.sensor.passive,
                reference_detection_probability=1.0,
                detection_range_cells=20.0,
                received_power_std_db=0.0,
            ),
        ),
    )
    engine = SimulationEngine(
        config,
        seed=42,
        llm_gateway=_FixtureGateway(),
        episode_id="passive-integration",
    )
    ship = engine.ships[0]
    ship._col, ship._row = 5.0, 5.0
    ship.radar_emitter = _AlwaysOnEmitter()
    engine._ship_position_history[ship.id] = [(0.0, ship.float_position)]
    engine._emitter_track_ids[ship.id] = "EMITTER-INTEGRATION"
    for uav, position in zip(engine.uavs, observer_positions):
        uav._col, uav._row = position
        engine._uav_position_history[uav.id] = [(0.0, uav.float_position)]
    return engine


def test_ais_toggle_connects_command_boundary_contact_evidence_and_revision():
    engine = build_scenario("ais-toggle", seed=42, transport="fixture")
    ship = next(
        item
        for item in engine.ships
        if item.vessel_class == "type_ii" and item.ais_enabled
    )

    engine._refresh_ais_signals(1.0)
    signal = ship.ais_signal
    assert signal is not None
    contact_id = next(
        contact.contact_id
        for contact in engine.allocator.sm.contacts.list_snapshots()
        if contact.ais_mmsi == signal.mmsi
    )
    before_contact = engine.allocator.sm.contacts.snapshot(contact_id)
    before_records = tuple(
        record
        for record in engine.allocator.sm.information_policy.evidence_store.all_records()
        if record.source_id == signal.mmsi
    )
    before_history = tuple(engine._ais_history[signal.mmsi])

    disable = VesselCommand(
        "ais-toggle-disable",
        engine.episode_id,
        "set_ais",
        ship.id,
        engine._vessel_revisions[ship.id],
        None,
        None,
        False,
    )
    engine.vessel_commands.enqueue(disable)
    assert engine.apply_pending_vessel_commands()[0].status == "applied"
    engine._refresh_ais_signals(2.0)

    assert ship.ais_signal is None
    assert tuple(engine._ais_history[signal.mmsi]) == before_history
    assert tuple(
        record
        for record in engine.allocator.sm.information_policy.evidence_store.all_records()
        if record.source_id == signal.mmsi
    ) == before_records

    enable = VesselCommand(
        "ais-toggle-enable",
        engine.episode_id,
        "set_ais",
        ship.id,
        engine._vessel_revisions[ship.id],
        None,
        None,
        True,
    )
    engine.vessel_commands.enqueue(enable)
    assert engine.apply_pending_vessel_commands()[0].status == "applied"
    engine._refresh_ais_signals(2.0)

    after_contact = engine.allocator.sm.contacts.snapshot(contact_id)
    after_records = tuple(
        record
        for record in engine.allocator.sm.information_policy.evidence_store.all_records()
        if record.source_id == signal.mmsi
    )
    assert len(engine._ais_history[signal.mmsi]) == len(before_history) + 1
    assert after_contact.revision == before_contact.revision + 1
    assert len(after_records) == len(before_records) + 1
    assert engine.allocator.sm.information_version > 0
    assert any(
        event["type"] == "ais_transmission_changed"
        and event["data"]["vessel_id"] == ship.id
        for event in engine.allocator.sm.get_recent_events(0.0)
    )


def test_passive_gates_publish_bearing_or_position_then_investigation_task():
    one_observer = _passive_engine(((3.0, 5.0), (20.0, 20.0)))
    one_observer.allocator.sm.current_time = 1.0
    one_observer._update_passive_sensors(1.0)
    assert len(one_observer.allocator.sm.get_passive_observations(1.0)) == 1
    assert one_observer.allocator.sm.get_passive_positions(1.0) == ()
    assert any(
        event["type"] == "passive_bearing_observed"
        and "vessel_class" not in event["data"]
        for event in one_observer.allocator.sm.get_recent_events(0.0)
    )

    two_observers = _passive_engine(((3.0, 5.0), (3.0, 7.0)))
    two_observers.allocator.sm.current_time = 1.0
    two_observers._update_passive_sensors(1.0)
    positions = two_observers.allocator.sm.get_passive_positions(1.0)
    assert len(two_observers.allocator.sm.get_passive_observations(1.0)) == 2
    assert len(positions) == 1
    position = positions[0]
    assert len(position.source_observation_ids) == 2

    tasks = two_observers.allocator.task_catalog.build(
        two_observers.allocator.sm,
        two_observers.allocator.sm.contacts.list_snapshots(),
        (),
        now_min=1.0,
    )
    investigation = next(
        task for task in tasks
        if task.task_id == "investigation:EMITTER-INTEGRATION"
    )
    assert investigation.priority == "high"
    assert investigation.bbox is not None


def test_information_version_flows_from_evidence_to_selection_and_commit():
    engine = build_scenario("information-loop", seed=42, transport="fixture")
    sm = engine.allocator.sm
    sm.current_time = 1.0
    position = PassivePosition(
        position_id="POS-INTEGRATION-1",
        emitter_track_id="EMITTER-INTEGRATION-1",
        burst_id="BURST-INTEGRATION-1",
        sample_id="SAMPLE-INTEGRATION-1",
        observed_at_min=1.0,
        position_cells=(15.0, 15.0),
        source_observation_ids=("OBS-INTEGRATION-1", "OBS-INTEGRATION-2"),
    )

    sm.register_passive_position(position)
    delta = sm.apply_information_facts((position,), 1.0)
    engine._publish_information_delta(delta, 1.0)
    assert delta is not None
    version = sm.information_version
    assert sm.apply_information_facts((position,), 1.0) is None
    assert sm.information_version == version

    statuses = engine._evaluate_intent_statuses(1.0)
    snapshot = engine.allocator.build_mission_snapshot(
        1.0,
        intents=engine.intents.intents(),
        intent_statuses=statuses,
    )
    task = next(
        item for item in snapshot.candidates
        if item.task_id == "investigation:EMITTER-INTEGRATION-1"
    )
    assert task.information_version == version
    assert snapshot.information_version == version

    scheduler = engine.allocator.mission_scheduler
    batch = scheduler.decide(snapshot)

    assert batch is not None
    assert batch.information_version == version
    assert task.task_id in {item.task_id for item in batch.assignments}
    assert engine.apply_assignment_batch(batch)
    assert any(
        event["type"] == "mission_assignment_committed"
        and task.task_id in event["data"]["task_ids"]
        for event in sm.get_recent_events(0.0)
    )

    sm.current_time = 17.0
    expired = sm.information_policy.advance_time(17.0)
    engine._publish_information_delta(expired, 17.0)
    assert expired is not None
    assert "evidence_expired" in expired.reason_codes
    assert sm.information_version > version
    assert sm.get_passive_positions(17.0) == ()


def test_information_prompt_exposes_bounded_fairness_metadata():
    engine = build_scenario("information-loop", seed=42, transport="fixture")
    snapshot = engine.allocator.build_mission_snapshot(
        1.0,
        intents=engine.intents.intents(),
        intent_statuses=engine._evaluate_intent_statuses(1.0),
    )
    payload = engine.allocator.mission_scheduler._prompt_payload(snapshot)

    assert payload["snapshot"]["candidates_truncated"] is True
    assert payload["snapshot"]["prompt_fairness_bound_cycles"] is not None
    prompt_ids = {
        item["task_id"] for item in payload["snapshot"]["candidates"]
    }
    assert prompt_ids <= {
        edge.task_id for edge in snapshot.feasible_edges
    }

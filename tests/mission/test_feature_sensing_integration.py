from scripts.replay_restoration_scenarios import build_scenario
from src.mission.contracts import VesselCommand


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

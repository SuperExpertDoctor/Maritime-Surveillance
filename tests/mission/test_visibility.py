from dataclasses import fields, replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.env.simulation import SimulationEngine
from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord, TargetReport
from src.schedule.info_value_table import InfoValueTable
from src.schedule.prompt_builder import PromptBuilder
from tests.mission.test_contact_store import visual


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "t06-offline-fixture")
    # Initialization only; these tests never invoke a model or engine.step().
    return SimulationEngine(ConfigLoader.load(), seed=41)


def test_global_ais_is_ingested_at_zero_even_with_far_uavs(engine):
    sm = engine.allocator.sm
    assert hasattr(sm, "contacts"), "StateManager must own ContactStore"
    expected = {s.ais_signal.mmsi for s in engine.ships if s.ais_signal is not None}
    assert {c.ais_mmsi for c in sm.contacts.list_snapshots()} == expected
    assert expected
    assert all(c.last_seen_min == 0 for c in sm.contacts.list_snapshots())
    for uav in engine.uavs:
        uav._col = uav._row = -1000
    engine._refresh_ais_signals(1)
    assert all(c.last_seen_min == 1 for c in sm.contacts.list_snapshots())


def test_silent_ships_without_sar_or_eo_never_create_contacts(engine):
    sm = engine.allocator.sm
    assert hasattr(sm, "contacts"), "StateManager must own ContactStore"
    before = sm.contacts.list_snapshots()
    engine.ships.append(SimpleNamespace(id="hidden-extra", ais_mode="silent", departed=False,
                                        set_ais_signal=lambda signal: None))
    engine._refresh_ais_signals(0)  # cadence already ingested
    engine._update_sensors_and_detections(0)
    assert sm.contacts.list_snapshots() == before


def test_detection_creates_observations_without_installing_a_task(engine):
    sm = engine.allocator.sm
    before = len(sm.get_track_regions())
    cid = engine._handle_detection(engine.uavs[0], engine.ships[0], 1)
    assert isinstance(cid, str), "sensor adapter must return the associated contact ID"
    c = sm.contacts.snapshot(cid)
    assert c.samples[-1].source == "sar"
    assert c.samples[-1].observer_position_cells == engine.uavs[0].float_position
    assert sm.get_track_regions() == [] and before == 0
    assert engine.uavs[0].target_group_id is None
    assert c.assigned_uav_id is None
    assert all("ship_id" not in e.get("data", {}) for e in sm.get_recent_events(0))


def test_track_prediction_is_finite_and_does_not_fabricate_samples(engine):
    sm = engine.allocator.sm
    assert hasattr(sm, "contacts"), "StateManager must own ContactStore"
    cid = sm.contacts.ingest_visual(visual(position=(12, 12), velocity_cells_min=(.2, 0)))
    original = sm.contacts.snapshot(cid)
    track = sm.create_track_region(cid, GridCoord(12, 12))
    engine.ships = []
    sm.current_time = 3
    engine._sync_state_from_entities()
    assert sm.contact_position(cid, 3) == pytest.approx((12.6, 12))
    assert track.bbox.col_start == 11  # rounded observation prediction, +/-2
    assert sm.contacts.snapshot(cid) == original
    sm.current_time = 5.01
    engine._sync_state_from_entities()
    assert sm.contact_position(cid, 5.01) is None
    assert sm.contacts.snapshot(cid).state == "lost"
    assert sm.contacts.snapshot(cid).last_seen_min == 0
    assert sm.contacts.snapshot(cid).revision == 1
    assert sm.get_track_regions() == []


def test_target_report_contract_uses_contact_id():
    assert "contact_id" in {f.name for f in fields(TargetReport)}
    assert "group_id" not in {f.name for f in fields(TargetReport)}


def test_hidden_identity_count_gate_and_trail_cannot_change_blue_payload(engine):
    sm = engine.allocator.sm
    assert hasattr(sm, "contacts"), "StateManager must own ContactStore"
    builder = PromptBuilder()
    assert hasattr(builder, "contact_payload"), "blue contacts need an explicit allowlist"
    def blue():
        contacts = builder.contact_payload(sm)
        prompts = builder.build(sm, InfoValueTable(sm), CandidateResult())
        return sm.contacts.list_snapshots(), hashlib.sha256(
            json.dumps((contacts, prompts), sort_keys=True).encode()).hexdigest()
    before = blue()
    for ship in engine.ships:
        ship.truth = replace(ship.truth, identity="civilian" if ship.truth_identity == "target" else "target")
        ship.gate_state = "evasiveness-secret"
        ship._col, ship._row = 29.123456, 29.654321
        ship.trail.append((29.123456, 29.654321))
    engine.ships.extend([SimpleNamespace(truth_identity="target", is_evading=True)] * 3)
    engine._sync_state_from_entities()
    assert blue() == before
    encoded = json.dumps(builder.contact_payload(sm))
    for forbidden in ("ship_id", "actual_military", "truth_identity", "gate_state", "is_evading", "trail"):
        assert forbidden not in encoded


def test_truth_departure_does_not_release_blue_contact(engine, monkeypatch):
    sm = engine.allocator.sm
    assert hasattr(sm, "contacts"), "StateManager must own ContactStore"
    before = sm.contacts.list_snapshots()
    for ship in engine.ships:
        monkeypatch.setattr(ship, "step", lambda *args: None)
        ship.departed = True
    engine._update_ships(1)
    assert sm.contacts.list_snapshots() == before
    assert not any(e["type"] == "target_departed" for e in sm.get_recent_events(0))


def test_contact_created_notifies_scheduler_without_control_event(engine):
    trigger = engine.allocator.trigger_manager.check(0)
    assert trigger.trigger_type == "heavy"
    assert engine.allocator.sm.get_track_regions() == []


def test_visual_history_never_copies_ship_trail_or_truth_velocity(engine):
    ship = engine.ships[0]
    ship.trail[:] = [(12345.0, 67890.0)] * 100
    cid = engine._handle_detection(engine.uavs[0], ship, 1)
    c = engine.allocator.sm.contacts.snapshot(cid)
    assert len(c.samples) == 1
    assert c.samples[0].velocity_cells_min is None
    assert c.estimated_velocity_cells_min is None
    assert "12345" not in repr(c)

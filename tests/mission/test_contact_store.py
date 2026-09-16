from dataclasses import fields, replace
import importlib
import math

import pytest

from src.env.ais_signal import AISSignal
from src.mission import contracts
from src.schedule.config_loader import ConfigLoader


def ais(t=0.0, position=(10.0, 10.0), mmsi="123456789", speed=0.0):
    return AISSignal(mmsi, position, speed, 0.0, "MV-001", "Cargo", t)


def visual(sample_id="EO-1", t=0.0, position=(10.0, 10.0), source="eo", **kwargs):
    assert hasattr(contracts, "VisualDetection"), "VisualDetection contract is missing"
    values = dict(sample_id=sample_id, observed_at_min=t, source=source,
                  source_id="UAV-1", position_cells=position, velocity_cells_min=None,
                  position_uncertainty_cells=0.05, observer_position_cells=(8.0, 10.0),
                  measured_range_cells=math.dist((8.0, 10.0), position),
                  navigation_context="open_water")
    values.update(kwargs)
    return contracts.VisualDetection(**values)


@pytest.fixture
def store():
    spec = importlib.util.find_spec("src.mission.contact_store")
    assert spec is not None, "ContactStore is missing"
    return importlib.import_module("src.mission.contact_store").ContactStore(
        ConfigLoader.load().mission.contact, cell_size_km=2.0)


def test_visual_contract_has_required_context_and_no_contact_id():
    detection = visual()
    assert tuple(f.name for f in fields(detection)) == tuple(
        f.name for f in fields(contracts.ObservationSample) if f.name != "contact_id")
    assert detection.__dataclass_params__.frozen
    with pytest.raises((TypeError, ValueError)):
        visual(observer_position_cells=None)
    with pytest.raises((TypeError, ValueError)):
        visual(measured_range_cells=None)


def test_ais_mmsi_and_packet_are_idempotent(store):
    cid = store.ingest_ais(ais(speed=18), 0.0)
    first = store.snapshot(cid)
    assert cid == "C0001"
    assert first.identity == "unknown" and first.state == "pending"
    assert first.estimated_velocity_cells_min == pytest.approx((18 * 1.852 / 120, 0))
    assert store.ingest_ais(ais(speed=18), 1.0) == cid
    assert store.snapshot(cid) == first
    assert store.ingest_ais(ais(1.0), 1.0) == cid
    assert store.snapshot(cid).revision == 2
    assert len(store.list_snapshots()) == 1


def test_visual_associates_before_constructing_sample(store, monkeypatch):
    module = importlib.import_module("src.mission.contact_store")
    original = module.ObservationSample
    def checked(*args, **kwargs):
        assert kwargs["contact_id"]
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "ObservationSample", checked)
    cid = store.ingest_visual(visual())
    assert store.snapshot(cid).samples[0].contact_id == cid


def test_two_confirmations_merge_alias_histories_and_duplicate_reservations(store):
    aid = store.ingest_ais(ais(), 0)
    vid = store.ingest_visual(visual())
    assert vid != aid and len(store.list_snapshots()) == 2
    store.reserve(aid, "UAV-2", "P0001")
    store.reserve(vid, "UAV-1", "P0002")
    original_ids = {s.sample_id for c in store.list_snapshots() for s in c.samples}
    assert store.ingest_visual(visual("EO-2", 1)) == aid
    merged = store.snapshot(vid)
    assert merged == store.snapshot(aid)
    assert len(store.list_snapshots()) == 1
    assert original_ids <= {s.sample_id for s in merged.samples}
    assert {s.source for s in merged.samples} == {"ais", "eo"}
    assert all(s.contact_id == aid for s in merged.samples)
    assert merged.assigned_uav_id == "UAV-2" and merged.active_probe_id == "P0001"
    assert store.aliases[vid] == aid
    assert any(e["type"] == "contact_merged" for e in store.events)
    assert any(e["type"] == "duplicate_task_cancelled" and e["probe_id"] == "P0002"
               for e in store.events)
    assert store.ingest_visual(visual()) == aid


def test_alternating_sar_contacts_preserve_independent_confirmation_streaks(store):
    left = store.ingest_ais(ais(position=(10, 10)), 0)
    right = store.ingest_ais(ais(position=(13, 10), mmsi="987654321"), 0)
    left_fix = visual("left-1", 0, (10, 10), source="sar")
    right_fix = visual("right-1", .1, (13, 10), source="sar")
    left_visual = store.ingest_visual(left_fix)
    right_visual = store.ingest_visual(right_fix)
    assert not store.aliases
    before = store.list_snapshots(), store.events
    store.ingest_visual(left_fix)
    store.ingest_visual(right_fix)
    assert (store.list_snapshots(), store.events) == before

    assert store.ingest_visual(visual("left-2", 1, (10, 10), source="sar")) == left
    assert store.ingest_visual(visual("right-2", 1.1, (13, 10), source="sar")) == right
    assert store.aliases == {left_visual: left, right_visual: right}
    assert len(store.list_snapshots()) == 2
    for cid, sample_ids in ((left, {"left-1", "left-2"}), (right, {"right-1", "right-2"})):
        samples = store.snapshot(cid).samples
        assert {s.sample_id for s in samples if s.source == "sar"} == sample_ids
        assert {s.source for s in samples} == {"ais", "sar"}


def test_visual_first_then_two_ais_confirmations_merge(store):
    vid = store.ingest_visual(visual())
    aid = store.ingest_ais(ais(), 0)
    assert aid != vid
    assert store.ingest_ais(ais(1), 1) == aid
    assert store.snapshot(vid).contact_id == aid


@pytest.mark.parametrize("other_candidate", [False, True])
def test_outside_gate_ais_breaks_confirmation_continuity(store, other_candidate):
    vid = store.ingest_visual(visual())
    if other_candidate:
        store.ingest_visual(visual("other", position=(13, 10)))
    aid = store.ingest_ais(ais(), 0)
    interrupted = ais(1, (13, 10))
    store.ingest_ais(interrupted, 1)
    store.ingest_ais(ais(2), 2)
    assert store.resolve(vid) == vid
    assert not store.aliases
    # Replaying the contradictory packet is idempotent, including continuity.
    before = store.list_snapshots(), store.events
    store.ingest_ais(interrupted, 2)
    assert (store.list_snapshots(), store.events) == before
    store.ingest_ais(ais(3), 3)
    assert store.resolve(vid) == aid


@pytest.mark.parametrize("source", ["eo", "sar"])
def test_matched_visual_contradiction_resets_only_its_pair(store, source):
    aid = store.ingest_ais(ais(), 0)
    other_aid = store.ingest_ais(ais(position=(13, 10), mmsi="987654321"), 0)
    vid = store.ingest_visual(visual("first", 0, (10.4, 10), source=source,
                                     velocity_cells_min=(0, 0)))
    other_vid = store.ingest_visual(visual("other", .1, (13, 10), source=source))
    # This still matches the visual contact, but is outside its AIS gate.
    interrupted = visual("outside-ais", 1, (10.8, 10), source=source,
                         velocity_cells_min=(0, 0))
    assert store.ingest_visual(interrupted) == vid
    assert store.ingest_visual(visual("other-confirm", 1.1, (13, 10), source=source)) == other_aid
    assert store.resolve(other_vid) == other_aid
    assert store.ingest_visual(visual("back", 2, (10.4, 10), source=source,
                                      velocity_cells_min=(0, 0))) == vid
    before = store.list_snapshots(), store.events
    store.ingest_visual(interrupted)
    assert (store.list_snapshots(), store.events) == before
    assert store.ingest_visual(visual("confirm", 3, (10.4, 10), source=source)) == aid


@pytest.mark.parametrize("interruption", ["new", "matched", "ambiguous"])
def test_unrelated_visual_report_preserves_confirmation(store, interruption):
    if interruption in ("matched", "ambiguous"):
        store.ingest_visual(visual("other-left", position=(13, 10)))
    if interruption == "ambiguous":
        store.ingest_visual(visual("other-right", position=(13.7, 10)))
    aid = store.ingest_ais(ais(), 0)
    vid = store.ingest_visual(visual())
    x = 13.35 if interruption == "ambiguous" else 13
    store.ingest_visual(visual("unrelated", 1, (x, 10)))
    assert store.resolve(vid) == vid
    assert store.ingest_visual(visual("confirm", 2)) == aid


@pytest.mark.parametrize("matching_x", [10.0, 10.7])
def test_ambiguous_ais_resets_confirmations_for_every_visual_candidate(store, matching_x):
    left = store.ingest_visual(visual(position=(10, 10)))
    right = store.ingest_visual(visual("EO-right", position=(10.7, 10)))
    aid = store.ingest_ais(ais(position=(matching_x, 10)), 0)
    store.ingest_ais(ais(1, (10.35, 10)), 1)
    store.ingest_ais(ais(2, (matching_x, 10)), 2)
    assert not store.aliases
    assert store.snapshot(left).ais_mmsi is None
    assert store.snapshot(right).ais_mmsi is None
    store.ingest_ais(ais(3, (matching_x, 10)), 3)
    assert store.resolve(left if matching_x == 10 else right) == aid


@pytest.mark.parametrize("matching_x", [10.0, 10.7])
def test_ambiguous_visual_gate_resets_existing_candidate_confirmations(store, matching_x):
    left = store.ingest_visual(visual(position=(10, 10)))
    right = store.ingest_visual(visual("EO-right", position=(10.7, 10)))
    store.ingest_ais(ais(position=(matching_x, 10)), 0)
    store.ingest_visual(visual("EO-ambiguous", 1, (10.35, 10)))
    store.ingest_visual(visual("EO-match", 2, (matching_x, 10)))
    assert not store.aliases
    assert store.snapshot(left).ais_mmsi is None
    assert store.snapshot(right).ais_mmsi is None


def test_reverse_ais_association_predicts_moving_visual_to_packet_time(store):
    vid = store.ingest_visual(visual(velocity_cells_min=(.3, 0)))
    speed_kn = .3 * 120 / 1.852
    aid = store.ingest_ais(ais(1, (10.3, 10), speed=speed_kn), 1)
    assert store.resolve(vid) == vid
    store.ingest_ais(ais(2, (10.6, 10), speed=speed_kn), 2)
    assert store.resolve(vid) == aid
    assert {s.source for s in store.snapshot(aid).samples} == {"ais", "eo"}


def test_ambiguity_and_contradiction_do_not_merge_crossing_contacts(store):
    a = store.ingest_ais(ais(position=(9.8, 10)), 0)
    b = store.ingest_ais(ais(position=(10.2, 10), mmsi="987654321"), 0)
    v = store.ingest_visual(visual())
    assert v not in (a, b)
    assert any(e["type"] == "association_ambiguous" for e in store.events)
    store.ingest_visual(visual("EO-2", 1, (9.8, 10)))  # first confirmation A
    store.ingest_visual(visual("EO-3", 2, (10.2, 10)))  # contradicts A
    assert len(store.list_snapshots()) >= 3
    assert not store.aliases


def test_association_predicts_to_measurement_time_and_rejects_outside_gate(store):
    cid = store.ingest_visual(visual(velocity_cells_min=(0.3, 0)))
    assert store.ingest_visual(visual("EO-2", 2, (10.6, 10))) == cid
    assert store.ingest_visual(visual("EO-3", 3, (13, 10))) != cid


def test_source_velocity_sorting_dedup_and_long_gap(store):
    cid = store.ingest_visual(visual(t=0))
    store.ingest_visual(visual("EO-3", 2, (10.2, 10)))
    store.ingest_visual(visual("EO-2", 1, (10.1, 10)))
    snapshot = store.snapshot(cid)
    assert [s.observed_at_min for s in snapshot.samples] == [0, 1, 2]
    assert snapshot.estimated_velocity_cells_min == pytest.approx((0.1, 0))
    store.ingest_visual(visual("EO-2", 1, (10.1, 10)))
    assert store.snapshot(cid) == snapshot
    store.ingest_visual(visual("SAR-1", 2.1, (10.3, 10), source="sar"))
    assert store.snapshot(cid).estimated_velocity_cells_min is None
    store.ingest_visual(visual("SAR-2", 5, (10.3, 10), source="sar"))
    assert store.snapshot(cid).estimated_velocity_cells_min is None


def test_history_window_archives_old_input_without_revision_or_last_seen_update(store):
    cid = store.ingest_ais(ais(150), 150)
    before = store.snapshot(cid)
    store.ingest_ais(ais(1), 151)
    assert store.snapshot(cid) == before
    assert any(s.observed_at_min == 1 for s in store.archived_samples)
    assert store.ingest_ais(ais(1), 152) == cid
    assert len(store.archived_samples) == 1


def test_history_sample_limit_and_expiry_preserve_last_seen(store):
    store.config = replace(store.config, history_max_samples=2)
    cid = store.ingest_ais(ais(), 0)
    store.ingest_ais(ais(1), 1)
    store.ingest_ais(ais(2), 2)
    assert len(store.snapshot(cid).samples) == 2
    assert len(store.archived_samples) == 1
    store.reserve(cid, "UAV-1", "P0001")
    assert store.expire(7) == ()
    assert store.expire(7.01) == (cid,)
    lost = store.snapshot(cid)
    assert lost.state == "lost" and lost.last_seen_min == 2
    assert lost.revision == 3 and len(lost.samples) == 2
    assert lost.assigned_uav_id is None
    assert store.expire(8) == ()
    store.ingest_ais(ais(9), 9)
    assert store.snapshot(cid).state == "pending"


def test_release_event_preserves_reservation_owner_before_clearing(store):
    cid = store.ingest_visual(visual())
    store.reserve(cid, "UAV-1", "P0001")

    store.release(cid, 1.0, "probe_timeout")

    event = store.events[-1]
    assert event == {
        "type": "contact_released",
        "contact_id": cid,
        "uav_id": "UAV-1",
        "probe_id": "P0001",
        "reason": "probe_timeout",
    }
    assert store.snapshot(cid).assigned_uav_id is None
    assert store.snapshot(cid).active_probe_id is None


def test_lost_event_preserves_reservation_owner_before_clearing(store):
    cid = store.ingest_visual(visual())
    store.reserve(cid, "UAV-1", "P0001")

    assert store.expire(store.config.stale_after_min + 0.01) == (cid,)

    event = next(event for event in store.events if event["type"] == "contact_lost")
    assert event["uav_id"] == "UAV-1"
    assert event["probe_id"] == "P0001"
    assert store.snapshot(cid).state == "lost"
    assert store.snapshot(cid).assigned_uav_id is None
    assert store.snapshot(cid).active_probe_id is None


def test_assessment_reservation_and_type_i_ais_cooldown(store):
    cid = store.ingest_visual(visual())
    store.reserve(cid, "UAV-1", "P0001")
    store.reserve(cid, "UAV-1", "P0001")
    with pytest.raises(ValueError):
        store.reserve(cid, "UAV-2", "P0002")
    assessment = contracts.Assessment("A1", cid, "P0001", 1, 1, "type_i", .9,
                                      ("EO-1",), ("validated evidence",), (), "call1")
    with pytest.raises(ValueError):
        store.apply_assessment(replace(assessment, history_revision=0))
    store.apply_assessment(assessment)
    cleared = store.snapshot(cid)
    assert cleared.vessel_class == "type_i" and cleared.state == "cleared"
    assert cleared.assigned_uav_id is None and cleared.active_probe_id is None
    assert cleared.next_probe_not_before_min == 61
    store.release(cid, 2, "type_i_released")
    assert store.snapshot(cid).cleared_at_min == 1
    with pytest.raises(ValueError):
        store.reserve(cid, "UAV-1", "P0002")


def test_assessment_emits_canonical_type_events(store):
    type_i_id = store.ingest_visual(visual("TYPE-I"))
    store.reserve(type_i_id, "UAV-1", "P-TYPE-I")
    type_i = contracts.Assessment(
        "A-TYPE-I", type_i_id, "P-TYPE-I", 1, 1, "type_i", .9,
        ("TYPE-I",), ("validated",), (), "call-type-i",
    )
    store.apply_assessment(type_i)
    assert [event["type"] for event in store.events if event["type"].startswith("type_")] == [
        "type_i_assessed", "type_i_released",
    ]

    type_ii_id = store.ingest_visual(visual("TYPE-II", position=(20.0, 20.0)))
    store.reserve(type_ii_id, "UAV-2", "P-TYPE-II")
    type_ii = contracts.Assessment(
        "A-TYPE-II", type_ii_id, "P-TYPE-II", 1, 2, "type_ii", .9,
        ("TYPE-II",), ("validated",), (), "call-type-ii",
    )
    store.apply_assessment(type_ii)
    assert [event["type"] for event in store.events if event["type"].startswith("type_")] == [
        "type_i_assessed", "type_i_released", "type_ii_assessed", "type_ii_confirmed",
    ]


def test_ordinary_ais_preserves_cleared_identity_after_merge(store):
    aid = store.ingest_ais(ais(), 0)
    store.ingest_visual(visual())
    store.ingest_visual(visual("EO-2", 1))
    store.reserve(aid, "UAV-1", "P1")
    c = store.snapshot(aid)
    store.apply_assessment(contracts.Assessment("A1", aid, "P1", c.revision, 1,
        "type_i", .9, ("EO-1", "EO-2"), ("validated",), (), "call1"))
    store.ingest_ais(ais(2), 2)
    assert store.snapshot(aid).state == "cleared"
    assert store.snapshot(aid).vessel_class == "type_i"


def test_invalid_numeric_observations_do_not_mutate_store(store):
    for signal in (ais(float("nan")), ais(position=(float("inf"), 0))):
        with pytest.raises(ValueError):
            store.ingest_ais(signal, 0)
    assert store.list_snapshots() == ()


def test_late_packet_does_not_resurrect_a_lost_contact(store):
    cid = store.ingest_ais(ais(1), 1)
    store.expire(10)
    store.ingest_ais(ais(2), 10)
    assert store.snapshot(cid).state == "lost"


def test_expire_archives_out_of_window_samples_without_changing_revision(store):
    cid = store.ingest_ais(ais(), 0)
    store.expire(121)
    c = store.snapshot(cid)
    assert c.samples == () and c.revision == 1 and c.last_seen_min == 0
    assert len(store.archived_samples) == 1


def test_ambiguous_visual_interrupts_existing_merge_confirmation(store):
    store.ingest_ais(ais(position=(9.8, 10)), 0)
    store.ingest_ais(ais(position=(10.2, 10), mmsi="987654321"), 0)
    vid = store.ingest_visual(visual(position=(9.8, 10)))
    store.ingest_visual(visual("EO-2", 1, (10, 10)))
    store.ingest_visual(visual("EO-3", 2, (9.8, 10)))
    assert not store.aliases
    assert store.snapshot(vid).ais_mmsi is None


def test_first_ancient_packet_is_archive_only_until_fresh_data_arrives(store):
    cid = store.ingest_ais(ais(0), 200)
    assert store.list_snapshots() == ()
    assert not store.events
    assert len(store.archived_samples) == 1
    assert store.ingest_ais(ais(201), 201) == cid
    c = store.snapshot(cid)
    assert c.first_seen_min == c.last_seen_min == 201
    assert c.revision == 1 and len(store.list_snapshots()) == 1
    assert [e["type"] for e in store.events] == ["contact_created"]

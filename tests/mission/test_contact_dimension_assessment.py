"""Radiation/evasion evidence -> contact dimension assessment wiring tests."""
import pytest

from src.mission.contact_assessor import ContactAssessor
from src.mission.contact_store import ContactStore
from src.mission.contracts import (
    ContactAssessment,
    CovarianceKernel,
    EvidenceRecord,
    PassivePosition,
    PointKernel,
    VisualDetection,
)
from src.mission.task_catalog import TaskCatalog
from src.schedule.config_loader import ConfigLoader


def _store():
    config = ConfigLoader.load()
    return ContactStore(
        config.mission.contact, cell_size_km=config.grid.cell_size_km
    )


def _position(position_id, burst_id, observed_at, cells=(10.0, 10.0)):
    return PassivePosition(
        position_id,
        "EMITTER-X",
        burst_id,
        f"SAMPLE-{burst_id}",
        observed_at,
        cells,
        (f"OBS-{burst_id}-A", f"OBS-{burst_id}-B"),
    )


def _evasion_record(contact_id, evidence_id="EVASION-1"):
    return EvidenceRecord(
        evidence_id=evidence_id,
        kind="evasive_maneuver",
        source_id="MMSI-1",
        contact_id=contact_id,
        observed_at_min=5.0,
        expires_at_min=65.0,
        strength=1.0,
        spatial=CovarianceKernel(
            mean_cells=(10.0, 10.0),
            covariance_cells2=((0.4, 0.0), (0.0, 0.4)),
        ),
    )


def test_radiation_bursts_register_dimension_evidence():
    store = _store()
    contact_id = store.ingest_passive_position(_position("POS-1", "B-1", 1.0))
    assert store.dimension_evidence(contact_id) == ()

    store.ingest_passive_position(_position("POS-2", "B-2", 2.0))
    evidence = store.dimension_evidence(contact_id)
    assert len(evidence) == 1
    assert evidence[0].kind == "type_ii_assessment"
    assert evidence[0].contact_id == contact_id

    drained = store.drain_radiation_activity_evidence()
    assert drained == evidence
    # The dimension pool survives draining — assessments are cumulative.
    assert store.dimension_evidence(contact_id) == evidence


def test_suspected_violation_commits_below_terminal_confidence_gate():
    store = _store()
    contact_id = store.ingest_passive_position(_position("POS-1", "B-1", 1.0))
    store.ingest_passive_position(_position("POS-2", "B-2", 2.0))

    assessment = ContactAssessor.assess_dimensions(
        store.dimension_evidence(contact_id)
    )
    assert assessment.activity == "suspected_violation"
    assert assessment.vessel_class == "unknown"

    updated = store.apply_dimension_assessment(
        contact_id,
        assessment,
        assessed_at_min=2.0,
        expected_revision=store.snapshot(contact_id).revision,
    )
    assert updated.activity == "suspected_violation"
    assert updated.activity_evidence_ids
    # Suspicion must not suspend probe eligibility: the contact stays pending
    # so the LLM probe path still owns terminal identification.
    assert updated.state == "pending"
    assert updated.vessel_class == "unknown"


def test_suspected_contact_is_track_candidate_but_probe_wins():
    store = _store()
    contact_id = store.ingest_passive_position(_position("POS-1", "B-1", 1.0))
    store.ingest_passive_position(_position("POS-2", "B-2", 2.0))
    assessment = ContactAssessor.assess_dimensions(
        store.dimension_evidence(contact_id)
    )
    store.apply_dimension_assessment(
        contact_id, assessment, assessed_at_min=2.0,
    )
    contact = store.snapshot(contact_id)

    assert TaskCatalog._is_track_candidate(contact)
    assert TaskCatalog._is_probe_candidate(contact, now=2.0)

    # Probe cooldown blocks the probe path; the track path must still open.
    from dataclasses import replace

    cooled = replace(contact, next_probe_not_before_min=30.0)
    assert not TaskCatalog._is_probe_candidate(cooled, now=2.0)
    assert TaskCatalog._is_track_candidate(cooled)


def test_evasion_plus_radiation_confirms_violation():
    store = _store()
    contact_id = store.ingest_passive_position(_position("POS-1", "B-1", 1.0))
    store.ingest_passive_position(_position("POS-2", "B-2", 2.0))
    assert store.register_dimension_evidence(contact_id, _evasion_record(contact_id))

    assessment = ContactAssessor.assess_dimensions(
        store.dimension_evidence(contact_id)
    )
    assert assessment.activity == "confirmed_violation"
    assert assessment.activity_confidence >= store.config.assessment_confidence_min

    updated = store.apply_dimension_assessment(
        contact_id,
        assessment,
        assessed_at_min=5.0,
        expected_revision=store.snapshot(contact_id).revision,
    )
    assert updated.activity == "confirmed_violation"
    assert updated.state == "tracking"


def test_unknown_class_assessment_preserves_established_class():
    store = _store()
    contact_id = store.ingest_passive_position(_position("POS-1", "B-1", 1.0))
    store.ingest_passive_position(_position("POS-2", "B-2", 2.0))
    typed = ContactAssessment(
        vessel_class="type_ii",
        class_confidence=0.9,
        class_evidence_ids=("EO-CLASS-1",),
        activity="unknown",
        activity_confidence=0.0,
        activity_evidence_ids=(),
    )
    store.apply_dimension_assessment(contact_id, typed, assessed_at_min=2.0)
    assert store.snapshot(contact_id).vessel_class == "type_ii"

    radiation_only = ContactAssessor.assess_dimensions(
        store.dimension_evidence(contact_id)
    )
    updated = store.apply_dimension_assessment(
        contact_id, radiation_only, assessed_at_min=3.0,
    )
    # An unknown-class dimension result never erases the established class.
    assert updated.vessel_class == "type_ii"
    assert updated.class_confidence == 0.9
    assert updated.class_evidence_ids == ("EO-CLASS-1",)
    assert updated.activity == "suspected_violation"


def test_confirmed_violation_below_confidence_gate_is_rejected():
    store = _store()
    contact_id = store.ingest_passive_position(_position("POS-1", "B-1", 1.0))
    weak = ContactAssessment(
        vessel_class="unknown",
        class_confidence=0.0,
        class_evidence_ids=(),
        activity="confirmed_violation",
        activity_confidence=0.5,
        activity_evidence_ids=("RAD-1",),
    )
    with pytest.raises(ValueError):
        store.apply_dimension_assessment(contact_id, weak, assessed_at_min=2.0)


def test_probe_cooldown_gates_probe_but_not_track_reservation():
    from dataclasses import replace

    store = _store()
    contact_id = store.ingest_passive_position(_position("POS-1", "B-1", 1.0))
    store._now = 10.0
    contact = store.snapshot(contact_id)
    store._contacts[contact_id] = replace(
        contact, next_probe_not_before_min=20.0
    )
    # Track reservation (probe_id=None) ignores the probe retry cooldown.
    store.reserve(contact_id, "UAV-1", None)
    assert store.snapshot(contact_id).state == "tracking"

    store.release(contact_id, 10.0, "track_done")
    with pytest.raises(ValueError):
        store.reserve(contact_id, "UAV-2", "P0001")


def test_register_dimension_evidence_ignores_unknown_contacts():
    store = _store()
    assert not store.register_dimension_evidence(
        "MMSI-UNRESOLVED", _evasion_record("MMSI-UNRESOLVED")
    )

from src.mission.contracts import EvidenceRecord, PointKernel
from src.mission.evidence_store import AisUpdateRegistry, EvidenceStore


def _record(evidence_id, *, source="SRC-1", strength=1.0, at=0.0):
    return EvidenceRecord(
        evidence_id=evidence_id,
        kind="passive_position",
        source_id=source,
        contact_id="CONTACT-1",
        observed_at_min=at,
        expires_at_min=at + 15.0,
        strength=strength,
        spatial=PointKernel((5.0, 5.0), 1.0),
    )


def test_evidence_store_is_idempotent_and_supersedes_same_subject():
    store = EvidenceStore()
    first = _record("E1")
    second = _record("E2", at=1.0)

    assert store.ingest(first) is True
    assert store.ingest(first) is False
    assert store.supersede(second, subject_key=("CONTACT-1", "passive_position")) is True
    assert store.active_records(1.0) == (second,)


def test_ais_registry_disable_is_keyed_by_mmsi_and_revision():
    registry = AisUpdateRegistry()
    assert registry.is_enabled("123") is True
    state = registry.disable("123", now_min=2.0, reason="confirmed_type_i")
    assert state.enabled is False
    assert state.revision == 1
    assert registry.is_enabled("123") is False
    assert registry.disable("123", now_min=3.0, reason="confirmed_type_i") == state

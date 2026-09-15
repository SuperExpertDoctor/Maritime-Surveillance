"""Small immutable-record stores used by the information update transaction."""

from __future__ import annotations


from src.mission.contracts import AisUpdateState, EvidenceRecord


class EvidenceStore:
    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        self._superseded: set[str] = set()
        self._active_keys: dict[tuple, str] = {}

    @staticmethod
    def subject_key(record: EvidenceRecord) -> tuple:
        return (record.contact_id or record.source_id, record.kind)

    def ingest(self, record: EvidenceRecord) -> bool:
        if not isinstance(record, EvidenceRecord):
            raise TypeError("EvidenceStore accepts EvidenceRecord")
        existing = self._records.get(record.evidence_id)
        if existing is not None:
            if existing != record:
                raise ValueError(f"evidence_id already contains a different record: {record.evidence_id}")
            return False
        self._records[record.evidence_id] = record
        self._active_keys[self.subject_key(record)] = record.evidence_id
        return True

    def supersede(self, record: EvidenceRecord, subject_key: tuple | None = None) -> bool:
        key = subject_key or self.subject_key(record)
        previous_id = self._active_keys.get(key)
        changed = self.ingest(record)
        if previous_id == record.evidence_id:
            return changed
        if previous_id is not None:
            self._superseded.add(previous_id)
        self._active_keys[key] = record.evidence_id
        return True

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._records.get(evidence_id)

    def all_records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def active_records(self, now_min: float) -> tuple[EvidenceRecord, ...]:
        return tuple(
            record for key, record in sorted(self._records.items())
            if key not in self._superseded
            and record.observed_at_min <= now_min
            and (now_min == float("inf") or now_min < record.expires_at_min)
        )

    def expire(self, now_min: float) -> tuple[str, ...]:
        return tuple(
            record.evidence_id
            for key, record in sorted(self._records.items())
            if key not in self._superseded
            if record.expires_at_min <= now_min
        )

    def is_superseded(self, evidence_id: str) -> bool:
        return evidence_id in self._superseded

    def snapshot(self) -> tuple[EvidenceRecord, ...]:
        return self.all_records()


class AisUpdateRegistry:
    """MMSI-keyed qualification state, independent of contact aliases."""

    def __init__(self) -> None:
        self._states: dict[str, AisUpdateState] = {}

    def state(self, mmsi: str) -> AisUpdateState:
        if not mmsi:
            raise ValueError("mmsi is required")
        return self._states.setdefault(
            mmsi,
            AisUpdateState(mmsi, True, 0, 0.0, "unclassified"),
        )

    def is_enabled(self, mmsi: str) -> bool:
        return self.state(mmsi).enabled

    def disable(
        self,
        mmsi: str,
        *,
        now_min: float,
        reason: str = "confirmed_civilian",
        expected_revision: int | None = None,
    ) -> AisUpdateState:
        current = self.state(mmsi)
        if expected_revision is not None and current.revision != expected_revision:
            raise ValueError("ais registry revision conflict")
        if not current.enabled and current.reason == reason:
            return current
        updated = AisUpdateState(
            mmsi=mmsi,
            enabled=False,
            revision=current.revision + 1,
            changed_at_min=float(now_min),
            reason="confirmed_civilian",
        )
        self._states[mmsi] = updated
        return updated

    def enable(self, mmsi: str, *, now_min: float, expected_revision: int | None = None) -> AisUpdateState:
        current = self.state(mmsi)
        if expected_revision is not None and current.revision != expected_revision:
            raise ValueError("ais registry revision conflict")
        updated = AisUpdateState(mmsi, True, current.revision + 1, float(now_min), "unclassified")
        self._states[mmsi] = updated
        return updated

    def snapshot(self) -> tuple[AisUpdateState, ...]:
        return tuple(self._states[key] for key in sorted(self._states))


__all__ = ["AisUpdateRegistry", "EvidenceStore"]

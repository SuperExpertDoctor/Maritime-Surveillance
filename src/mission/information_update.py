"""Atomic observation-to-evidence-to-information-field updates."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np

from src.mission.contracts import (
    BearingKernel,
    CovarianceKernel,
    EvidenceRecord,
    EvasiveManeuverFact,
    InfoFieldDelta,
    InformationSnapshot,
    PassiveBearingObservation,
    PassivePosition,
    PointKernel,
)
from src.mission.evidence_store import AisUpdateRegistry, EvidenceStore


@dataclass(frozen=True)
class ScanRefresh:
    bbox: tuple[int, int, int, int]
    scan_kind: str = "search"


_TTL = {
    "ais_position": (15.0, 5.0, 0.35),
    "sar_contact": (30.0, 10.0, 0.55),
    "eo_class": (60.0, 20.0, None),
    "eo_activity": (30.0, 10.0, None),
    "passive_bearing": (10.0, 3.0, 0.60),
    "passive_position": (15.0, 5.0, 1.00),
    "evasive_maneuver": (20.0, 8.0, 1.00),
    "type_ii_assessment": (60.0, 20.0, 0.85),
    "violation_assessment": (60.0, 20.0, 1.00),
    "handoff": (10.0, 5.0, 1.00),
    "track_loss": (20.0, 8.0, 1.00),
}


class InformationUpdatePolicy:
    """Owns the only mutable evidence and information version boundary."""

    def __init__(self, config, *, evidence_store: EvidenceStore | None = None) -> None:
        self.config = config
        self.cols, self.rows = tuple(config.grid.resolution)
        self.evidence_store = evidence_store or EvidenceStore()
        self.ais_updates = AisUpdateRegistry()
        self._last_scan_time = np.full((self.cols, self.rows), -np.inf, dtype=float)
        self._track_scan = np.zeros((self.cols, self.rows), dtype=bool)
        self._version = 0
        self._recent_deltas: list[InfoFieldDelta] = []
        self._urgent_subjects: set[tuple] = set()
        self._passive_position_sources: dict[str, frozenset[str]] = {}
        self._committed_value = self.matrices(0.0)[3]
        self._committed_at_min = 0.0

    @property
    def version(self) -> int:
        return self._version

    @property
    def last_scan_time(self) -> np.ndarray:
        return self._last_scan_time.copy()

    def info_matrix(self, now_min: float) -> np.ndarray:
        return self._info_matrix(float(now_min)).copy()

    def value_matrix(self, now_min: float) -> np.ndarray:
        return self.matrices(float(now_min))[3].copy()

    def _weights(self) -> tuple[float, float, float]:
        update = getattr(self.config.mission, "information_update", None)
        if update is not None:
            return update.value_alpha, update.value_beta, update.value_gamma
        return self.config.grid.value_alpha, self.config.grid.value_beta, self.config.grid.value_gamma

    def _kernel_matrix(self, record: EvidenceRecord) -> np.ndarray:
        c_grid = np.arange(self.cols, dtype=float)[:, None]
        r_grid = np.arange(self.rows, dtype=float)[None, :]
        spatial = record.spatial
        if isinstance(spatial, PointKernel):
            sigma = max(spatial.sigma_cells, 1e-6)
            distance2 = (c_grid - spatial.mean_cells[0]) ** 2 + (r_grid - spatial.mean_cells[1]) ** 2
            return np.exp(-0.5 * distance2 / (sigma * sigma))
        if isinstance(spatial, BearingKernel):
            dx = c_grid - spatial.origin_cells[0]
            dy = r_grid - spatial.origin_cells[1]
            radians = math.radians(spatial.bearing_deg)
            forward = dx * math.cos(radians) + dy * math.sin(radians)
            perpendicular = -dx * math.sin(radians) + dy * math.cos(radians)
            sigma = spatial.sigma_origin_cells + np.maximum(forward, 0.0) * math.tan(
                math.radians(spatial.bearing_std_deg)
            )
            corridor = np.exp(-0.5 * (perpendicular / np.maximum(sigma, 1e-6)) ** 2)
            decay = np.exp(-np.maximum(forward, 0.0) / spatial.range_decay_cells)
            return np.where(forward >= 0.0, corridor * decay, 0.0)
        diagonal = max(
            float(spatial.covariance_cells2[0][0]),
            float(spatial.covariance_cells2[1][1]),
            1e-6,
        )
        distance2 = (c_grid - spatial.mean_cells[0]) ** 2 + (r_grid - spatial.mean_cells[1]) ** 2
        return np.exp(-0.5 * distance2 / diagonal)

    def _info_matrix(self, now_min: float) -> np.ndarray:
        info = np.zeros((self.cols, self.rows), dtype=float)
        finite = np.isfinite(self._last_scan_time)
        if np.any(finite):
            age = np.maximum(float(now_min) - self._last_scan_time, 0.0)
            search_half = max(float(self.config.grid.decay_half_life_min), 1e-6)
            track_half = max(float(self.config.grid.track_decay_half_life_min), 1e-6)
            half_life = np.where(self._track_scan, track_half, search_half)
            info[finite] = np.exp(-math.log(2.0) * age[finite] / half_life[finite])
        return np.clip(info, 0.0, 1.0)

    def _evidence_components(self, now_min: float) -> tuple[np.ndarray, np.ndarray]:
        strategic = np.zeros((self.cols, self.rows), dtype=float)
        timeliness = np.zeros((self.cols, self.rows), dtype=float)
        active = self.evidence_store.active_records(now_min)
        point_source_observations = {
            observation_id
            for record in active
            if record.kind == "passive_position"
            for observation_id in self._passive_position_sources.get(
                record.evidence_id, ()
            )
        }
        for record in active:
            # A released point is the stronger, direct observation for this
            # emitter/sample group. Its bearing corridors must not add a
            # second gain around the same source.
            if (record.kind == "passive_bearing"
                    and record.evidence_id in point_source_observations):
                continue
            age = max(0.0, now_min - record.observed_at_min)
            ttl, tau, _ = _TTL.get(record.kind, (15.0, 5.0, record.strength))
            kernel = self._kernel_matrix(record)
            strategic = np.maximum(strategic, record.strength * kernel * max(0.0, 1.0 - age / ttl))
            timeliness = np.maximum(timeliness, record.strength * kernel * math.exp(-age / tau))
        return np.clip(strategic, 0.0, 1.0), np.clip(timeliness, 0.0, 1.0)

    def matrices(self, now_min: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        info = self._info_matrix(now_min)
        strategic, timeliness = self._evidence_components(now_min)
        alpha, beta, gamma = self._weights()
        value = np.clip(alpha * (1.0 - info) + beta * strategic + gamma * timeliness, 0.0, 1.0)
        return info, strategic, timeliness, value

    def snapshot(self, now_min: float) -> InformationSnapshot:
        info, strategic, timeliness, value = self.matrices(now_min)
        return InformationSnapshot(
            version=self._version,
            frozen_at_min=float(now_min),
            info=tuple(tuple(row) for row in info.tolist()),
            strategic=tuple(tuple(row) for row in strategic.tolist()),
            timeliness=tuple(tuple(row) for row in timeliness.tolist()),
            value=tuple(tuple(row) for row in value.tolist()),
            recent_deltas=tuple(self._recent_deltas[-20:]),
        )

    def freeze_information_snapshot(self, now_min: float) -> InformationSnapshot:
        return self.snapshot(now_min)

    def has_point_evidence(self, subject_id: str) -> bool:
        return any(
            record.kind == "passive_position" and record.source_id == subject_id
            for record in self.evidence_store.active_records(float("inf"))
        )

    def _record_from_fact(self, fact, now_min: float) -> EvidenceRecord | None:
        if isinstance(fact, EvidenceRecord):
            return fact
        if isinstance(fact, PassiveBearingObservation):
            return EvidenceRecord(
                evidence_id=fact.observation_id,
                kind="passive_bearing",
                source_id=fact.emitter_track_id,
                contact_id=None,
                observed_at_min=fact.observed_at_min,
                expires_at_min=fact.observed_at_min + _TTL["passive_bearing"][0],
                strength=_TTL["passive_bearing"][2],
                spatial=BearingKernel(
                    origin_cells=fact.observer_position_cells,
                    bearing_deg=fact.bearing_deg,
                    bearing_std_deg=fact.bearing_std_deg,
                    sigma_origin_cells=0.5,
                    range_decay_cells=10.0,
                ),
            )
        if isinstance(fact, PassivePosition):
            return EvidenceRecord(
                evidence_id=fact.position_id,
                kind="passive_position",
                source_id=fact.emitter_track_id,
                contact_id=None,
                observed_at_min=fact.observed_at_min,
                expires_at_min=fact.observed_at_min + _TTL["passive_position"][0],
                strength=1.0,
                spatial=PointKernel(
                    mean_cells=fact.position_cells,
                    sigma_cells=float(self.config.grid.marker_sigma_cells),
                ),
            )
        if isinstance(fact, EvasiveManeuverFact):
            evasion = getattr(getattr(self.config.mission, "evasion", None),
                              "evidence_ttl_min", _TTL["evasive_maneuver"][0])
            return EvidenceRecord(
                evidence_id=fact.fact_id,
                kind="evasive_maneuver",
                source_id=fact.mmsi,
                contact_id=fact.contact_id,
                observed_at_min=fact.observed_at_min,
                expires_at_min=fact.observed_at_min + float(evasion),
                strength=1.0,
                spatial=CovarianceKernel(
                    mean_cells=fact.position_cells,
                    covariance_cells2=fact.covariance_cells2,
                ),
            )
        return None

    @staticmethod
    def _support(record: EvidenceRecord, epsilon: float, cols: int, rows: int) -> tuple[int, int, int, int]:
        spatial = record.spatial
        if isinstance(spatial, PointKernel):
            radius = max(1, int(math.ceil(spatial.sigma_cells * math.sqrt(2.0 * math.log(1.0 / epsilon)))))
            x, y = spatial.mean_cells
        elif isinstance(spatial, BearingKernel):
            radius = max(cols, rows)
            x, y = spatial.origin_cells
        else:
            radius = max(1, int(math.ceil(3.0 * math.sqrt(max(
                spatial.covariance_cells2[0][0], spatial.covariance_cells2[1][1], 1e-6
            )))))
            x, y = spatial.mean_cells
        return (
            max(0, int(math.floor(x - radius))),
            max(0, int(math.floor(y - radius))),
            min(cols, int(math.ceil(x + radius + 1))),
            min(rows, int(math.ceil(y + radius + 1))),
        )

    @staticmethod
    def _mask_bbox(mask: np.ndarray, cols: int, rows: int) -> tuple[int, int, int, int]:
        coordinates = np.argwhere(mask)
        if coordinates.size == 0:
            return (0, 0, cols, rows)
        c0, r0 = coordinates.min(axis=0)
        c1, r1 = coordinates.max(axis=0) + 1
        return (int(c0), int(r0), int(c1), int(r1))

    def _commit_delta(
        self,
        before: np.ndarray,
        after: np.ndarray,
        now_min: float,
        reasons: Iterable[str],
        causes: Iterable[str],
        *,
        urgent: bool = False,
    ) -> InfoFieldDelta | None:
        difference = np.abs(after - before)
        changed = difference > 1e-12
        threshold = float(self.config.grid.candidate_value_threshold)
        crossed = (before < threshold) != (after < threshold)
        max_delta = float(np.max(difference)) if difference.size else 0.0
        reason_codes = tuple(dict.fromkeys(reasons))
        cause_ids = tuple(dict.fromkeys(causes))
        should_publish = bool(
            urgent
            or max_delta >= 0.05
            or np.any(crossed)
            or "evidence_expired" in reason_codes
        )
        if not should_publish:
            return None

        previous_version = self._version
        self._version += 1
        self._committed_value = after.copy()
        self._committed_at_min = float(now_min)
        delta = InfoFieldDelta(
            previous_version=previous_version,
            version=self._version,
            changed_bbox=self._mask_bbox(changed | crossed, self.cols, self.rows),
            max_abs_value_delta=max_delta,
            value_changed=bool(np.any(changed)),
            crossed_candidate_threshold=bool(np.any(crossed)),
            urgent=bool(urgent),
            reason_codes=reason_codes,
            cause_evidence_ids=cause_ids,
        )
        self._recent_deltas.append(delta)
        self._recent_deltas[:] = self._recent_deltas[-20:]
        return delta

    def advance_time(self, now_min: float) -> InfoFieldDelta | None:
        now_min = float(now_min)
        if not math.isfinite(now_min) or now_min < 0.0:
            raise ValueError("now_min must be finite and non-negative")
        after = self.matrices(now_min)[3]
        expired = self.evidence_store.expired_ids_since(
            self._committed_at_min, now_min,
        )
        return self._commit_delta(
            self._committed_value,
            after,
            now_min,
            ("evidence_expired",) if expired else ("time_decay",),
            expired,
        )

    def apply_batch(self, facts: Iterable[object], now_min: float) -> InfoFieldDelta | None:
        facts = tuple(facts)
        active_subjects = {
            self.evidence_store.subject_key(record)
            for record in self.evidence_store.active_records(now_min)
        }
        self._urgent_subjects.intersection_update(active_subjects)
        drafts: list[EvidenceRecord] = []
        passive_position_sources: dict[str, frozenset[str]] = {}
        scans: list[ScanRefresh] = []
        for fact in facts:
            if isinstance(fact, ScanRefresh):
                if fact.scan_kind not in ("search", "track") or len(fact.bbox) != 4:
                    raise ValueError("invalid scan refresh")
                scans.append(fact)
                continue
            record = self._record_from_fact(fact, now_min)
            if record is None:
                raise TypeError(f"unsupported information fact: {type(fact).__name__}")
            if record.observed_at_min > now_min:
                raise ValueError("fact cannot be observed after transaction time")
            drafts.append(record)
            if isinstance(fact, PassivePosition):
                passive_position_sources[record.evidence_id] = frozenset(
                    fact.source_observation_ids
                )

        changed = False
        cause_ids: list[str] = []
        reason_codes: list[str] = []
        urgent = False
        for scan in scans:
            c0, r0, c1, r1 = scan.bbox
            clipped = (
                max(0, c0), max(0, r0), min(self.cols, c1), min(self.rows, r1)
            )
            if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
                raise ValueError("scan refresh bbox is outside the information field")
            scan_time = self._last_scan_time[clipped[0]:clipped[2], clipped[1]:clipped[3]]
            scan_kind = self._track_scan[clipped[0]:clipped[2], clipped[1]:clipped[3]]
            desired_kind = scan.scan_kind == "track"
            if np.all(scan_time == now_min) and np.all(scan_kind == desired_kind):
                continue
            scan_time[...] = now_min
            scan_kind[...] = desired_kind
            changed = True
            reason_codes.append(f"scan_{scan.scan_kind}")
        for record in drafts:
            existing = self.evidence_store.get(record.evidence_id)
            if existing is not None and existing == record:
                continue
            key = self.evidence_store.subject_key(record)
            is_new_urgent = record.kind in {
                "evasive_maneuver", "passive_position", "type_ii_assessment",
                "violation_assessment", "handoff",
            } and key not in self._urgent_subjects
            self.evidence_store.supersede(record, key)
            self._urgent_subjects.add(key)
            changed = True
            cause_ids.append(record.evidence_id)
            reason_codes.append(record.kind)
            urgent = urgent or is_new_urgent
        self._passive_position_sources.update(passive_position_sources)
        if not changed:
            return None
        after = self.matrices(now_min)[3]
        return self._commit_delta(
            self._committed_value,
            after,
            now_min,
            reason_codes,
            cause_ids,
            urgent=urgent,
        )


__all__ = ["InformationUpdatePolicy", "ScanRefresh"]

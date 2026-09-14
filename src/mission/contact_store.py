"""Observation-only contact association, histories and resource reservations.

Mutable state stays in this store. AIS and visual streams remain separate until
two successive, unambiguous matches support merging them. No vessel lookup is
available here; IDs, motion and lifecycle are derived exclusively from reports.
"""
from __future__ import annotations

from dataclasses import replace
import math

from src.env.ais_signal import AISSignal
from src.mission.config import ContactConfig
from src.mission.contracts import Assessment, ContactSnapshot, ObservationSample, VisualDetection


class ContactStore:
    def __init__(self, config: ContactConfig, *, cell_size_km: float,
                 ais_uncertainty_cells: float = 0.05):
        if not math.isfinite(cell_size_km) or cell_size_km <= 0:
            raise ValueError("cell_size_km must be positive and finite")
        self.config = config
        self.cell_size_km = cell_size_km
        self.ais_uncertainty_cells = ais_uncertainty_cells
        self._contacts: dict[str, ContactSnapshot] = {}
        self._aliases: dict[str, str] = {}
        self._mmsi: dict[str, str] = {}
        self._sample_contacts: dict[str, str] = {}
        self._confirmations: dict[str, tuple[str, int, float]] = {}
        self._events: list[dict] = []
        self._archive: list[ObservationSample] = []
        self._counter = 0
        self._now = 0.0

    @property
    def aliases(self) -> dict[str, str]:
        return {alias: self.resolve(alias) for alias in self._aliases}

    @property
    def events(self) -> tuple[dict, ...]:
        return tuple(dict(event) for event in self._events)

    @property
    def archived_samples(self) -> tuple[ObservationSample, ...]:
        return tuple(self._archive)

    def resolve(self, contact_id: str) -> str:
        while contact_id in self._aliases:
            contact_id = self._aliases[contact_id]
        return contact_id

    def snapshot(self, contact_id: str) -> ContactSnapshot:
        return self._contacts[self.resolve(contact_id)]

    def list_snapshots(self) -> tuple[ContactSnapshot, ...]:
        return tuple(self._contacts[key] for key in sorted(self._contacts)
                     if self._contacts[key].revision > 0)

    def _event(self, event_type: str, **data) -> None:
        self._events.append({"type": event_type, **data})

    def _create(self, position, timestamp, mmsi=None) -> str:
        self._counter += 1
        cid = f"C{self._counter:04d}"
        self._contacts[cid] = ContactSnapshot(
            cid, 0, "lost", "unknown", mmsi, timestamp, timestamp,
            tuple(position), None, 0.0, None, None, None, None, timestamp, ())
        return cid

    @staticmethod
    def _finite_time(value: float) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError("time must be finite and nonnegative")

    def ingest_ais(self, signal: AISSignal, received_at_min: float) -> str:
        self._finite_time(received_at_min)
        self._finite_time(signal.timestamp)
        if (not signal.mmsi or len(signal.reported_position) != 2
                or not all(math.isfinite(x) for x in signal.reported_position)
                or not math.isfinite(signal.reported_speed_kn) or signal.reported_speed_kn < 0
                or not math.isfinite(signal.reported_heading_deg)
                or signal.timestamp > received_at_min):
            raise ValueError("invalid AIS measurement")
        sample_id = f"AIS:{signal.mmsi}:{float(signal.timestamp).hex()}"
        if sample_id in self._sample_contacts:
            return self.resolve(self._sample_contacts[sample_id])
        self._now = max(self._now, received_at_min)
        cid = self._mmsi.get(signal.mmsi)
        if cid is None:
            cid = self._create(signal.reported_position, signal.timestamp, signal.mmsi)
            self._mmsi[signal.mmsi] = cid
        cid = self.resolve(cid)
        speed = signal.reported_speed_kn * 1.852 / 60 / self.cell_size_km
        heading = math.radians(signal.reported_heading_deg)
        sample = ObservationSample(
            sample_id=sample_id, contact_id=cid, observed_at_min=signal.timestamp,
            source="ais", source_id=signal.mmsi, position_cells=tuple(signal.reported_position),
            velocity_cells_min=(speed * math.cos(heading), speed * math.sin(heading)),
            position_uncertainty_cells=self.ais_uncertainty_cells,
            observer_position_cells=None, measured_range_cells=None, navigation_context="unknown")
        if self._append(sample):
            # Visual-first reception uses the same two-confirmation rule.
            vid, ambiguous = self._nearest(signal.reported_position, signal.timestamp,
                                           [c for c in self.list_snapshots() if c.ais_mmsi is None])
            if vid is not None:
                # A second nearby AIS identity must also pass the ambiguity gate.
                candidate, tied = self._nearest(
                    self.snapshot(vid).estimated_position_cells, signal.timestamp,
                    [c for c in self.list_snapshots() if c.ais_mmsi is not None])
                self._confirm(vid, cid if candidate == cid and not tied else None,
                              signal.timestamp)
            elif ambiguous:
                self._event("association_ambiguous", contact_id=cid)
        return self.resolve(cid)

    def ingest_visual(self, detection: VisualDetection) -> str:
        if not isinstance(detection, VisualDetection):
            raise TypeError("expected VisualDetection")
        if detection.sample_id in self._sample_contacts:
            return self.resolve(self._sample_contacts[detection.sample_id])
        self._now = max(self._now, detection.observed_at_min)
        visual_contacts = [c for c in self.list_snapshots()
                           if any(s.source != "ais" for s in c.samples)]
        cid, ambiguous = self._nearest(detection.position_cells, detection.observed_at_min,
                                      visual_contacts)
        if cid is None:
            cid = self._create(detection.position_cells, detection.observed_at_min)
        # Association happens before constructing the public sample. No empty ID.
        sample = ObservationSample(
            sample_id=detection.sample_id, contact_id=cid,
            observed_at_min=detection.observed_at_min, source=detection.source,
            source_id=detection.source_id, position_cells=detection.position_cells,
            velocity_cells_min=detection.velocity_cells_min,
            position_uncertainty_cells=detection.position_uncertainty_cells,
            observer_position_cells=detection.observer_position_cells,
            measured_range_cells=detection.measured_range_cells,
            navigation_context=detection.navigation_context)
        if not self._append(sample):
            return cid
        if self.snapshot(cid).ais_mmsi is None:
            aid, ais_ambiguous = self._nearest(
                detection.position_cells, detection.observed_at_min,
                [c for c in self.list_snapshots() if c.ais_mmsi is not None])
            self._confirm(cid, aid if not ambiguous else None, detection.observed_at_min)
            ambiguous = ambiguous or ais_ambiguous
        if ambiguous:
            self._event("association_ambiguous", contact_id=cid,
                        sample_id=detection.sample_id)
        return self.resolve(cid)

    def _nearest(self, position, timestamp, candidates) -> tuple[str | None, bool]:
        distances = []
        for c in candidates:
            if c.state == "departed" or abs(timestamp - c.last_seen_min) > self.config.stale_after_min:
                continue
            velocity = c.estimated_velocity_cells_min or (0.0, 0.0)
            dt = timestamp - c.last_seen_min
            predicted = tuple(p + v * dt for p, v in zip(c.estimated_position_cells, velocity))
            distances.append((math.dist(position, predicted), c.contact_id))
        distances.sort()
        if not distances or distances[0][0] > self.config.association_gate_cells:
            return None, False
        if len(distances) > 1 and distances[1][0] - distances[0][0] <= self.config.association_margin_cells:
            return None, True
        return distances[0][1], False

    def _confirm(self, vid: str, aid: str | None, timestamp: float) -> None:
        previous = self._confirmations.get(vid)
        if aid is None:
            self._confirmations.pop(vid, None)
            return
        if previous is not None and timestamp <= previous[2]:
            return  # duplicate time or delayed packets cannot confirm a merge
        count = (previous[1] + 1 if previous and previous[0] == aid
                 and timestamp - previous[2] <= self.config.max_sample_gap_min else 1)
        self._confirmations[vid] = (aid, count, timestamp)
        if count >= 2:
            self._merge(aid, vid)

    def _append(self, sample: ObservationSample) -> bool:
        self._sample_contacts[sample.sample_id] = sample.contact_id
        if sample.observed_at_min < self._now - self.config.history_window_min:
            self._archive.append(sample)
            return False
        c = self.snapshot(sample.contact_id)
        if c.revision == 0:
            c = replace(c, first_seen_min=sample.observed_at_min,
                        last_seen_min=sample.observed_at_min,
                        next_probe_not_before_min=sample.observed_at_min)
            self._event("contact_created", contact_id=c.contact_id,
                        observed_at_min=sample.observed_at_min)
        samples = self._trim((*c.samples, sample))
        c = replace(c, revision=c.revision + 1, samples=samples,
                    first_seen_min=min(c.first_seen_min, sample.observed_at_min),
                    last_seen_min=max(c.last_seen_min, sample.observed_at_min),
                    state=("cleared" if c.identity == "civilian" else "pending")
                    if c.state == "lost" and self._now - sample.observed_at_min <= self.config.stale_after_min
                    else c.state)
        self._contacts[c.contact_id] = self._estimate(c)
        return True

    def _trim(self, samples) -> tuple[ObservationSample, ...]:
        ordered = sorted(samples, key=lambda s: (s.observed_at_min, s.sample_id))
        active = [s for s in ordered if s.observed_at_min >= self._now - self.config.history_window_min]
        retained = active[-self.config.history_max_samples:]
        retained_ids = {s.sample_id for s in retained}
        self._archive.extend(s for s in ordered if s.sample_id not in retained_ids)
        return tuple(retained)

    def _estimate(self, c: ContactSnapshot) -> ContactSnapshot:
        if not c.samples:
            return c
        latest = c.samples[-1]
        velocity = latest.velocity_cells_min
        if velocity is None:
            same_source = [s for s in c.samples[:-1] if s.source == latest.source
                           and s.source_id == latest.source_id
                           and s.observed_at_min < latest.observed_at_min]
            if same_source:
                previous = same_source[-1]
                dt = latest.observed_at_min - previous.observed_at_min
                if dt <= self.config.max_sample_gap_min:
                    velocity = tuple((p - q) / dt for p, q in zip(
                        latest.position_cells, previous.position_cells))
        return replace(c, estimated_position_cells=latest.position_cells,
                       estimated_velocity_cells_min=velocity,
                       uncertainty_cells=latest.position_uncertainty_cells)

    def _merge(self, aid: str, vid: str) -> None:
        a, v = self.snapshot(aid), self.snapshot(vid)
        owner = a if a.assigned_uav_id is not None else v
        if a.assigned_uav_id is not None and v.assigned_uav_id is not None:
            self._event("duplicate_task_cancelled", contact_id=aid, alias_contact_id=vid,
                        uav_id=v.assigned_uav_id, probe_id=v.active_probe_id)
        assessed = max((a, v), key=lambda c: c.last_assessment.assessed_at_min
                       if c.last_assessment else -1)
        merged = replace(a, revision=a.revision + v.revision + 1,
                         first_seen_min=min(a.first_seen_min, v.first_seen_min),
                         last_seen_min=max(a.last_seen_min, v.last_seen_min),
                         state=assessed.state if assessed.last_assessment else owner.state,
                         identity=assessed.identity, last_assessment=assessed.last_assessment,
                         cleared_at_min=assessed.cleared_at_min,
                         next_probe_not_before_min=max(a.next_probe_not_before_min, v.next_probe_not_before_min),
                         assigned_uav_id=owner.assigned_uav_id, active_probe_id=owner.active_probe_id,
                         samples=self._trim(tuple(replace(s, contact_id=aid) for s in (*a.samples, *v.samples))))
        if merged.identity == "civilian":
            merged = replace(merged, state="cleared", assigned_uav_id=None, active_probe_id=None)
        self._contacts[aid] = self._estimate(merged)
        del self._contacts[vid]
        self._aliases[vid] = aid
        self._confirmations.pop(vid, None)
        self._event("contact_merged", contact_id=aid, alias_contact_id=vid,
                    assigned_uav_id=merged.assigned_uav_id, active_probe_id=merged.active_probe_id)

    def expire(self, now_min: float) -> tuple[str, ...]:
        self._finite_time(now_min)
        self._now = max(self._now, now_min)
        expired = []
        for c in self.list_snapshots():
            c = replace(c, samples=self._trim(c.samples))
            self._contacts[c.contact_id] = c
            if c.state not in ("lost", "departed") and now_min - c.last_seen_min > self.config.stale_after_min:
                self._contacts[c.contact_id] = replace(c, state="lost", assigned_uav_id=None, active_probe_id=None)
                self._confirmations.pop(c.contact_id, None)
                expired.append(c.contact_id)
                self._event("contact_lost", contact_id=c.contact_id, observed_at_min=now_min,
                            uav_id=c.assigned_uav_id, probe_id=c.active_probe_id)
        return tuple(expired)

    def reserve(self, contact_id: str, uav_id: str, probe_id: str | None) -> None:
        c = self.snapshot(contact_id)
        if not uav_id or c.state in ("cleared", "lost", "departed") or self._now < c.next_probe_not_before_min:
            raise ValueError("contact cannot be reserved")
        if c.assigned_uav_id is not None and (c.assigned_uav_id, c.active_probe_id) != (uav_id, probe_id):
            raise ValueError("contact already reserved")
        if any(other.contact_id != c.contact_id and other.assigned_uav_id == uav_id
               for other in self.list_snapshots()):
            raise ValueError("UAV already reserved")
        self._contacts[c.contact_id] = replace(c, assigned_uav_id=uav_id, active_probe_id=probe_id,
                                              state="approaching" if probe_id else "tracking")

    def release(self, contact_id: str, now_min: float, reason: str) -> None:
        self._finite_time(now_min)
        c = self.snapshot(contact_id)
        if c.assigned_uav_id is None and c.active_probe_id is None:
            return
        state = c.state if c.state in ("cleared", "lost", "departed") else "pending"
        cooldown = (now_min + self.config.probe_retry_cooldown_min
                    if reason in ("timeout", "probe_timeout", "approach_timeout")
                    else c.next_probe_not_before_min)
        self._contacts[c.contact_id] = replace(c, state=state, assigned_uav_id=None,
                                              active_probe_id=None, next_probe_not_before_min=cooldown)
        self._event("contact_released", contact_id=c.contact_id, uav_id=c.assigned_uav_id,
                    probe_id=c.active_probe_id, reason=reason)

    def apply_assessment(self, assessment: Assessment) -> None:
        """Apply a service-validated assessment; reject stale probe/history references.

        Behavioral evidence sufficiency is the assessor's responsibility (T08).
        This store checks the references again before mutating shared state.
        """
        c = self.snapshot(assessment.contact_id)
        if c.last_assessment == assessment:
            return
        sample_ids = {s.sample_id for s in c.samples}
        if (assessment.history_revision != c.revision or assessment.probe_id != c.active_probe_id
                or not assessment.probe_id or assessment.identity not in ("unknown", "civilian", "target")
                or not math.isfinite(assessment.confidence) or not 0 <= assessment.confidence <= 1
                or not set(assessment.evidence_sample_ids) <= sample_ids):
            raise ValueError("assessment does not match current contact history/probe")
        self._finite_time(assessment.assessed_at_min)
        if assessment.identity != "unknown" and (
                assessment.confidence < self.config.assessment_confidence_min
                or not any(s.source != "ais" and s.sample_id in assessment.evidence_sample_ids
                           for s in c.samples)):
            raise ValueError("terminal assessment requires confident visual evidence")
        c = replace(c, identity=assessment.identity, last_assessment=assessment)
        if assessment.identity == "civilian":
            c = replace(c, state="cleared", cleared_at_min=assessment.assessed_at_min,
                        next_probe_not_before_min=assessment.assessed_at_min + self.config.civilian_recheck_cooldown_min)
        elif assessment.identity == "target":
            c = replace(c, state="tracking")
        self._contacts[c.contact_id] = c
        if c.state == "cleared":
            self.release(c.contact_id, assessment.assessed_at_min, "civilian")

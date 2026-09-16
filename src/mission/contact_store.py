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
from src.mission.contracts import (
    Assessment,
    ContactAssessment,
    ContactSnapshot,
    EvidenceRecord,
    ObservationSample,
    PassivePosition,
    PointKernel,
    VisualDetection,
)


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
        self._emitter_contacts: dict[str, str] = {}
        self._passive_position_ids: dict[str, str] = {}
        self._passive_bursts: dict[str, dict[str, float]] = {}
        self._radiation_activity_bursts: dict[str, set[str]] = {}
        self._radiation_activity_queue: list[EvidenceRecord] = []
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

    @property
    def emitter_contacts(self) -> dict[str, str]:
        """Return stable signal-track associations without exposing vessel truth."""
        return {
            emitter: self.resolve(contact_id)
            for emitter, contact_id in self._emitter_contacts.items()
        }

    def passive_burst_count(
        self,
        contact_id: str,
        now_min: float,
        *,
        window_min: float = 10.0,
    ) -> int:
        self._finite_time(now_min)
        if not math.isfinite(window_min) or window_min <= 0.0:
            raise ValueError("window_min must be positive and finite")
        resolved = self.resolve(contact_id)
        bursts = self._passive_bursts.get(resolved, {})
        return len({
            burst_id for burst_id, timestamp in bursts.items()
            if now_min - timestamp <= window_min
        })

    def drain_radiation_activity_evidence(self) -> tuple[EvidenceRecord, ...]:
        """Return newly qualified radiation activity facts once, in order."""
        evidence = tuple(self._radiation_activity_queue)
        self._radiation_activity_queue.clear()
        return evidence

    def ingest_passive_position(
        self,
        position: PassivePosition,
        *,
        association_radius_cells: float = 1.0,
        radiation_window_min: float = 10.0,
        min_distinct_bursts: int = 2,
    ) -> str:
        """Associate a released passive position using observation geometry only.

        The emitter track is stable across bursts.  A visual/AIS contact is
        reused only when it is the unique nearest prediction inside the
        configured radius; an equidistant pair remains a separate signal
        contact and emits an ambiguity event.
        """
        if not isinstance(position, PassivePosition):
            raise TypeError("position must be PassivePosition")
        if (not math.isfinite(association_radius_cells)
                or association_radius_cells <= 0.0):
            raise ValueError("association_radius_cells must be positive and finite")
        if not math.isfinite(radiation_window_min) or radiation_window_min <= 0.0:
            raise ValueError("radiation_window_min must be positive and finite")
        if (isinstance(min_distinct_bursts, bool)
                or not isinstance(min_distinct_bursts, int)
                or min_distinct_bursts < 1):
            raise ValueError("min_distinct_bursts must be a positive integer")
        known = self._passive_position_ids.get(position.position_id)
        if known is not None:
            return self.resolve(known)
        self._finite_time(position.observed_at_min)
        self._now = max(self._now, position.observed_at_min)

        contact_id = self._emitter_contacts.get(position.emitter_track_id)
        if contact_id is not None:
            contact_id = self.resolve(contact_id)
        else:
            signal_ids = {
                self.resolve(value) for value in self._emitter_contacts.values()
            }
            distances = []
            for contact in self.list_snapshots():
                if contact.contact_id in signal_ids or contact.state == "departed":
                    continue
                if abs(position.observed_at_min - contact.last_seen_min) > self.config.stale_after_min:
                    continue
                predicted = self._predicted_position(contact, position.observed_at_min)
                distances.append((
                    math.dist(position.position_cells, predicted), contact.contact_id,
                ))
            distances.sort()
            if distances and distances[0][0] <= association_radius_cells:
                tied = len(distances) > 1 and abs(
                    distances[1][0] - distances[0][0]
                ) <= 1e-9
                if tied:
                    self._event(
                        "ambiguous_contact_association",
                        emitter_track_id=position.emitter_track_id,
                        position_id=position.position_id,
                    )
                else:
                    contact_id = distances[0][1]
                    self._event(
                        "passive_position_associated",
                        emitter_track_id=position.emitter_track_id,
                        position_id=position.position_id,
                        contact_id=contact_id,
                    )
            if contact_id is None:
                contact_id = self._create(
                    position.position_cells, position.observed_at_min,
                )
                self._event(
                    "passive_signal_contact_created",
                    emitter_track_id=position.emitter_track_id,
                    position_id=position.position_id,
                    contact_id=contact_id,
                )
            self._emitter_contacts[position.emitter_track_id] = contact_id

        contact_id = self.resolve(contact_id)
        contact = self.snapshot(contact_id)
        previous_position = contact.estimated_position_cells
        dt = position.observed_at_min - contact.last_seen_min
        velocity = contact.estimated_velocity_cells_min
        if dt > 1e-9 and contact.revision > 0:
            velocity = tuple(
                (current - previous) / dt
                for current, previous in zip(position.position_cells, previous_position)
            )
        state = contact.state
        if state == "lost":
            state = "cleared" if contact.vessel_class == "type_i" else "pending"
        updated = replace(
            contact,
            revision=max(1, contact.revision + 1),
            state=state,
            first_seen_min=min(contact.first_seen_min, position.observed_at_min),
            last_seen_min=max(contact.last_seen_min, position.observed_at_min),
            estimated_position_cells=tuple(position.position_cells),
            estimated_velocity_cells_min=velocity,
        )
        self._contacts[contact_id] = updated
        self._passive_position_ids[position.position_id] = contact_id
        bursts = self._passive_bursts.setdefault(contact_id, {})
        bursts[position.burst_id] = position.observed_at_min
        cutoff = position.observed_at_min - radiation_window_min
        self._passive_bursts[contact_id] = {
            burst_id: timestamp
            for burst_id, timestamp in bursts.items()
            if timestamp >= cutoff
        }
        qualified_bursts = self._radiation_activity_bursts.setdefault(contact_id, set())
        if (
            len(self._passive_bursts[contact_id]) >= min_distinct_bursts
            and position.burst_id not in qualified_bursts
        ):
            qualified_bursts.add(position.burst_id)
            self._radiation_activity_queue.append(EvidenceRecord(
                evidence_id=(
                    f"RADIATION-ACTIVITY:{position.emitter_track_id}:"
                    f"{position.burst_id}"
                ),
                kind="type_ii_assessment",
                source_id=position.emitter_track_id,
                contact_id=contact_id,
                observed_at_min=position.observed_at_min,
                expires_at_min=position.observed_at_min + 60.0,
                strength=0.85,
                spatial=PointKernel(
                    mean_cells=position.position_cells,
                    sigma_cells=1.0,
                ),
            ))
            self._event(
                "radiation_activity_evidence",
                contact_id=contact_id,
                emitter_track_id=position.emitter_track_id,
                burst_id=position.burst_id,
            )
        return contact_id

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
            # Every new packet for this MMSI must support the same visual pair.
            # A miss or a match to another visual contact breaks continuity.
            for previous_vid, (aid, _, _) in tuple(self._confirmations.items()):
                if aid == cid and previous_vid != vid:
                    self._confirm(previous_vid, None, signal.timestamp)
            if vid is not None:
                # A second nearby AIS identity must also pass the ambiguity gate.
                candidate, tied = self._nearest(
                    self._predicted_position(self.snapshot(vid), signal.timestamp), signal.timestamp,
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
        signal_contacts = {
            self.resolve(contact_id)
            for contact_id in self._emitter_contacts.values()
        }
        visual_contacts = [
            c for c in self.list_snapshots()
            if c.contact_id in signal_contacts
            or any(s.source != "ais" for s in c.samples)
        ]
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
        # _nearest resets only pairs involved in an ambiguous gate. A unique
        # visual match can invalidate its own AIS pair below; sharing a UAV
        # with another contact is not contradictory association evidence.
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

    @staticmethod
    def _predicted_position(contact: ContactSnapshot, timestamp: float) -> tuple[float, float]:
        velocity = contact.estimated_velocity_cells_min or (0.0, 0.0)
        dt = timestamp - contact.last_seen_min
        return tuple(p + v * dt for p, v in zip(contact.estimated_position_cells, velocity))

    def _nearest(self, position, timestamp, candidates) -> tuple[str | None, bool]:
        distances = []
        for c in candidates:
            if c.state == "departed" or abs(timestamp - c.last_seen_min) > self.config.stale_after_min:
                continue
            predicted = self._predicted_position(c, timestamp)
            distances.append((math.dist(position, predicted), c.contact_id))
        distances.sort()
        if not distances or distances[0][0] > self.config.association_gate_cells:
            return None, False
        if len(distances) > 1 and distances[1][0] - distances[0][0] <= self.config.association_margin_cells:
            # An ambiguous observation interrupts every involved pair, whether
            # this gate is searching visual candidates or AIS candidates.
            limit = max(self.config.association_gate_cells,
                        distances[0][0] + self.config.association_margin_cells)
            involved = {cid for distance, cid in distances if distance <= limit}
            for vid, (aid, _, _) in tuple(self._confirmations.items()):
                if vid in involved or aid in involved:
                    self._confirmations.pop(vid)
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
                    state=("cleared" if c.vessel_class == "type_i" else "pending")
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
                         last_assessment=assessed.last_assessment,
                         vessel_class=assessed.vessel_class,
                         class_confidence=assessed.class_confidence,
                         class_evidence_ids=assessed.class_evidence_ids,
                         activity=assessed.activity,
                         activity_confidence=assessed.activity_confidence,
                         activity_evidence_ids=assessed.activity_evidence_ids,
                         cleared_at_min=assessed.cleared_at_min,
                         next_probe_not_before_min=max(a.next_probe_not_before_min, v.next_probe_not_before_min),
                         assigned_uav_id=owner.assigned_uav_id, active_probe_id=owner.active_probe_id,
                         samples=self._trim(tuple(replace(s, contact_id=aid) for s in (*a.samples, *v.samples))))
        if merged.vessel_class == "type_i":
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
                or not assessment.probe_id or assessment.vessel_class not in ("unknown", "type_i", "type_ii")
                or not math.isfinite(assessment.confidence) or not 0 <= assessment.confidence <= 1
                or not set(assessment.evidence_sample_ids) <= sample_ids):
            raise ValueError("assessment does not match current contact history/probe")
        self._finite_time(assessment.assessed_at_min)
        if assessment.vessel_class != "unknown" and (
                assessment.confidence < self.config.assessment_confidence_min
                or not any(s.source != "ais" and s.sample_id in assessment.evidence_sample_ids
                           for s in c.samples)):
            raise ValueError("terminal assessment requires confident visual evidence")
        dimension_class = {
            "type_i": "type_i",
            "type_ii": "type_ii",
        }.get(assessment.vessel_class)
        c = replace(
            c,
            last_assessment=assessment,
            vessel_class=dimension_class or c.vessel_class,
            class_confidence=(
                assessment.confidence if dimension_class else c.class_confidence
            ),
        )
        if assessment.vessel_class == "type_i":
            c = replace(c, state="cleared", cleared_at_min=assessment.assessed_at_min,
                        next_probe_not_before_min=assessment.assessed_at_min + self.config.type_i_recheck_cooldown_min)
        elif assessment.vessel_class == "type_ii":
            c = replace(c, state="tracking")
        self._contacts[c.contact_id] = c
        if assessment.vessel_class in ("type_i", "type_ii"):
            event_prefix = assessment.vessel_class
            self._event(
                f"{event_prefix}_assessed",
                contact_id=c.contact_id,
                assessed_at_min=assessment.assessed_at_min,
                vessel_class=assessment.vessel_class,
                confidence=assessment.confidence,
            )
        if assessment.vessel_class == "type_i":
            self.release(c.contact_id, assessment.assessed_at_min, "type_i_released")
            self._event(
                "type_i_released",
                contact_id=c.contact_id,
                assessed_at_min=assessment.assessed_at_min,
                vessel_class="type_i",
            )
        elif assessment.vessel_class == "type_ii":
            self._event(
                "type_ii_confirmed",
                contact_id=c.contact_id,
                assessed_at_min=assessment.assessed_at_min,
                vessel_class="type_ii",
            )

    def apply_dimension_assessment(
        self,
        contact_id: str,
        assessment: ContactAssessment,
        *,
        assessed_at_min: float,
        expected_revision: int | None = None,
    ) -> ContactSnapshot:
        """Commit an observation-derived class/activity assessment.

        This is separate from the legacy gateway ``Assessment`` because class
        and activity have independent evidence references and confidences.
        """
        if not isinstance(assessment, ContactAssessment):
            raise TypeError("assessment must be ContactAssessment")
        self._finite_time(assessed_at_min)
        contact = self.snapshot(contact_id)
        if expected_revision is not None and contact.revision != expected_revision:
            raise ValueError("contact revision conflict")
        for name in ("class_evidence_ids", "activity_evidence_ids"):
            ids = getattr(assessment, name)
            if any(not isinstance(item, str) or not item for item in ids):
                raise ValueError(f"{name} must contain non-empty IDs")
        if (
            assessment.vessel_class != "unknown"
            and assessment.class_confidence < self.config.assessment_confidence_min
        ):
            raise ValueError("class assessment confidence is below threshold")
        if (
            assessment.activity != "unknown"
            and assessment.activity_confidence < self.config.assessment_confidence_min
        ):
            raise ValueError("activity assessment confidence is below threshold")
        updated = replace(
            contact,
            vessel_class=assessment.vessel_class,
            class_confidence=assessment.class_confidence,
            class_evidence_ids=assessment.class_evidence_ids,
            activity=assessment.activity,
            activity_confidence=assessment.activity_confidence,
            activity_evidence_ids=assessment.activity_evidence_ids,
        )
        if assessment.activity in {"suspected_violation", "confirmed_violation"}:
            updated = replace(updated, state="tracking")
        self._contacts[updated.contact_id] = updated
        self._event(
            "assessment_changed",
            contact_id=updated.contact_id,
            assessed_at_min=assessed_at_min,
            vessel_class=assessment.vessel_class,
            activity=assessment.activity,
            class_evidence_ids=assessment.class_evidence_ids,
            activity_evidence_ids=assessment.activity_evidence_ids,
        )
        return updated

    # Explicit alias for callers using the contract's terminology.
    apply_contact_assessment = apply_dimension_assessment

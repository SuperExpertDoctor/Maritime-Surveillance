"""Deterministic observation-task handoff lifecycle."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from uuid import uuid4

from src.mission.contracts import HandoffAttempt


@dataclass(frozen=True)
class HandoffEvidence:
    handoff_id: str
    evidence_id: str
    mean: tuple[float, float] | None
    covariance_cells2: tuple[tuple[float, float], tuple[float, float]] | None
    direction: tuple[float, float] | None
    observed_at_min: float


@dataclass(frozen=True)
class ContactTaskState:
    contact_id: str
    status: str
    completed_at_min: float | None = None


def _time(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and non-negative")
    value = float(value)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


class HandoffManager:
    """Own handoff attempts without completing the underlying contact task."""

    def __init__(self, *, assignment_deadline_min: float = 5.0,
                 lock_deadline_min: float = 10.0):
        if assignment_deadline_min <= 0 or lock_deadline_min <= assignment_deadline_min:
            raise ValueError("handoff deadlines must be positive and ordered")
        self.assignment_deadline_min = float(assignment_deadline_min)
        self.lock_deadline_min = float(lock_deadline_min)
        self._contacts: dict[str, ContactTaskState] = {}
        self._attempts: dict[str, HandoffAttempt] = {}
        self._evidence: dict[str, HandoffEvidence] = {}

    def register_contact(self, contact_id: str, *, status: str = "observing") -> ContactTaskState:
        if not contact_id:
            raise ValueError("contact_id must be non-empty")
        current = self._contacts.get(contact_id)
        if current is None:
            current = ContactTaskState(contact_id, status)
            self._contacts[contact_id] = current
        return current

    def task(self, contact_id: str) -> ContactTaskState:
        return self._contacts[contact_id]

    def attempts(self) -> tuple[HandoffAttempt, ...]:
        return tuple(self._attempts[key] for key in sorted(self._attempts))

    def latest_for_contact(self, contact_id: str):
        attempts = [item for item in self.attempts() if item.contact_id == contact_id]
        return max(attempts, key=lambda item: item.required_at_min, default=None)

    def observed_successors(self, contact, now_min: float) -> frozenset[str]:
        """Only recent SAR/EO samples authorize seamless or reacquired tracking."""
        attempt = self.latest_for_contact(contact.contact_id)
        if attempt is None:
            return frozenset()
        return frozenset(
            sample.source_id for sample in contact.samples
            if sample.source in {"sar", "eo"}
            and sample.source_id != attempt.source_uav_id
            and max(attempt.required_at_min - 1.0, now_min - 1.0)
                <= sample.observed_at_min <= now_min
            and (attempt.successor_uav_id is None
                 or sample.source_id == attempt.successor_uav_id)
        )

    def evidence(self, handoff_id: str) -> HandoffEvidence:
        return self._evidence[handoff_id]

    def require(
        self,
        contact_id: str,
        *,
        source_uav_id: str,
        required_at_min: float,
        last_position=None,
        velocity_cells_min=(0.0, 0.0),
        last_observed_at_min: float | None = None,
        bearing=None,
    ) -> HandoffAttempt:
        required_at = _time(required_at_min, "required_at_min")
        self.register_contact(contact_id, status="observing")
        active = [attempt for attempt in self._attempts.values()
                  if attempt.contact_id == contact_id
                  and attempt.state in {"required", "pending"}]
        if active:
            return max(active, key=lambda attempt: attempt.required_at_min)
        if not source_uav_id:
            raise ValueError("source_uav_id must be non-empty")
        if last_position is None and bearing is None:
            raise ValueError("handoff requires a point or bearing state")
        evidence_id = f"handoff-evidence-{uuid4().hex}"
        handoff_id = f"handoff-{uuid4().hex}"
        mean = None
        covariance = None
        direction = None
        if last_position is not None:
            if len(last_position) != 2 or len(velocity_cells_min) != 2:
                raise ValueError("position and velocity must be pairs")
            observed_at = required_at if last_observed_at_min is None else _time(
                last_observed_at_min, "last_observed_at_min")
            dt = max(0.0, required_at - observed_at)
            mean = (
                float(last_position[0]) + float(velocity_cells_min[0]) * dt,
                float(last_position[1]) + float(velocity_cells_min[1]) * dt,
            )
            variance = max(0.25, 0.05 * (1.0 + dt * dt))
            covariance = ((variance, 0.0), (0.0, variance))
        else:
            if len(bearing) != 2 or math.hypot(float(bearing[0]), float(bearing[1])) <= 0:
                raise ValueError("bearing must be a non-zero pair")
            direction = (float(bearing[0]), float(bearing[1]))
            observed_at = required_at if last_observed_at_min is None else _time(
                last_observed_at_min, "last_observed_at_min")
        evidence = HandoffEvidence(
            handoff_id, evidence_id, mean, covariance, direction, observed_at
        )
        attempt = HandoffAttempt(
            handoff_id=handoff_id,
            contact_id=contact_id,
            source_uav_id=source_uav_id,
            successor_uav_id=None,
            evidence_id=evidence_id,
            required_at_min=required_at,
            assignment_deadline_min=required_at + self.assignment_deadline_min,
            lock_deadline_min=required_at + self.lock_deadline_min,
            assignment_committed_at_min=None,
            eo_lock_acquired_at_min=None,
            state="required",
            failure_reason=None,
        )
        self._attempts[handoff_id] = attempt
        self._evidence[handoff_id] = evidence
        self._contacts[contact_id] = replace(
            self._contacts[contact_id], status="handoff_required", completed_at_min=None
        )
        return attempt

    def commit_assignment(self, handoff_id: str, successor_uav_id: str,
                          committed_at_min: float) -> HandoffAttempt:
        at = _time(committed_at_min, "committed_at_min")
        attempt = self._attempts[handoff_id]
        if attempt.state != "required":
            raise ValueError("handoff assignment is not pending")
        if at > attempt.assignment_deadline_min:
            self._fail(handoff_id, "assignment_deadline")
            raise ValueError("handoff assignment deadline exceeded")
        if not successor_uav_id or successor_uav_id == attempt.source_uav_id:
            raise ValueError("successor UAV must differ from source UAV")
        attempt = replace(
            attempt,
            successor_uav_id=successor_uav_id,
            assignment_committed_at_min=at,
            state="pending",
        )
        self._attempts[handoff_id] = attempt
        self._contacts[attempt.contact_id] = replace(
            self._contacts[attempt.contact_id], status="handoff_pending"
        )
        return attempt

    def acquire_eo_lock(self, handoff_id: str, successor_uav_id: str,
                        acquired_at_min: float) -> HandoffAttempt:
        at = _time(acquired_at_min, "acquired_at_min")
        attempt = self._attempts[handoff_id]
        if attempt.state != "pending" or attempt.successor_uav_id != successor_uav_id:
            raise ValueError("handoff is not awaiting this successor EO lock")
        if at > attempt.lock_deadline_min:
            self._fail(handoff_id, "lock_deadline")
            raise ValueError("handoff EO lock deadline exceeded")
        attempt = replace(
            attempt, eo_lock_acquired_at_min=at, state="succeeded"
        )
        self._attempts[handoff_id] = attempt
        self._contacts[attempt.contact_id] = replace(
            self._contacts[attempt.contact_id], status="observing"
        )
        return attempt

    def advance(self, now_min: float) -> tuple[HandoffAttempt, ...]:
        now = _time(now_min, "now_min")
        failed = []
        for handoff_id, attempt in tuple(self._attempts.items()):
            if attempt.state == "required" and now > attempt.assignment_deadline_min:
                self._fail(handoff_id, "assignment_deadline")
                failed.append(self._attempts[handoff_id])
            elif attempt.state == "pending" and now > attempt.lock_deadline_min:
                self._fail(handoff_id, "lock_deadline")
                failed.append(self._attempts[handoff_id])
        return tuple(failed)

    def fail_for_contact(
        self, contact_id: str, at_min: float, reason: str,
    ) -> tuple[HandoffAttempt, ...]:
        """Fail all live handoffs for a removed or invalidated contact."""
        at = _time(at_min, "at_min")
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        failed = []
        for attempt in self.attempts():
            if attempt.contact_id != contact_id:
                continue
            if attempt.state not in {"required", "pending"}:
                continue
            failed.append(self._fail(attempt.handoff_id, reason))
        return tuple(failed)

    def _fail(self, handoff_id: str, reason: str) -> HandoffAttempt:
        attempt = self._attempts[handoff_id]
        failed = replace(
            attempt, state="failed", failure_reason=reason
        )
        self._attempts[handoff_id] = failed
        self._contacts[attempt.contact_id] = replace(
            self._contacts[attempt.contact_id], status="handoff_required"
        )
        return failed


__all__ = ["ContactTaskState", "HandoffEvidence", "HandoffManager"]

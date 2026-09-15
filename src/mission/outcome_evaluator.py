"""Evaluation-only truth snapshots and reproducible mission outcomes."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from src.mission.contracts import ContactSnapshot, IntentStatus, TaskRecord, Vec2


@dataclass(frozen=True)
class VesselTruthSample:
    ship_id: str
    identity: Literal["target", "civilian"]
    position_cells: Vec2
    departed: bool
    ais_on: bool
    gate_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.ship_id, str) or not self.ship_id:
            raise ValueError("ship_id must be a non-empty string")
        if self.identity not in {"target", "civilian"}:
            raise ValueError("identity must be target or civilian")
        if len(self.position_cells) != 2 or not all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in self.position_cells
        ):
            raise ValueError("position_cells must be finite")
        if not isinstance(self.departed, bool) or not isinstance(self.ais_on, bool):
            raise TypeError("departed and ais_on must be bools")
        if not isinstance(self.gate_state, str) or not self.gate_state:
            raise ValueError("gate_state must be a non-empty string")
        object.__setattr__(self, "position_cells", tuple(float(value) for value in self.position_cells))


@dataclass(frozen=True)
class EvaluationTick:
    sim_time_min: float
    dt_min: float
    vessels: tuple[VesselTruthSample, ...]
    uav_operations: tuple[tuple[str, str], ...]
    task_records: tuple[TaskRecord, ...]
    contacts: tuple[ContactSnapshot, ...]
    valid_eo_links: tuple[tuple[str, str, str], ...]
    intent_statuses: tuple[IntentStatus, ...]
    unique_coverage_ratio: float

    def __post_init__(self) -> None:
        _nonnegative_finite(self.sim_time_min, "sim_time_min")
        _nonnegative_finite(self.dt_min, "dt_min")
        if not 0.0 <= float(self.unique_coverage_ratio) <= 1.0:
            raise ValueError("unique_coverage_ratio must be in [0, 1]")
        object.__setattr__(self, "vessels", tuple(self.vessels))
        object.__setattr__(self, "uav_operations", tuple(
            (str(uav_id), str(operation)) for uav_id, operation in self.uav_operations
        ))
        object.__setattr__(self, "task_records", tuple(self.task_records))
        object.__setattr__(self, "contacts", tuple(self.contacts))
        object.__setattr__(self, "valid_eo_links", tuple(
            (str(uav_id), str(contact_id), str(ship_id))
            for uav_id, contact_id, ship_id in self.valid_eo_links
        ))
        object.__setattr__(self, "intent_statuses", tuple(self.intent_statuses))


@dataclass(frozen=True)
class EpisodeOutcome:
    episode_id: str
    valid: bool
    invalid_reasons: tuple[str, ...]
    unique_coverage_ratio: float
    intent_satisfaction_ratio: float | None
    target_tracking_ratio: float | None
    classification_accuracy: float | None
    false_civilian_ratio: float | None
    civilian_probe_uav_min: float
    total_uav_active_min: float
    mean_probe_wait_min: float | None
    task_switch_count: int
    score: float | None
    terminal_classified_vessels: int = 0
    observed_vessels: int = 0
    unknown_contacts: int = 0
    terminal_classification_coverage: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise ValueError("episode_id must be a non-empty string")
        if not isinstance(self.valid, bool):
            raise TypeError("valid must be bool")
        object.__setattr__(self, "invalid_reasons", tuple(self.invalid_reasons))
        for name in (
            "unique_coverage_ratio", "civilian_probe_uav_min", "total_uav_active_min",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.unique_coverage_ratio > 1.0:
            raise ValueError("unique_coverage_ratio must be in [0, 1]")
        for name in (
            "intent_satisfaction_ratio", "target_tracking_ratio",
            "classification_accuracy", "false_civilian_ratio",
            "terminal_classification_coverage", "score",
        ):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite or None")
        if isinstance(self.task_switch_count, bool) or self.task_switch_count < 0:
            raise ValueError("task_switch_count must be nonnegative")

    @property
    def civilian_probe_cost(self) -> float:
        return self.civilian_probe_uav_min / max(self.total_uav_active_min, 1.0)


class OutcomeEvaluator:
    """Aggregate execution facts without exposing truth to mission roles."""

    _INACTIVE_OPERATIONS = {"idle", "holding", "refueling", "off"}
    _TRACKING_OPERATIONS = {"tracking", "track"}

    def __init__(self, episode_id: str = "episode-unknown") -> None:
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("episode_id must be a non-empty string")
        self.episode_id = episode_id
        self._coverage = 0.0
        self._intent_satisfied_min = 0.0
        self._intent_observed_min = 0.0
        self._target_present_min: dict[str, float] = {}
        self._target_tracking_min: dict[str, float] = {}
        self._truth_identity: dict[str, str] = {}
        self._observed_vessels: set[str] = set()
        self._terminal_labels: dict[str, str] = {}
        self._terminal_label_times: dict[str, float] = {}
        self._observed_contact_identity: dict[str, str] = {}
        self._terminal_contact_ids: set[str] = set()
        self._probe_minutes = 0.0
        self._probe_waits: dict[str, float] = {}
        self._previous_tasks: dict[str, str] = {}
        self._task_switch_count = 0
        self._active_minutes = 0.0
        self._invalid_reasons: list[str] = []
        self._ticks = 0
        self._finalized: EpisodeOutcome | None = None

    def invalidate(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("invalid reason must be non-empty")
        if reason.strip() not in self._invalid_reasons:
            self._invalid_reasons.append(reason.strip())

    def observe(self, tick: EvaluationTick) -> None:
        if self._finalized is not None:
            raise RuntimeError("outcome has already been finalized")
        if not isinstance(tick, EvaluationTick):
            raise TypeError("tick must be an EvaluationTick")
        self._ticks += 1
        dt = float(tick.dt_min)
        self._coverage = max(self._coverage, float(tick.unique_coverage_ratio))
        start = max(0.0, float(tick.sim_time_min) - dt)
        end = float(tick.sim_time_min)

        truth_by_id = {vessel.ship_id: vessel for vessel in tick.vessels}
        for vessel in tick.vessels:
            self._truth_identity[vessel.ship_id] = vessel.identity
            if not vessel.departed:
                self._truth_identity[vessel.ship_id] = vessel.identity
                self._target_present_min.setdefault(vessel.ship_id, 0.0)
                if vessel.identity == "target":
                    self._target_present_min[vessel.ship_id] += dt

        contact_by_id = {contact.contact_id: contact for contact in tick.contacts}
        for contact in tick.contacts:
            self._observed_contact_identity[contact.contact_id] = contact.identity

        operations = {uav_id: operation.lower() for uav_id, operation in tick.uav_operations}
        active_tasks = {
            record.assigned_uav_id: record.task_id
            for record in tick.task_records
            if record.assigned_uav_id is not None and record.status == "executing"
        }
        for uav_id, task_id in active_tasks.items():
            previous = self._previous_tasks.get(uav_id)
            if previous is not None and previous != task_id:
                self._task_switch_count += 1
        self._previous_tasks = active_tasks

        good_tracking: set[str] = set()
        for uav_id, contact_id, physical_ship_id in tick.valid_eo_links:
            self._observed_vessels.add(physical_ship_id)
            vessel = truth_by_id.get(physical_ship_id)
            if vessel is not None and not vessel.departed and operations.get(uav_id, "") in self._TRACKING_OPERATIONS:
                good_tracking.add(physical_ship_id)
            contact = contact_by_id.get(contact_id)
            if contact is not None:
                assessment = contact.last_assessment
                identity = getattr(assessment, "identity", None)
                if identity in {"target", "civilian"}:
                    self._record_terminal_label(physical_ship_id, identity, tick.sim_time_min)
                    self._terminal_contact_ids.add(contact_id)

        for physical_ship_id in good_tracking:
            self._target_tracking_min[physical_ship_id] = (
                self._target_tracking_min.get(physical_ship_id, 0.0) + dt
            )

        if tick.intent_statuses:
            self._intent_observed_min += dt
            satisfied = sum(status.unmet_reason is None for status in tick.intent_statuses)
            self._intent_satisfied_min += dt * satisfied / len(tick.intent_statuses)

        for operation in operations.values():
            if operation not in self._INACTIVE_OPERATIONS:
                self._active_minutes += dt

        for record in tick.task_records:
            if record.kind != "probe" or not record.contact_id:
                continue
            contact = contact_by_id.get(record.contact_id)
            if contact is None or contact.identity != "civilian" or record.started_at_min is None:
                continue
            task_start = max(start, float(record.started_at_min))
            task_end = end if record.finished_at_min is None else min(end, float(record.finished_at_min))
            executed = max(0.0, task_end - task_start)
            self._probe_minutes += executed
            if record.task_id not in self._probe_waits:
                self._probe_waits[record.task_id] = max(
                    0.0, float(record.started_at_min) - float(record.created_at_min)
                )

    def finalize(self) -> EpisodeOutcome:
        if self._finalized is not None:
            return self._finalized
        outcome = self._build_outcome()
        self._finalized = outcome
        return outcome

    def snapshot(self) -> EpisodeOutcome:
        """Return current metrics without preventing later observations."""
        if self._finalized is not None:
            return self._finalized
        return self._build_outcome()

    def _build_outcome(self) -> EpisodeOutcome:
        target_ids = set(self._target_present_min)
        target_denominator = sum(self._target_present_min.values())
        tracking_numerator = sum(
            self._target_tracking_min.get(ship_id, 0.0)
            for ship_id in target_ids
        )
        target_tracking = (
            tracking_numerator / target_denominator if target_denominator else None
        )

        terminal_ids = {
            ship_id for ship_id in self._terminal_labels
            if ship_id in self._truth_identity
        }
        correct = sum(
            self._terminal_labels[ship_id] == self._truth_identity[ship_id]
            for ship_id in terminal_ids
        )
        classification_accuracy = correct / len(terminal_ids) if terminal_ids else None
        observed_count = len(self._observed_vessels)
        terminal_coverage = len(terminal_ids) / observed_count if observed_count else None
        false_target_ids = {
            ship_id for ship_id in target_ids
            if self._terminal_labels.get(ship_id) == "civilian"
        }
        false_civilian = len(false_target_ids) / len(target_ids) if target_ids else None
        intent_ratio = (
            self._intent_satisfied_min / self._intent_observed_min
            if self._intent_observed_min else None
        )
        total_active = getattr(self, "_active_minutes", 0.0)
        positive_components = [self._coverage]
        if intent_ratio is not None:
            positive_components.append(intent_ratio)
        if target_tracking is not None:
            positive_components.append(target_tracking)
        if classification_accuracy is not None:
            positive_components.append(classification_accuracy)
        score = sum(positive_components) / len(positive_components)
        score -= 0.15 * self._probe_minutes / max(total_active, 1.0)
        unknown_contacts = sum(
            identity == "unknown" for identity in self._observed_contact_identity.values()
        )
        reasons = tuple(self._invalid_reasons)
        if self._ticks == 0:
            reasons = (*reasons, "no_evaluation_ticks")
        return EpisodeOutcome(
            self.episode_id,
            not reasons,
            reasons,
            self._coverage,
            intent_ratio,
            target_tracking,
            classification_accuracy,
            false_civilian,
            self._probe_minutes,
            total_active,
            sum(self._probe_waits.values()) / len(self._probe_waits)
            if self._probe_waits else None,
            self._task_switch_count,
            score,
            len(terminal_ids),
            observed_count,
            unknown_contacts,
            terminal_coverage,
        )

    def _record_terminal_label(self, physical_ship_id: str, identity: str, time: float) -> None:
        previous_time = self._terminal_label_times.get(physical_ship_id)
        if previous_time is None or time >= previous_time:
            self._terminal_labels[physical_ship_id] = identity
            self._terminal_label_times[physical_ship_id] = time


def _nonnegative_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


__all__ = [
    "EpisodeOutcome",
    "EvaluationTick",
    "OutcomeEvaluator",
    "VesselTruthSample",
]

"""Evaluation-only truth snapshots and reproducible mission outcomes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
import math
from typing import Literal, Mapping

from src.mission.contracts import ContactSnapshot, IntentStatus, TaskRecord, Vec2


@dataclass(frozen=True)
class VesselTruthSample:
    ship_id: str
    vessel_class: Literal["type_i", "type_ii"]
    position_cells: Vec2
    departed: bool
    ais_enabled: bool
    gate_state: str

    def __post_init__(self) -> None:
        if not isinstance(self.ship_id, str) or not self.ship_id:
            raise ValueError("ship_id must be a non-empty string")
        if self.vessel_class not in {"type_i", "type_ii"}:
            raise ValueError("vessel_class must be type_i or type_ii")
        if len(self.position_cells) != 2 or not all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in self.position_cells
        ):
            raise ValueError("position_cells must be finite")
        if not isinstance(self.departed, bool) or not isinstance(self.ais_enabled, bool):
            raise TypeError("departed and ais_enabled must be bools")
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
    persistent_coverage: dict | None = field(default=None, kw_only=True)

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
        if self.persistent_coverage is not None:
            if not isinstance(self.persistent_coverage, dict):
                raise TypeError("persistent_coverage must be a mapping or None")
            object.__setattr__(self, "persistent_coverage", deepcopy(self.persistent_coverage))


@dataclass(frozen=True)
class EpisodeOutcome:
    episode_id: str
    valid: bool
    invalid_reasons: tuple[str, ...]
    unique_coverage_ratio: float
    intent_satisfaction_ratio: float | None
    type_ii_tracking_ratio: float | None
    classification_accuracy: float | None
    type_ii_misclassified_as_type_i_ratio: float | None
    type_i_probe_uav_min: float
    total_uav_active_min: float
    mean_probe_wait_min: float | None
    task_switch_count: int
    score: float | None
    terminal_classified_vessels: int = 0
    observed_vessels: int = 0
    unknown_contacts: int = 0
    terminal_classification_coverage: float | None = None
    classification_confusion: dict = field(default_factory=dict)
    type_i_recall: float | None = None
    type_ii_recall: float | None = None
    balanced_accuracy: float | None = None
    discovery_tracking_rate: float | None = None
    handoff_success_rate: float | None = None
    continuous_observation_rate: float | None = None
    decision_latency_seconds: dict = field(default_factory=dict)
    metric_denominators: dict = field(default_factory=dict)
    operational_failures: int = 0
    persistent_coverage: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise ValueError("episode_id must be a non-empty string")
        if not isinstance(self.valid, bool):
            raise TypeError("valid must be bool")
        object.__setattr__(self, "invalid_reasons", tuple(self.invalid_reasons))
        for name in (
            "unique_coverage_ratio", "type_i_probe_uav_min", "total_uav_active_min",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.unique_coverage_ratio > 1.0:
            raise ValueError("unique_coverage_ratio must be in [0, 1]")
        for name in (
            "intent_satisfaction_ratio", "type_ii_tracking_ratio",
            "classification_accuracy", "type_ii_misclassified_as_type_i_ratio",
            "terminal_classification_coverage", "score",
        ):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite or None")
        if isinstance(self.task_switch_count, bool) or self.task_switch_count < 0:
            raise ValueError("task_switch_count must be nonnegative")
        if isinstance(self.operational_failures, bool) or self.operational_failures < 0:
            raise ValueError("operational_failures must be nonnegative")
        if not isinstance(self.persistent_coverage, dict):
            raise TypeError("persistent_coverage must be a mapping")
        object.__setattr__(self, "persistent_coverage", deepcopy(self.persistent_coverage))

    @property
    def type_i_probe_cost(self) -> float:
        return self.type_i_probe_uav_min / max(self.total_uav_active_min, 1.0)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "EpisodeOutcome":
        """Read new or historical outcome JSON at the compatibility boundary."""
        from src.mission.vessel_compat import normalize_legacy_outcome_payload

        normalized = normalize_legacy_outcome_payload(payload)
        names = {item.name for item in fields(cls)}
        return cls(**{name: normalized[name] for name in names if name in normalized})


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
        self._type_ii_present_min: dict[str, float] = {}
        self._type_ii_tracking_min: dict[str, float] = {}
        self._truth_class: dict[str, str] = {}
        self._observed_vessels: set[str] = set()
        self._terminal_labels: dict[str, str] = {}
        self._terminal_label_times: dict[str, float] = {}
        self._observed_contact_class: dict[str, str] = {}
        self._terminal_contact_ids: set[str] = set()
        self._probe_minutes = 0.0
        self._probe_waits: dict[str, float] = {}
        self._previous_tasks: dict[str, str] = {}
        self._task_switch_count = 0
        self._active_minutes = 0.0
        self._invalid_reasons: list[str] = []
        self._ticks = 0
        self._last_observed_time: float | None = None
        self._persistent_coverage_samples: list[dict] = []
        self._finalized: EpisodeOutcome | None = None
        self._classification_matrix = {
            "type_i": {"type_i": 0, "type_ii": 0, "unknown": 0},
            "type_ii": {"type_i": 0, "type_ii": 0, "unknown": 0},
        }
        self._classification_recorded: set[str] = set()
        self._discovery_required: set[str] = set()
        self._discovery_success: set[str] = set()
        self._handoff_events: dict[str, dict] = {}
        self._survey_intervals: dict[str, list[tuple[float, float]]] = {}
        self._eo_lock_intervals: dict[str, list[tuple[float, float]]] = {}
        self._decision_latencies: list[dict] = []
        self._operational_failures = 0

    def invalidate(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("invalid reason must be non-empty")
        if reason.strip() not in self._invalid_reasons:
            self._invalid_reasons.append(reason.strip())

    @staticmethod
    def _classification_label(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized in {"type_i", "type_ii", "unknown"}:
            return normalized
        raise ValueError("classification labels must be type_i, type_ii, or unknown")

    def add_classification(self, *, truth: str, predicted: str, eligible: bool = True) -> None:
        """Record one eligible vessel classification, including unknown as an error."""
        if not isinstance(eligible, bool):
            raise TypeError("eligible must be bool")
        if not eligible:
            return
        truth_label = self._classification_label(truth)
        predicted_label = self._classification_label(predicted)
        if truth_label == "unknown":
            raise ValueError("classification truth cannot be unknown")
        self._classification_matrix[truth_label][predicted_label] += 1

    def register_discovery_target(self, vessel_id: str) -> None:
        if not isinstance(vessel_id, str) or not vessel_id:
            raise ValueError("vessel_id is required")
        self._discovery_required.add(vessel_id)

    def record_discovery_tracking(self, vessel_id: str, *, success: bool = True) -> None:
        self.register_discovery_target(vessel_id)
        if not isinstance(success, bool):
            raise TypeError("success must be bool")
        if success:
            self._discovery_success.add(vessel_id)

    def register_handoff(
        self,
        interruption_id: str,
        *,
        at_min: float,
        successor_uav_ids=(),
    ) -> dict:
        """Register one interruption; retries update the same ledger row."""
        if not isinstance(interruption_id, str) or not interruption_id:
            raise ValueError("interruption_id is required")
        _nonnegative_finite(at_min, "at_min")
        successors = tuple(sorted({str(item) for item in successor_uav_ids if item}))
        existing = self._handoff_events.get(interruption_id)
        if existing is not None:
            return dict(existing)
        event = {
            "interruption_id": interruption_id,
            "at_min": float(at_min),
            "successors": successors,
            "eligible": bool(successors),
            "status": "pending" if successors else "excluded_no_successor",
            "assignment_at_min": None,
            "lock_at_min": None,
            "failure_reason": None if successors else "no_feasible_successor",
        }
        self._handoff_events[interruption_id] = event
        return dict(event)

    def record_handoff_assignment(self, interruption_id: str, *, at_min: float) -> dict:
        event = self._handoff_events.get(interruption_id)
        if event is None:
            raise KeyError(interruption_id)
        _nonnegative_finite(at_min, "at_min")
        if not event["eligible"] or event["status"] == "success":
            return dict(event)
        if float(at_min) > event["at_min"] + 5.0:
            event["status"] = "failed"
            event["failure_reason"] = "assignment_deadline"
        else:
            event["assignment_at_min"] = float(at_min)
            event["status"] = "assigned"
        return dict(event)

    def record_handoff_lock(self, interruption_id: str, *, at_min: float) -> dict:
        event = self._handoff_events.get(interruption_id)
        if event is None:
            raise KeyError(interruption_id)
        _nonnegative_finite(at_min, "at_min")
        if event["status"] == "success":
            return dict(event)
        if event["status"] != "assigned":
            return dict(event)
        if float(at_min) > event["at_min"] + 10.0:
            event["status"] = "failed"
            event["failure_reason"] = "lock_deadline"
        else:
            event["lock_at_min"] = float(at_min)
            event["status"] = "success"
        return dict(event)

    def add_survey_interval(self, vessel_id: str, start_min: float, end_min: float) -> None:
        self._add_interval(self._survey_intervals, vessel_id, start_min, end_min)

    def add_eo_lock_interval(self, vessel_id: str, start_min: float, end_min: float) -> None:
        self._add_interval(self._eo_lock_intervals, vessel_id, start_min, end_min)

    @staticmethod
    def _add_interval(store: dict, vessel_id: str, start_min: float, end_min: float) -> None:
        if not isinstance(vessel_id, str) or not vessel_id:
            raise ValueError("vessel_id is required")
        _nonnegative_finite(start_min, "start_min")
        _nonnegative_finite(end_min, "end_min")
        if end_min <= start_min:
            raise ValueError("interval end must be after start")
        store.setdefault(vessel_id, []).append((float(start_min), float(end_min)))

    @staticmethod
    def _merge_intervals(intervals) -> list[tuple[float, float]]:
        merged: list[list[float]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(start, end) for start, end in merged]

    @classmethod
    def _interval_duration(cls, intervals) -> float:
        return sum(end - start for start, end in cls._merge_intervals(intervals))

    @classmethod
    def _intersection_duration(cls, left, right) -> float:
        left_merged = cls._merge_intervals(left)
        right_merged = cls._merge_intervals(right)
        total = 0.0
        right_index = 0
        for left_start, left_end in left_merged:
            while right_index < len(right_merged) and right_merged[right_index][1] <= left_start:
                right_index += 1
            index = right_index
            while index < len(right_merged) and right_merged[index][0] < left_end:
                total += max(0.0, min(left_end, right_merged[index][1]) - max(left_start, right_merged[index][0]))
                index += 1
        return total

    def record_decision_latency(
        self,
        *,
        snapshot_frozen_wall: float,
        decision_finished_wall: float,
        llm_seconds: float = 0.0,
        validation_seconds: float = 0.0,
        matching_seconds: float = 0.0,
        success: bool = True,
        failure_reason: str | None = None,
    ) -> dict:
        values = {
            "snapshot_frozen_wall": snapshot_frozen_wall,
            "decision_finished_wall": decision_finished_wall,
            "llm_seconds": llm_seconds,
            "validation_seconds": validation_seconds,
            "matching_seconds": matching_seconds,
        }
        for name, value in values.items():
            if name.endswith("wall"):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError(f"{name} must be finite")
            else:
                _nonnegative_finite(value, name)
        total = float(decision_finished_wall) - float(snapshot_frozen_wall)
        if total < 0:
            raise ValueError("decision_finished_wall must not precede snapshot_frozen_wall")
        if not isinstance(success, bool):
            raise TypeError("success must be bool")
        sample = {
            "total_seconds": total,
            "llm_seconds": float(llm_seconds),
            "validation_seconds": float(validation_seconds),
            "matching_seconds": float(matching_seconds),
            "success": success,
            "failure_reason": failure_reason,
        }
        self._decision_latencies.append(sample)
        if not success:
            self._operational_failures += 1
        return dict(sample)

    def _metric_summary(self) -> dict:
        denominators = {
            label: sum(row.values()) for label, row in self._classification_matrix.items()
        }
        recalls = {
            label: (
                self._classification_matrix[label][label] / denominators[label]
                if denominators[label] else None
            )
            for label in ("type_i", "type_ii")
        }
        balanced = (
            (recalls["type_i"] + recalls["type_ii"]) / 2
            if recalls["type_i"] is not None and recalls["type_ii"] is not None else None
        )
        eligible_handoffs = [item for item in self._handoff_events.values() if item["eligible"]]
        handoff_success = (
            sum(item["status"] == "success" for item in eligible_handoffs) / len(eligible_handoffs)
            if eligible_handoffs else None
        )
        survey_denominator = sum(
            self._interval_duration(intervals) for intervals in self._survey_intervals.values()
        )
        survey_numerator = sum(
            self._intersection_duration(
                self._survey_intervals.get(vessel_id, ()),
                self._eo_lock_intervals.get(vessel_id, ()),
            )
            for vessel_id in self._survey_intervals
        )
        continuous_rate = survey_numerator / survey_denominator if survey_denominator else None
        latencies = [item["total_seconds"] for item in self._decision_latencies]
        latency_summary = {
            "count": len(latencies),
            "failures": sum(not item["success"] for item in self._decision_latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
            "llm_seconds": sum(item["llm_seconds"] for item in self._decision_latencies),
            "validation_seconds": sum(item["validation_seconds"] for item in self._decision_latencies),
            "matching_seconds": sum(item["matching_seconds"] for item in self._decision_latencies),
        }
        return {
            "confusion_matrix": self._classification_matrix,
            "classification_denominators": denominators,
            "type_i_recall": recalls["type_i"],
            "type_ii_recall": recalls["type_ii"],
            "balanced_accuracy": balanced,
            "discovery_tracking_rate": (
                len(self._discovery_success & self._discovery_required) / len(self._discovery_required)
                if self._discovery_required else None
            ),
            "discovery_tracking_numerator": len(
                self._discovery_success & self._discovery_required
            ),
            "discovery_tracking_denominator": len(self._discovery_required),
            "handoff_success_rate": handoff_success,
            "handoff_success_numerator": sum(
                item["status"] == "success" for item in eligible_handoffs
            ),
            "handoff_denominator": len(eligible_handoffs),
            "handoff_excluded_no_successor": sum(
                not item["eligible"] for item in self._handoff_events.values()
            ),
            "continuous_observation_rate": continuous_rate,
            "continuous_observation_numerator_min": survey_numerator,
            "continuous_observation_denominator_min": survey_denominator,
            "decision_latency_seconds": latency_summary,
            "operational_failures": self._operational_failures,
            "na_reasons": {
                "type_i_recall": "no_eligible_type_i" if not denominators["type_i"] else None,
                "type_ii_recall": "no_eligible_type_ii" if not denominators["type_ii"] else None,
                "balanced_accuracy": "missing_class_denominator" if balanced is None else None,
                "discovery_tracking_rate": "no_required_type_ii_vessels" if not self._discovery_required else None,
                "handoff_success_rate": "no_eligible_handoff_interruptions" if not eligible_handoffs else None,
                "continuous_observation_rate": "no_survey_minutes" if not survey_denominator else None,
            },
            "handoff_ledger": tuple(dict(item) for item in self._handoff_events.values()),
        }

    @staticmethod
    def _weighted_percentile(samples: list[tuple[float, float]], fraction: float) -> float | None:
        if not samples:
            return None
        ordered = sorted(samples, key=lambda item: item[1])
        total = sum(duration for _, duration in ordered)
        if total <= 0:
            return None
        target = total * fraction
        accumulated = 0.0
        for value, duration in ordered:
            accumulated += duration
            if accumulated >= target:
                return value
        return ordered[-1][0]

    def _persistent_coverage_summary(self) -> dict:
        if not self._persistent_coverage_samples:
            return {
                "availability": False,
                "measurement_mode": "native",
                "sample_count": 0,
                "windows": {},
            }
        samples = sorted(self._persistent_coverage_samples, key=lambda item: item["sim_time_min"])
        schema_versions = {
            item["metrics"].get("schema_version")
            for item in samples
            if isinstance(item.get("metrics"), dict)
        }
        windows: dict[str, dict] = {}
        for window_min in (30, 60, 120):
            values: list[float] = []
            intervals: list[tuple[float, float, float]] = []
            for index, sample in enumerate(samples):
                metrics = sample["metrics"]
                window = next(
                    (
                        item for item in metrics.get("windows", ())
                        if isinstance(item, dict) and item.get("minutes") == window_min
                    ),
                    None,
                )
                value = window.get("coverage_pct") if window else None
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                    continue
                value = float(value)
                if window.get("window_complete", False):
                    values.append(value)
                if index + 1 < len(samples):
                    end = float(samples[index + 1]["sim_time_min"])
                    start = float(sample["sim_time_min"])
                    if end > start:
                        intervals.append((value, start, end))
            durations = [(value, end - start) for value, start, end in intervals]
            total_duration = sum(duration for _, duration in durations)
            windows[str(window_min)] = {
                "sample_count": len(values),
                "sample_mean_pct": sum(values) / len(values) if values else None,
                "time_mean_pct": (
                    sum(value * duration for value, duration in durations) / total_duration
                    if total_duration else None
                ),
                "p5_pct": self._weighted_percentile(durations, 0.05),
                "minimum_pct": min(values) if values else None,
                "duration_min": total_duration,
                "longest_zero_coverage_min": max(
                    (duration for value, duration in durations if value == 0.0),
                    default=0.0,
                ),
            }
        return {
            "availability": True,
            "measurement_mode": "native",
            "schema_versions": sorted(str(value) for value in schema_versions if value is not None),
            "sample_count": len(samples),
            "as_of_min": samples[-1]["sim_time_min"],
            "windows": windows,
        }

    def summary(self) -> dict:
        """Return the explicit acceptance metrics without finalizing the episode."""
        return self._metric_summary()

    @property
    def decision_latency_samples(self) -> tuple[dict, ...]:
        """Return defensive copies of raw planning latency samples."""
        return tuple(dict(sample) for sample in self._decision_latencies)

    def observe(self, tick: EvaluationTick) -> None:
        if self._finalized is not None:
            raise RuntimeError("outcome has already been finalized")
        if not isinstance(tick, EvaluationTick):
            raise TypeError("tick must be an EvaluationTick")
        end = float(tick.sim_time_min)
        if self._last_observed_time is not None:
            if end < self._last_observed_time:
                self.invalidate("out_of_order_evaluation_tick")
                return
            if end == self._last_observed_time:
                return
            dt = end - self._last_observed_time
        else:
            dt = min(float(tick.dt_min), end)
        self._last_observed_time = end
        self._ticks += 1
        self._coverage = max(self._coverage, float(tick.unique_coverage_ratio))
        start = max(0.0, end - dt)
        if tick.persistent_coverage is not None:
            self._persistent_coverage_samples.append({
                "sim_time_min": end,
                "metrics": deepcopy(tick.persistent_coverage),
            })

        truth_by_id = {vessel.ship_id: vessel for vessel in tick.vessels}
        for vessel in tick.vessels:
            self._truth_class[vessel.ship_id] = vessel.vessel_class
            if not vessel.departed:
                self._truth_class[vessel.ship_id] = vessel.vessel_class
                self._type_ii_present_min.setdefault(vessel.ship_id, 0.0)
                if vessel.vessel_class == "type_ii":
                    self._type_ii_present_min[vessel.ship_id] += dt
                    if vessel.gate_state == "survey":
                        self.add_survey_interval(vessel.ship_id, start, end)

        contact_by_id = {contact.contact_id: contact for contact in tick.contacts}
        for contact in tick.contacts:
            self._observed_contact_class[contact.contact_id] = contact.vessel_class

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
                self.add_eo_lock_interval(physical_ship_id, start, end)
            contact = contact_by_id.get(contact_id)
            if contact is not None:
                assessment = contact.last_assessment
                vessel_class = getattr(assessment, "vessel_class", None)
                if vessel_class in {"type_ii", "type_i"}:
                    self._record_terminal_label(physical_ship_id, vessel_class, tick.sim_time_min)
                    self._terminal_contact_ids.add(contact_id)

        for physical_ship_id in good_tracking:
            self._type_ii_tracking_min[physical_ship_id] = (
                self._type_ii_tracking_min.get(physical_ship_id, 0.0) + dt
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
            if contact is None or contact.vessel_class != "type_i" or record.started_at_min is None:
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
        type_ii_ids = set(self._type_ii_present_min)
        type_ii_denominator = sum(self._type_ii_present_min.values())
        tracking_numerator = sum(
            self._type_ii_tracking_min.get(ship_id, 0.0)
            for ship_id in type_ii_ids
        )
        type_ii_tracking = (
            tracking_numerator / type_ii_denominator if type_ii_denominator else None
        )

        terminal_ids = {
            ship_id for ship_id in self._terminal_labels
            if ship_id in self._truth_class
        }
        correct = sum(
            self._terminal_labels[ship_id] == self._truth_class[ship_id]
            for ship_id in terminal_ids
        )
        classification_accuracy = correct / len(terminal_ids) if terminal_ids else None
        observed_count = len(self._observed_vessels)
        terminal_coverage = len(terminal_ids) / observed_count if observed_count else None
        false_type_i_ids = {
            ship_id for ship_id in type_ii_ids
            if self._terminal_labels.get(ship_id) == "type_i"
        }
        type_ii_misclassified_as_type_i = (
            len(false_type_i_ids) / len(type_ii_ids) if type_ii_ids else None
        )
        intent_ratio = (
            self._intent_satisfied_min / self._intent_observed_min
            if self._intent_observed_min else None
        )
        total_active = getattr(self, "_active_minutes", 0.0)
        positive_components = [self._coverage]
        if intent_ratio is not None:
            positive_components.append(intent_ratio)
        if type_ii_tracking is not None:
            positive_components.append(type_ii_tracking)
        if classification_accuracy is not None:
            positive_components.append(classification_accuracy)
        score = sum(positive_components) / len(positive_components)
        score -= 0.15 * self._probe_minutes / max(total_active, 1.0)
        unknown_contacts = sum(
            vessel_class == "unknown" for vessel_class in self._observed_contact_class.values()
        )
        reasons = tuple(self._invalid_reasons)
        if self._ticks == 0:
            reasons = (*reasons, "no_evaluation_ticks")
        metrics = self._metric_summary()
        return EpisodeOutcome(
            self.episode_id,
            not reasons,
            reasons,
            self._coverage,
            intent_ratio,
            type_ii_tracking,
            classification_accuracy,
            type_ii_misclassified_as_type_i,
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
            classification_confusion=metrics["confusion_matrix"],
            type_i_recall=metrics["type_i_recall"],
            type_ii_recall=metrics["type_ii_recall"],
            balanced_accuracy=metrics["balanced_accuracy"],
            discovery_tracking_rate=metrics["discovery_tracking_rate"],
            handoff_success_rate=metrics["handoff_success_rate"],
            continuous_observation_rate=metrics["continuous_observation_rate"],
            decision_latency_seconds=metrics["decision_latency_seconds"],
            metric_denominators={
                **metrics["classification_denominators"],
                "discovery_tracking_numerator": metrics["discovery_tracking_numerator"],
                "discovery_tracking_denominator": metrics["discovery_tracking_denominator"],
                "handoff_success_numerator": metrics["handoff_success_numerator"],
                "handoff_success_denominator": metrics["handoff_denominator"],
                "handoff": metrics["handoff_denominator"],
                "continuous_observation_numerator_min": metrics[
                    "continuous_observation_numerator_min"
                ],
                "continuous_observation_denominator_min": metrics[
                    "continuous_observation_denominator_min"
                ],
                "continuous_observation_min": metrics["continuous_observation_denominator_min"],
            },
            operational_failures=metrics["operational_failures"],
            persistent_coverage=self._persistent_coverage_summary(),
        )

    def _record_terminal_label(self, physical_ship_id: str, identity: str, time: float) -> None:
        previous_time = self._terminal_label_times.get(physical_ship_id)
        if previous_time is None or time >= previous_time:
            self._terminal_labels[physical_ship_id] = identity
            self._terminal_label_times[physical_ship_id] = time
        if physical_ship_id not in self._classification_recorded and physical_ship_id in self._truth_class:
            self.add_classification(
                truth=self._truth_class[physical_ship_id],
                predicted=identity,
                eligible=True,
            )
            self._classification_recorded.add(physical_ship_id)


def _nonnegative_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


__all__ = [
    "EpisodeOutcome",
    "EvaluationTick",
    "OutcomeEvaluator",
    "VesselTruthSample",
]

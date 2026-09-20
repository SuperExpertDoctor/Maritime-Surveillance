"""Immutable public contracts for the mixed maritime mission domain."""

from dataclasses import dataclass, field
from copy import deepcopy
import hashlib
import math
from typing import Literal


Vec2 = tuple[float, float]
Rect = tuple[int, int, int, int]
VesselClass = Literal["unknown", "type_i", "type_ii"]
ActivityState = Literal[
    "unknown", "normal", "suspected_violation", "confirmed_violation"
]
EvidenceKind = Literal[
    "ais_position", "sar_contact", "eo_class", "eo_activity",
    "passive_bearing", "passive_position", "evasive_maneuver",
    "type_ii_assessment", "type_i_assessment", "track_loss", "handoff",
]
ContactState = Literal[
    "pending",
    "queued",
    "approaching",
    "observing",
    "tracking",
    "cleared",
    "lost",
    "departed",
]

SHIP_RNG_STREAMS = ("ship_class", "ship_ais_enabled")


def ship_rng_manifest(episode_seed: int) -> dict[str, int]:
    """Derive stable, independent ship population RNG seeds for a run manifest."""
    if isinstance(episode_seed, bool) or not isinstance(episode_seed, int):
        raise ValueError("episode_seed: expected integer")
    return {
        stream: int.from_bytes(
            hashlib.sha256(f"{episode_seed}:{stream}".encode("ascii")).digest()[:8],
            "big",
        )
        for stream in SHIP_RNG_STREAMS
    }


@dataclass(frozen=True)
class PointKernel:
    mean_cells: Vec2
    sigma_cells: float

    def __post_init__(self) -> None:
        _finite_vec2(self.mean_cells, "mean_cells")
        _finite_nonnegative(self.sigma_cells, "sigma_cells")
        object.__setattr__(self, "mean_cells", tuple(self.mean_cells))


@dataclass(frozen=True)
class BearingKernel:
    origin_cells: Vec2
    bearing_deg: float
    bearing_std_deg: float
    sigma_origin_cells: float
    range_decay_cells: float

    def __post_init__(self) -> None:
        _finite_vec2(self.origin_cells, "origin_cells")
        _finite_number(self.bearing_deg, "bearing_deg")
        if not 0.0 < self.bearing_std_deg < 90.0:
            raise ValueError("bearing_std_deg must be in (0, 90)")
        _finite_nonnegative(self.sigma_origin_cells, "sigma_origin_cells")
        if not math.isfinite(self.range_decay_cells) or self.range_decay_cells <= 0.0:
            raise ValueError("range_decay_cells must be positive")
        object.__setattr__(self, "origin_cells", tuple(self.origin_cells))


@dataclass(frozen=True)
class CovarianceKernel:
    mean_cells: Vec2
    covariance_cells2: tuple[tuple[float, float], tuple[float, float]]

    def __post_init__(self) -> None:
        _finite_vec2(self.mean_cells, "mean_cells")
        if len(self.covariance_cells2) != 2 or any(
            len(row) != 2 or not all(math.isfinite(value) for value in row)
            for row in self.covariance_cells2
        ):
            raise ValueError("covariance_cells2 must be a finite 2x2 matrix")
        object.__setattr__(self, "mean_cells", tuple(self.mean_cells))
        object.__setattr__(
            self,
            "covariance_cells2",
            tuple(tuple(row) for row in self.covariance_cells2),
        )


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: expected finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name}: expected finite number")
    return number


def _finite_nonnegative(value: object, name: str) -> float:
    number = _finite_number(value, name)
    if number < 0.0:
        raise ValueError(f"{name}: expected non-negative number")
    return number


def _finite_vec2(value: object, name: str) -> None:
    if value is None or len(value) != 2:
        raise ValueError(f"{name}: expected a pair")
    for item in value:
        _finite_number(item, name)


@dataclass(frozen=True)
class PassiveBearingObservation:
    observation_id: str
    sample_id: str
    emitter_track_id: str
    burst_id: str
    observed_at_min: float
    observer_uav_id: str
    observer_position_cells: Vec2
    bearing_deg: float
    bearing_std_deg: float

    def __post_init__(self) -> None:
        for name in ("observation_id", "sample_id", "emitter_track_id", "burst_id", "observer_uav_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name}: expected non-empty string")
        _finite_nonnegative(self.observed_at_min, "observed_at_min")
        _finite_vec2(self.observer_position_cells, "observer_position_cells")
        _finite_number(self.bearing_deg, "bearing_deg")
        if not 0.0 < self.bearing_std_deg < 90.0:
            raise ValueError("bearing_std_deg must be in (0, 90)")
        object.__setattr__(self, "observer_position_cells", tuple(self.observer_position_cells))


@dataclass(frozen=True)
class PassivePosition:
    position_id: str
    emitter_track_id: str
    burst_id: str
    sample_id: str
    observed_at_min: float
    position_cells: Vec2
    source_observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("position_id", "emitter_track_id", "burst_id", "sample_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name}: expected non-empty string")
        _finite_nonnegative(self.observed_at_min, "observed_at_min")
        _finite_vec2(self.position_cells, "position_cells")
        if len(self.source_observation_ids) < 2 or any(
            not isinstance(value, str) or not value for value in self.source_observation_ids
        ):
            raise ValueError("source_observation_ids must contain at least two IDs")
        object.__setattr__(self, "position_cells", tuple(self.position_cells))
        object.__setattr__(self, "source_observation_ids", tuple(self.source_observation_ids))


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    kind: EvidenceKind
    source_id: str
    contact_id: str | None
    observed_at_min: float
    expires_at_min: float
    strength: float
    spatial: PointKernel | BearingKernel | CovarianceKernel

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.source_id:
            raise ValueError("evidence requires evidence_id and source_id")
        _finite_nonnegative(self.observed_at_min, "observed_at_min")
        if not math.isfinite(self.expires_at_min) or self.expires_at_min < self.observed_at_min:
            raise ValueError("expires_at_min must not precede observed_at_min")
        if not math.isfinite(self.strength) or not 0.0 <= self.strength <= 1.0:
            raise ValueError("strength must be in [0, 1]")


@dataclass(frozen=True)
class InfoFieldDelta:
    previous_version: int
    version: int
    changed_bbox: Rect
    max_abs_value_delta: float
    value_changed: bool
    crossed_candidate_threshold: bool
    urgent: bool
    reason_codes: tuple[str, ...]
    cause_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.version <= self.previous_version:
            raise ValueError("information version must increase")
        if len(self.changed_bbox) != 4:
            raise ValueError("changed_bbox must be a half-open rectangle")
        if not math.isfinite(self.max_abs_value_delta) or self.max_abs_value_delta < 0.0:
            raise ValueError("max_abs_value_delta must be finite and non-negative")
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "cause_evidence_ids", tuple(self.cause_evidence_ids))


@dataclass(frozen=True)
class InformationSnapshot:
    version: int
    frozen_at_min: float
    info: tuple[tuple[float, ...], ...]
    strategic: tuple[tuple[float, ...], ...]
    timeliness: tuple[tuple[float, ...], ...]
    value: tuple[tuple[float, ...], ...]
    recent_deltas: tuple[InfoFieldDelta, ...]

    def __post_init__(self) -> None:
        _finite_nonnegative(self.frozen_at_min, "frozen_at_min")
        for name in ("info", "strategic", "timeliness", "value"):
            matrix = tuple(tuple(float(item) for item in row) for row in getattr(self, name))
            if any(not math.isfinite(item) for row in matrix for item in row):
                raise ValueError(f"{name}: expected finite matrix")
            object.__setattr__(self, name, matrix)
        object.__setattr__(self, "recent_deltas", tuple(self.recent_deltas))


@dataclass(frozen=True)
class EvasiveManeuverFact:
    fact_id: str
    evasion_episode_id: str
    episode_started: bool
    mmsi: str
    contact_id: str
    observed_at_min: float
    position_cells: Vec2
    covariance_cells2: tuple[tuple[float, float], tuple[float, float]]

    @property
    def kind(self) -> str:
        return "evasive_maneuver"

    @property
    def strength(self) -> float:
        return 1.0


@dataclass(frozen=True)
class AisUpdateState:
    mmsi: str
    enabled: bool
    revision: int
    changed_at_min: float
    reason: Literal["unclassified", "confirmed_type_i"]


@dataclass(frozen=True)
class VesselCommand:
    command_id: str
    episode_id: str
    operation: Literal["create", "delete", "set_ais"]
    vessel_id: str | None = None
    expected_revision: int | None = None
    vessel_class: VesselClass | None = None
    position_cells: Vec2 | None = None
    ais_enabled: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, str) or not self.command_id:
            raise ValueError("command_id must be non-empty")
        if not isinstance(self.episode_id, str) or not self.episode_id:
            raise ValueError("episode_id must be non-empty")
        if self.operation not in ("create", "delete", "set_ais"):
            raise ValueError("invalid vessel command operation")
        if self.ais_enabled is not None and type(self.ais_enabled) is not bool:
            raise ValueError("ais_enabled must be bool")


@dataclass(frozen=True)
class HandoffAttempt:
    handoff_id: str
    contact_id: str
    source_uav_id: str
    successor_uav_id: str | None
    evidence_id: str
    required_at_min: float
    assignment_deadline_min: float
    lock_deadline_min: float
    assignment_committed_at_min: float | None
    eo_lock_acquired_at_min: float | None
    state: Literal["required", "pending", "succeeded", "failed"]
    failure_reason: str | None


@dataclass(frozen=True)
class ContactAssessment:
    vessel_class: VesselClass
    class_confidence: float
    class_evidence_ids: tuple[str, ...]
    activity: ActivityState
    activity_confidence: float
    activity_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.vessel_class not in ("unknown", "type_i", "type_ii"):
            raise ValueError("invalid vessel_class")
        if self.activity not in ("unknown", "normal", "suspected_violation", "confirmed_violation"):
            raise ValueError("invalid activity")
        for name in ("class_confidence", "activity_confidence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        object.__setattr__(self, "class_evidence_ids", tuple(self.class_evidence_ids))
        object.__setattr__(self, "activity_evidence_ids", tuple(self.activity_evidence_ids))


@dataclass(frozen=True)
class SensorSnapshot:
    active_mode: Literal["standby", "switching_to_sar", "sar", "switching_to_eo", "eo"]
    transition_remaining_min: float
    passive_enabled: bool = True

    def __post_init__(self) -> None:
        if self.active_mode not in {
            "standby", "switching_to_sar", "sar", "switching_to_eo", "eo",
        }:
            raise ValueError("invalid active_mode")
        _finite_nonnegative(self.transition_remaining_min, "transition_remaining_min")
        if not self.passive_enabled:
            raise ValueError("passive sensing must remain enabled")


@dataclass(frozen=True)
class VisualDetection:
    """A sensor fix before association; physical vessel IDs never cross here."""

    sample_id: str
    observed_at_min: float
    source: Literal["sar", "eo"]
    source_id: str
    position_cells: Vec2
    velocity_cells_min: Vec2 | None
    position_uncertainty_cells: float
    observer_position_cells: Vec2
    measured_range_cells: float
    navigation_context: Literal["open_water", "near_land", "unknown"]

    def __post_init__(self) -> None:
        if not self.sample_id or not self.source_id or self.source not in ("sar", "eo"):
            raise ValueError("visual detection requires sample/source IDs and SAR or EO")
        for name in ("position_cells", "observer_position_cells", "velocity_cells_min"):
            value = getattr(self, name)
            if value is None and name == "velocity_cells_min":
                continue
            if value is None or len(value) != 2 or not all(math.isfinite(v) for v in value):
                raise ValueError(f"{name}: expected a finite position/velocity pair")
            object.__setattr__(self, name, tuple(value))
        for name in ("observed_at_min", "position_uncertainty_cells", "measured_range_cells"):
            value = getattr(self, name)
            if value is None or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name}: expected a finite nonnegative number")
        if self.navigation_context not in ("open_water", "near_land", "unknown"):
            raise ValueError("invalid navigation context")


@dataclass(frozen=True)
class ObservationSample:
    sample_id: str
    contact_id: str
    observed_at_min: float
    source: Literal["ais", "sar", "eo"]
    source_id: str
    position_cells: Vec2
    velocity_cells_min: Vec2 | None
    position_uncertainty_cells: float
    observer_position_cells: Vec2 | None
    measured_range_cells: float | None
    navigation_context: Literal["open_water", "near_land", "unknown"]


@dataclass(frozen=True)
class Assessment:
    assessment_id: str
    contact_id: str
    probe_id: str
    history_revision: int
    assessed_at_min: float
    vessel_class: VesselClass
    confidence: float
    evidence_sample_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    alternative_explanations: tuple[str, ...]
    model_call_id: str

@dataclass(frozen=True)
class ContactSnapshot:
    contact_id: str
    revision: int
    state: ContactState
    vessel_class: VesselClass
    ais_mmsi: str | None
    first_seen_min: float
    last_seen_min: float
    estimated_position_cells: Vec2
    estimated_velocity_cells_min: Vec2 | None
    uncertainty_cells: float
    assigned_uav_id: str | None
    active_probe_id: str | None
    last_assessment: Assessment | None
    cleared_at_min: float | None
    next_probe_not_before_min: float
    samples: tuple[ObservationSample, ...]
    _position_covariance_cells2: tuple[tuple[float, float], tuple[float, float]] | None = field(
        default=None, kw_only=True
    )
    class_confidence: float = field(default=0.0, kw_only=True)
    class_evidence_ids: tuple[str, ...] = field(default=(), kw_only=True)
    activity: ActivityState = field(default="unknown", kw_only=True)
    activity_confidence: float = field(default=0.0, kw_only=True)
    activity_evidence_ids: tuple[str, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if self.vessel_class not in ("unknown", "type_i", "type_ii"):
            raise ValueError("invalid vessel_class")
        for name in ("class_confidence", "activity_confidence"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")
            object.__setattr__(self, name, value)
        if self.activity not in (
            "unknown", "normal", "suspected_violation", "confirmed_violation",
        ):
            raise ValueError("invalid activity")
        object.__setattr__(self, "class_evidence_ids", tuple(self.class_evidence_ids))
        object.__setattr__(self, "activity_evidence_ids", tuple(self.activity_evidence_ids))

    @property
    def position_covariance_cells2(self):
        return self._position_covariance_cells2


@dataclass(frozen=True)
class ProbeSession:
    probe_id: str
    contact_id: str
    uav_id: str
    phase: Literal[
        "baseline", "closing", "near", "awaiting_assessment", "finished"
    ]
    started_at_min: float
    baseline_started_at_min: float | None
    phase_started_at_min: float
    baseline_sample_ids: tuple[str, ...]
    near_sample_ids: tuple[str, ...]
    close_exposure_min: float
    completed_reason: str | None
    # Immutable bookkeeping for incremental updates; public IDs remain the
    # evidence references. Defaults preserve existing construction/call sites.
    _phase_samples: tuple[ObservationSample, ...] = field(default=(), repr=False, kw_only=True)
    _phase_marker: tuple[str, float] | None = field(default=None, repr=False, kw_only=True)
    # Session-wide identity and per-(source, source_id) watermarks survive
    # phase completion and resets without retaining old observation payloads.
    _seen_sample_ids: frozenset[str] = field(default=frozenset(), repr=False, kw_only=True)
    _seen_sample_fixes: frozenset[tuple[str, str, float]] = field(
        default=frozenset(), repr=False, kw_only=True)
    _last_sample_keys: tuple[tuple[tuple[str, str], tuple[float, str]], ...] = field(
        default=(), repr=False, kw_only=True)
    # Keep completed evidence only until the next accepted timestamp, so an
    # independent sensor at the completion time still belongs to that phase.
    _completion_samples: tuple[ObservationSample, ...] = field(
        default=(), repr=False, kw_only=True)
    # A range reset can be undone by an independent in-band peer at the same
    # accepted timestamp. Keep only the prior phase, start and local evidence.
    _pending_reset: tuple[str, float, tuple[ObservationSample, ...]] | None = field(
        default=None, repr=False, kw_only=True)


@dataclass(frozen=True)
class TrajectoryFeatures:
    """Observed trajectory evidence (D §7.3), never an identity decision."""

    contact_id: str
    history_revision: int
    probe_id: str
    baseline_sample_ids: tuple[str, ...]
    near_sample_ids: tuple[str, ...]
    baseline_duration_min: float
    near_duration_min: float
    baseline_speed_mean_kn: float | None
    near_speed_mean_kn: float | None
    baseline_abs_turn_rate_deg_min: float | None
    near_abs_turn_rate_deg_min: float | None
    heading_change_deg: float | None
    min_observed_uav_distance_cells: float | None
    close_exposure_min: float
    near_land_fraction: float
    max_observation_gap_min: float
    sufficient_evidence: bool
    confounders: tuple[str, ...]


@dataclass(frozen=True)
class Intent:
    intent_id: str
    revision: int
    label: str
    bbox: Rect
    mode: Literal["search_priority", "maintain_freshness"]
    priority: Literal["high", "medium", "low"]
    weight: float
    created_at_min: float
    expires_at_min: float
    revisit_interval_min: float | None
    lifecycle: Literal["active", "expired", "cancelled"]


@dataclass(frozen=True)
class IntentStatus:
    intent_id: str
    revision: int
    evaluated_at_min: float
    searchable_cells: int
    scanned_cells: int
    unseen_cells: int
    fresh_cells: int
    coverage_ratio: float
    freshness_ratio: float
    max_scan_age_min: float | None
    assigned_task_ids: tuple[str, ...]
    unmet_reason: str | None


@dataclass(frozen=True)
class TaskCandidate:
    task_id: str
    kind: Literal[
        "search", "direction_search", "investigation", "probe", "track"
    ]
    bbox: Rect | None
    contact_id: str | None
    intent_ids: tuple[str, ...]
    feasible_uav_ids: tuple[str, ...]
    eligible_since_min: float
    priority: Literal["high", "medium", "low"]
    estimated_duration_min: float
    utility: float
    expected_information_gain: float
    _information_version: int = field(default=0, repr=False, kw_only=True)

    @property
    def information_version(self) -> int:
        return self._information_version

    @property
    def cells(self) -> tuple[tuple[int, int], ...]:
        if self.bbox is None:
            return ()
        return tuple(
            (col, row)
            for col in range(self.bbox[0], self.bbox[2])
            for row in range(self.bbox[1], self.bbox[3])
        )


@dataclass(frozen=True)
class UavResource:
    uav_id: str
    position_cells: Vec2
    heading_rad: float
    speed_cells_min: float
    remaining_range_cells: float
    operation: str
    current_task_id: str | None
    generation: int
    last_reassigned_at_min: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "position_cells", tuple(self.position_cells))


@dataclass(frozen=True)
class FeasibleEdge:
    task_id: str
    uav_id: str
    transit_time_min: float
    mission_range_cells: float
    return_range_cells: float
    reserve_range_cells: float
    route_cache_key: str


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    kind: Literal[
        "search", "direction_search", "investigation", "probe", "track"
    ]
    status: Literal[
        "candidate", "approved", "executing", "completed", "cancelled", "blocked"
    ]
    bbox: Rect | None
    contact_id: str | None
    intent_ids: tuple[str, ...]
    assigned_uav_id: str | None
    approved_call_id: str | None
    created_at_min: float
    started_at_min: float | None
    finished_at_min: float | None
    release_reason: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_ids", tuple(self.intent_ids))


@dataclass(frozen=True)
class ZoneCoverageRequirement:
    zone_id: str
    required_search_count: int
    representative_task_ids: tuple[str, ...]
    must_service_task_ids: tuple[str, ...]
    infeasible_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.zone_id, str) or not self.zone_id:
            raise ValueError("zone_id must be non-empty")
        count = self.required_search_count
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("required_search_count must be a non-negative integer")
        for name in ("representative_task_ids", "must_service_task_ids"):
            ids = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
                raise ValueError(f"{name} must contain unique non-empty strings")
            object.__setattr__(self, name, ids)
        if not set(self.must_service_task_ids) <= set(self.representative_task_ids):
            raise ValueError("must_service_task_ids must be representatives")
        if self.infeasible_reason is not None and (
            not isinstance(self.infeasible_reason, str) or not self.infeasible_reason
        ):
            raise ValueError("infeasible_reason must be non-empty when provided")


@dataclass(frozen=True)
class CoverageConstraint:
    """Exact ordinary-search budget and feasible spatial obligations."""

    desired_search_count: int
    active_search_count: int
    required_new_search_count: int
    representative_task_ids: tuple[str, ...]
    must_service_task_ids: tuple[str, ...]
    infeasible_reason: str | None = None
    zone_requirements: tuple[ZoneCoverageRequirement, ...] = ()
    zone_infeasible: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "desired_search_count",
            "active_search_count",
            "required_new_search_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.required_new_search_count > self.desired_search_count:
            raise ValueError("required_new_search_count exceeds desired_search_count")
        representatives = tuple(self.representative_task_ids)
        must_service = tuple(self.must_service_task_ids)
        if any(not isinstance(item, str) or not item for item in representatives):
            raise ValueError("representative_task_ids must contain non-empty strings")
        if any(item not in representatives for item in must_service):
            raise ValueError("must_service_task_ids must be representatives")
        if self.infeasible_reason is not None and (
            not isinstance(self.infeasible_reason, str) or not self.infeasible_reason
        ):
            raise ValueError("infeasible_reason must be non-empty when provided")
        object.__setattr__(self, "representative_task_ids", representatives)
        object.__setattr__(self, "must_service_task_ids", must_service)
        zones = tuple(self.zone_requirements)
        if any(not isinstance(zone, ZoneCoverageRequirement) for zone in zones):
            raise ValueError("zone_requirements must contain ZoneCoverageRequirement")
        if len({zone.zone_id for zone in zones}) != len(zones):
            raise ValueError("zone_requirements must have unique zone IDs")
        if any(not set(zone.representative_task_ids) <= set(representatives) for zone in zones):
            raise ValueError("zone representatives must be global representatives")
        object.__setattr__(self, "zone_requirements", zones)
        object.__setattr__(self, "zone_infeasible", tuple(tuple(item) for item in self.zone_infeasible))


@dataclass(frozen=True)
class MissionSnapshot:
    snapshot_id: str
    sim_time_min: float
    candidates: tuple[TaskCandidate, ...]
    available_uav_ids: tuple[str, ...]
    preemptible_uav_ids: tuple[str, ...]
    uav_generations: tuple[tuple[str, int], ...]
    resources: tuple[UavResource, ...]
    feasible_edges: tuple[FeasibleEdge, ...]
    active_tasks: tuple[TaskRecord, ...]
    contacts: tuple[ContactSnapshot, ...]
    intents: tuple[Intent, ...]
    intent_statuses: tuple[IntentStatus, ...]
    memory_version: str
    planning_map_version: int
    reviewer_summary: str
    _information_version: int = field(default=0, repr=False, kw_only=True)
    prompt_task_ids: tuple[str, ...] = field(default=(), kw_only=True)
    prompt_sources: tuple[tuple[str, str], ...] = field(default=(), kw_only=True)
    coverage_constraint: CoverageConstraint | None = field(default=None, kw_only=True)
    coverage_summary: dict | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.coverage_summary is not None:
            if not isinstance(self.coverage_summary, dict):
                raise ValueError("coverage_summary must be a dict or None")
            object.__setattr__(self, "coverage_summary", deepcopy(self.coverage_summary))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "available_uav_ids", tuple(self.available_uav_ids))
        object.__setattr__(self, "preemptible_uav_ids", tuple(self.preemptible_uav_ids))
        object.__setattr__(self, "uav_generations", tuple(tuple(item) for item in self.uav_generations))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "feasible_edges", tuple(self.feasible_edges))
        object.__setattr__(self, "active_tasks", tuple(self.active_tasks))
        object.__setattr__(self, "contacts", tuple(self.contacts))
        object.__setattr__(self, "intents", tuple(self.intents))
        object.__setattr__(self, "intent_statuses", tuple(self.intent_statuses))
        object.__setattr__(self, "prompt_task_ids", tuple(self.prompt_task_ids))
        object.__setattr__(
            self,
            "prompt_sources",
            tuple(tuple(item) for item in self.prompt_sources),
        )
        if self.coverage_constraint is not None and not isinstance(
            self.coverage_constraint, CoverageConstraint
        ):
            raise ValueError("coverage_constraint must be CoverageConstraint or None")

    @property
    def information_version(self) -> int:
        return self._information_version


@dataclass(frozen=True)
class Assignment:
    task_id: str
    uav_id: str
    expected_generation: int
    previous_task_id: str | None


@dataclass(frozen=True)
class AssignmentBatch:
    snapshot_id: str
    assignments: tuple[Assignment, ...]
    selection_call_id: str
    _information_version: int = field(default=0, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignments", tuple(self.assignments))

    @property
    def information_version(self) -> int:
        return self._information_version


@dataclass(frozen=True)
class IntentCommand:
    """A versioned operator mutation waiting for the simulation thread."""

    command_id: str
    episode_id: str
    operation: Literal["create", "update", "cancel"]
    intent_id: str | None
    expected_revision: int | None
    payload: dict

    def __post_init__(self) -> None:
        if not isinstance(self.payload, dict):
            raise TypeError("IntentCommand.payload must be a mapping")
        # Do not retain a caller-owned mutable mapping in a frozen contract.
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True)
class CommandResult:
    """Published status for an intent or runtime command."""

    command_id: str
    status: Literal["queued", "applied", "rejected"]
    intent: Intent | None
    error_code: str | None


@dataclass(frozen=True)
class RuntimeCommand:
    """A simulation-thread command independent from intent mutations."""

    command_id: str
    episode_id: str
    operation: Literal["retry", "abort"]


@dataclass(frozen=True)
class MissionSelection:
    schema_version: str
    snapshot_id: str
    selected_task_ids: tuple[str, ...]
    preempt_uav_ids: tuple[str, ...]
    defer_reason: str | None
    notes: str
    _information_version: int = field(default=0, repr=False, kw_only=True)

    @property
    def information_version(self) -> int:
        return self._information_version


@dataclass(frozen=True)
class RedMotionParameters:
    ship_id: str
    heading_offset_deg: float
    speed_kn: float
    zigzag_heading_deg: float
    zigzag_period_min: float
    phase_deg: float


@dataclass(frozen=True)
class RedPlan:
    schema_version: str
    snapshot_id: str
    valid_for_min: float
    commands: tuple[RedMotionParameters, ...]
    notes: str


# New planning terminology retains the immutable legacy snapshot type while
# allowing callers to migrate without duplicating the contract.
PlanningSnapshot = MissionSnapshot


__all__ = [
    "Assessment",
    "ActivityState",
    "AisUpdateState",
    "Assignment",
    "AssignmentBatch",
    "BearingKernel",
    "ContactSnapshot",
    "ContactState",
    "ContactAssessment",
    "CommandResult",
    "CovarianceKernel",
    "CoverageConstraint",
    "ZoneCoverageRequirement",
    "EvasiveManeuverFact",
    "EvidenceKind",
    "EvidenceRecord",
    "FeasibleEdge",
    "HandoffAttempt",
    "InfoFieldDelta",
    "InformationSnapshot",
    "Intent",
    "IntentCommand",
    "IntentStatus",
    "MissionSelection",
    "MissionSnapshot",
    "ObservationSample",
    "PassiveBearingObservation",
    "PassivePosition",
    "PlanningSnapshot",
    "PointKernel",
    "ProbeSession",
    "Rect",
    "RedMotionParameters",
    "RedPlan",
    "RuntimeCommand",
    "SensorSnapshot",
    "SHIP_RNG_STREAMS",
    "TaskCandidate",
    "TaskRecord",
    "TrajectoryFeatures",
    "UavResource",
    "VesselClass",
    "VesselCommand",
    "Vec2",
    "VisualDetection",
    "ship_rng_manifest",
]

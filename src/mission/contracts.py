"""Immutable public contracts for the mixed maritime mission domain."""

from dataclasses import dataclass
import hashlib
from typing import Literal


Vec2 = tuple[float, float]
Rect = tuple[int, int, int, int]
Identity = Literal["unknown", "target", "civilian"]
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

SHIP_RNG_STREAMS = ("ship_identity", "ship_ais_mode")


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
    identity: Identity
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
    identity: Identity
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
class TaskCandidate:
    task_id: str
    kind: Literal["search", "probe", "track"]
    bbox: Rect | None
    contact_id: str | None
    intent_ids: tuple[str, ...]
    feasible_uav_ids: tuple[str, ...]
    eligible_since_min: float
    priority: Literal["high", "medium", "low"]
    estimated_duration_min: float
    utility: float
    expected_information_gain: float


@dataclass(frozen=True)
class MissionSelection:
    schema_version: str
    snapshot_id: str
    selected_task_ids: tuple[str, ...]
    preempt_uav_ids: tuple[str, ...]
    defer_reason: str | None
    notes: str


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


__all__ = [
    "Assessment",
    "ContactSnapshot",
    "ContactState",
    "Identity",
    "Intent",
    "MissionSelection",
    "ObservationSample",
    "ProbeSession",
    "Rect",
    "RedMotionParameters",
    "RedPlan",
    "SHIP_RNG_STREAMS",
    "TaskCandidate",
    "Vec2",
    "ship_rng_manifest",
]

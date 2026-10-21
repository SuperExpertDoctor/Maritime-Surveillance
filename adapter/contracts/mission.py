"""Adapter DTOs for the scheduling boundary.

These pydantic models are the only shapes allowed to cross between the
adapter layer and the internal ``src.*`` objects. Internal code hands the
adapter its own dataclasses; the adapter publishes these DTOs to the PI
worker and to websocket clients.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SELECTION_SCHEMA = "mission-selection/v1"
LEGACY_PLAN_SCHEMA = "legacy-mission-plan/v1"


class TaskCandidateDTO(BaseModel):
    """Serializable mirror of ``src.mission.contracts.TaskCandidate``."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    kind: Literal["search", "direction_search", "investigation", "probe", "track"]
    bbox: tuple[int, int, int, int] | None = None
    contact_id: str | None = None
    intent_ids: tuple[str, ...] = ()
    feasible_uav_ids: tuple[str, ...] = ()
    eligible_since_min: float = 0.0
    priority: Literal["high", "medium", "low"] = "medium"
    estimated_duration_min: float = 0.0
    utility: float = 0.0
    expected_information_gain: float = 0.0


class UavResourceDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    uav_id: str
    position_cells: tuple[float, float] = (0.0, 0.0)
    heading_rad: float = 0.0
    speed_cells_min: float = 0.0
    remaining_range_cells: float = 0.0
    operation: str = ""
    current_task_id: str | None = None
    generation: int = 0
    last_reassigned_at_min: float = 0.0


class FeasibleEdgeDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    uav_id: str
    transit_time_min: float = 0.0
    mission_range_cells: float = 0.0
    return_range_cells: float = 0.0
    reserve_range_cells: float = 0.0


class ActiveTaskDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    kind: str
    status: str
    bbox: tuple[int, int, int, int] | None = None
    contact_id: str | None = None
    intent_ids: tuple[str, ...] = ()
    assigned_uav_id: str | None = None
    approved_call_id: str | None = None
    created_at_min: float = 0.0


class ContactDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    contact_id: str
    state: str = ""
    vessel_class: str = ""
    estimated_position_cells: tuple[float, float] | None = None
    assigned_uav_id: str | None = None
    last_seen_min: float = 0.0
    class_confidence: float = 0.0
    activity: str = "unknown"


class IntentDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_id: str
    kind: str = ""
    bbox: tuple[int, int, int, int] | None = None
    status: str = ""
    revision: int = 0


class MissionSnapshotDTO(BaseModel):
    """The public scheduling snapshot served to the PI worker."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    episode_id: str = ""
    sim_time_min: float = 0.0
    information_version: int = 0
    candidates: tuple[TaskCandidateDTO, ...] = ()
    resources: tuple[UavResourceDTO, ...] = ()
    available_uav_ids: tuple[str, ...] = ()
    preemptible_uav_ids: tuple[str, ...] = ()
    uav_generations: tuple[tuple[str, int], ...] = ()
    feasible_edges: tuple[FeasibleEdgeDTO, ...] = ()
    active_tasks: tuple[ActiveTaskDTO, ...] = ()
    contacts: tuple[ContactDTO, ...] = ()
    intents: tuple[IntentDTO, ...] = ()
    memory_version: str = ""
    reviewer_summary: str = ""
    coverage_summary: dict | None = None


class SelectionPayloadDTO(BaseModel):
    """Wire contract ``mission-selection/v1`` submitted by the worker."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["mission-selection/v1"] = SELECTION_SCHEMA
    snapshot_id: str
    selected_task_ids: tuple[str, ...] = ()
    preempt_uav_ids: tuple[str, ...] = ()
    defer_reason: str | None = None
    notes: str = ""
    information_version: int = 0


class PlanSubmissionDTO(BaseModel):
    """Worker-submitted plan regions for the legacy scheduler kind."""

    model_config = ConfigDict(frozen=True)

    search_regions: tuple[dict, ...] = ()
    notes: str = ""
    decision_reason: str = ""


class AssignmentDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    uav_id: str
    expected_generation: int = 0
    previous_task_id: str | None = None


class ValidationIssueDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""
    field: str | None = None


class ValidationResultDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    errors: tuple[str, ...] = ()
    matched: tuple[AssignmentDTO, ...] = ()

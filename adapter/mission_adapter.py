"""Internal ``src.*`` objects -> adapter DTOs.

This module is the only place allowed to read internal attributes for
publication; nothing inside ``src`` imports from it.
"""

from __future__ import annotations

from adapter.contracts import (
    ActiveTaskDTO,
    ContactDTO,
    FeasibleEdgeDTO,
    IntentDTO,
    MissionSnapshotDTO,
    TaskCandidateDTO,
    UavResourceDTO,
)


def _bbox(value) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    cells = list(value) if not isinstance(value, tuple) else value
    return tuple(int(v) for v in cells)


def candidate_dto(candidate) -> TaskCandidateDTO:
    return TaskCandidateDTO(
        task_id=candidate.task_id,
        kind=candidate.kind,
        bbox=_bbox(candidate.bbox),
        contact_id=candidate.contact_id,
        intent_ids=tuple(candidate.intent_ids),
        feasible_uav_ids=tuple(candidate.feasible_uav_ids),
        eligible_since_min=float(candidate.eligible_since_min),
        priority=candidate.priority,
        estimated_duration_min=float(candidate.estimated_duration_min),
        utility=float(candidate.utility),
        expected_information_gain=float(candidate.expected_information_gain),
    )


def resource_dto(resource) -> UavResourceDTO:
    return UavResourceDTO(
        uav_id=resource.uav_id,
        position_cells=tuple(float(v) for v in resource.position_cells),
        heading_rad=float(resource.heading_rad),
        speed_cells_min=float(resource.speed_cells_min),
        remaining_range_cells=float(resource.remaining_range_cells),
        operation=str(resource.operation),
        current_task_id=resource.current_task_id,
        generation=int(resource.generation),
        last_reassigned_at_min=float(resource.last_reassigned_at_min),
    )


def edge_dto(edge) -> FeasibleEdgeDTO:
    return FeasibleEdgeDTO(
        task_id=edge.task_id,
        uav_id=edge.uav_id,
        transit_time_min=float(edge.transit_time_min),
        mission_range_cells=float(edge.mission_range_cells),
        return_range_cells=float(edge.return_range_cells),
        reserve_range_cells=float(edge.reserve_range_cells),
    )


def task_dto(task) -> ActiveTaskDTO:
    return ActiveTaskDTO(
        task_id=task.task_id,
        kind=str(task.kind),
        status=str(task.status),
        bbox=_bbox(task.bbox),
        contact_id=task.contact_id,
        intent_ids=tuple(task.intent_ids),
        assigned_uav_id=task.assigned_uav_id,
        approved_call_id=task.approved_call_id,
        created_at_min=float(task.created_at_min),
    )


def contact_dto(contact) -> ContactDTO:
    position = getattr(contact, "estimated_position_cells", None)
    return ContactDTO(
        contact_id=contact.contact_id,
        state=str(getattr(contact, "state", "")),
        vessel_class=str(getattr(contact, "vessel_class", "")),
        estimated_position_cells=(
            tuple(float(v) for v in position) if position is not None else None
        ),
        assigned_uav_id=getattr(contact, "assigned_uav_id", None),
        last_seen_min=float(getattr(contact, "last_seen_min", 0.0)),
        class_confidence=float(getattr(contact, "class_confidence", 0.0)),
        activity=str(getattr(contact, "activity", "unknown")),
    )


def intent_dto(intent) -> IntentDTO:
    return IntentDTO(
        intent_id=getattr(intent, "intent_id", ""),
        kind=str(getattr(intent, "kind", getattr(intent, "task_kind", ""))),
        bbox=_bbox(getattr(intent, "bbox", None)),
        status=str(getattr(intent, "status", "")),
        revision=int(getattr(intent, "revision", 0)),
    )


def snapshot_dto(snapshot, *, episode_id: str = "") -> MissionSnapshotDTO:
    """Convert an internal MissionSnapshot into its public DTO."""
    return MissionSnapshotDTO(
        snapshot_id=snapshot.snapshot_id,
        episode_id=episode_id,
        sim_time_min=float(snapshot.sim_time_min),
        information_version=int(snapshot.information_version),
        candidates=tuple(candidate_dto(item) for item in snapshot.candidates),
        resources=tuple(resource_dto(item) for item in snapshot.resources),
        available_uav_ids=tuple(snapshot.available_uav_ids),
        preemptible_uav_ids=tuple(snapshot.preemptible_uav_ids),
        uav_generations=tuple(
            (uav_id, int(gen)) for uav_id, gen in snapshot.uav_generations
        ),
        feasible_edges=tuple(edge_dto(edge) for edge in snapshot.feasible_edges),
        active_tasks=tuple(task_dto(task) for task in snapshot.active_tasks),
        contacts=tuple(contact_dto(item) for item in snapshot.contacts),
        intents=tuple(intent_dto(item) for item in snapshot.intents),
        memory_version=str(snapshot.memory_version),
        reviewer_summary=str(snapshot.reviewer_summary or ""),
        coverage_summary=snapshot.coverage_summary,
    )

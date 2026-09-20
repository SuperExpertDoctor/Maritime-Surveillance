"""Strict mission selection validation and exact feasible-task matching."""
from __future__ import annotations

import math
import os
import time
import json
import logging
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from functools import lru_cache

from src.mission.contracts import (
    Assignment,
    AssignmentBatch,
    ContactSnapshot,
    FeasibleEdge,
    MissionSelection,
    MissionSnapshot,
    TaskCandidate,
    TaskRecord,
    UavResource,
)
from src.mission.strategy_memory import StrategyMemoryStore
from src.mission.trajectory_features import select_keypoints
from src.mission.prompt_window import PromptWindow


SELECTION_SCHEMA = "mission-selection/v1"
DEFAULT_REASSIGNMENT_COOLDOWN_MIN = 5.0
DEFAULT_PLANNING_DEADLINE_SECONDS = 2.0
DEFAULT_POSTPROCESS_RESERVE_SECONDS = 0.2
_SELECTION_FIELDS = {
    "schema_version",
    "snapshot_id",
    "selected_task_ids",
    "preempt_uav_ids",
    "defer_reason",
    "notes",
}
_OPTIONAL_SELECTION_FIELDS = {"information_version"}
_PROTECTED_OPERATIONS = {
    "return",
    "returning",
    "refuel",
    "refueling",
    "probe",
    "track",
    "tracking",
    "safety",
    "safety_takeover",
}
_ORDINARY_SEARCH_OPERATIONS = {
    "coverage",
    "search",
    "searching",
    "transit",
}
_ACTIVE_RECORD_STATUSES = {"approved", "executing"}
_SEARCH_TASK_KINDS = frozenset({"search", "direction_search", "investigation"})
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, eq=False)
class PairingResult:
    """Post-selection pairing result with explicit validation state."""

    assignments: tuple[Assignment, ...] = ()
    errors: tuple[str, ...] = ()
    is_valid: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignments", tuple(self.assignments))
        object.__setattr__(self, "errors", tuple(self.errors))
        if not isinstance(self.is_valid, bool):
            raise TypeError("is_valid must be a bool")

    def __iter__(self):
        return iter(self.assignments)

    def __len__(self) -> int:
        return len(self.assignments)

    def __getitem__(self, index):
        return self.assignments[index]

    def __eq__(self, other) -> bool:
        if isinstance(other, PairingResult):
            return (
                self.assignments == other.assignments
                and self.errors == other.errors
                and self.is_valid == other.is_valid
            )
        if isinstance(other, (tuple, list)):
            return self.assignments == tuple(other)
        return NotImplemented


def _jsonable(value):
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(_jsonable(item) for item in value)
    return value


def _candidate_payload(candidate: TaskCandidate) -> dict:
    payload = _jsonable(candidate)
    if "_information_version" in payload:
        payload["information_version"] = payload.pop("_information_version")
    return payload


def _active_task_payload(task: TaskRecord) -> dict:
    return _jsonable(task)


def _assessment_payload(assessment) -> dict | None:
    if assessment is None:
        return None
    return {
        "assessment_id": assessment.assessment_id,
        "history_revision": assessment.history_revision,
        "assessed_at_min": assessment.assessed_at_min,
        "vessel_class": assessment.vessel_class,
        "confidence": assessment.confidence,
        "evidence_sample_ids": list(assessment.evidence_sample_ids),
        "reasons": list(assessment.reasons),
        "alternative_explanations": list(assessment.alternative_explanations),
    }


def _contact_payload(contact: ContactSnapshot) -> dict:
    """Serialize only public contact evidence needed for mission selection."""
    def sample_payload(sample) -> dict:
        return {
            "sample_id": sample.sample_id,
            "observed_at_min": sample.observed_at_min,
            "source": sample.source,
            "source_id": sample.source_id,
            "position_cells": sample.position_cells,
            "velocity_cells_min": sample.velocity_cells_min,
            "measured_range_cells": sample.measured_range_cells,
            "navigation_context": sample.navigation_context,
        }

    return {
        "contact_id": contact.contact_id,
        "revision": contact.revision,
        "state": contact.state,
        "vessel_class": contact.vessel_class,
        "ais_mmsi": contact.ais_mmsi,
        "first_seen_min": contact.first_seen_min,
        "last_seen_min": contact.last_seen_min,
        "estimated_position_cells": contact.estimated_position_cells,
        "estimated_velocity_cells_min": contact.estimated_velocity_cells_min,
        "uncertainty_cells": contact.uncertainty_cells,
        "assigned_uav_id": contact.assigned_uav_id,
        "active_probe_id": contact.active_probe_id,
        "last_assessment": _assessment_payload(contact.last_assessment),
        "cleared_at_min": contact.cleared_at_min,
        "next_probe_not_before_min": contact.next_probe_not_before_min,
        "class_confidence": contact.class_confidence,
        "class_evidence_ids": list(contact.class_evidence_ids),
        "activity": contact.activity,
        "activity_confidence": contact.activity_confidence,
        "activity_evidence_ids": list(contact.activity_evidence_ids),
        "samples": [
            sample_payload(sample)
            for sample in select_keypoints(contact.samples, 12)
        ],
    }


def _prompt_budget_field(payload: dict) -> str:
    snapshot = payload.get("snapshot", {})
    field_sizes = []
    for field, value in snapshot.items():
        try:
            size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            size = 0
        field_sizes.append((size, field))
    return max(field_sizes, default=(0, "snapshot"))[1]


def _selection_object(payload) -> tuple[MissionSelection | None, list[str]]:
    if isinstance(payload, MissionSelection):
        return payload, []
    if not isinstance(payload, dict):
        return None, ["selection must be an object"]
    errors: list[str] = []
    unknown = set(payload) - _SELECTION_FIELDS - _OPTIONAL_SELECTION_FIELDS
    missing = _SELECTION_FIELDS - set(payload)
    if unknown:
        errors.append(f"unknown_selection_fields: {sorted(unknown)}")
    if missing:
        errors.append(f"missing_selection_fields: {sorted(missing)}")
    if errors:
        return None, errors

    schema_version = payload["schema_version"]
    if schema_version != SELECTION_SCHEMA:
        errors.append("invalid_schema_version")
    snapshot_id = payload["snapshot_id"]
    if not isinstance(snapshot_id, str) or not snapshot_id:
        errors.append("invalid_snapshot_id")
    selected = payload["selected_task_ids"]
    preempt = payload["preempt_uav_ids"]
    for name, value in (("selected_task_ids", selected), ("preempt_uav_ids", preempt)):
        if not isinstance(value, (list, tuple)):
            errors.append(f"{name}_must_be_array")
        elif any(not isinstance(item, str) or not item for item in value):
            errors.append(f"{name}_must_contain_nonempty_strings")
    defer_reason = payload["defer_reason"]
    if defer_reason is not None and (
        not isinstance(defer_reason, str) or not defer_reason.strip()
    ):
        errors.append("invalid_defer_reason")
    if not isinstance(payload["notes"], str):
        errors.append("notes_must_be_string")
    elif len(payload["notes"]) > 160:
        errors.append("notes_too_long")
    if errors:
        return None, errors
    information_version = payload.get("information_version", 0)
    if (isinstance(information_version, bool)
            or not isinstance(information_version, int)
            or information_version < 0):
        # The field is optional, so omission still means "no version supplied".
        # A present but malformed value must be rejected rather than coerced,
        # otherwise a wrong version could pass the staleness check.
        errors.append("invalid_information_version")
        return None, errors
    return MissionSelection(
        schema_version,
        snapshot_id,
        tuple(selected),
        tuple(preempt),
        defer_reason,
        payload["notes"],
        _information_version=information_version,
    ), []


def _task_maps(snapshot: MissionSnapshot):
    active = {
        task.task_id: task
        for task in snapshot.active_tasks
        if task.status in _ACTIVE_RECORD_STATUSES
    }
    # An active record is the execution source of truth when a stable task ID
    # appears in both streams. Terminal audit records must not shadow a fresh
    # candidate with the same stable geometry ID.
    candidates = {
        task.task_id: task
        for task in snapshot.candidates
        if task.task_id not in active
    }
    return candidates, active


def _resource_maps(snapshot: MissionSnapshot):
    return {resource.uav_id: resource for resource in snapshot.resources}


def _operation(resource: UavResource) -> str:
    return str(resource.operation).lower()


def _protected(resource: UavResource) -> bool:
    return _operation(resource) in _PROTECTED_OPERATIONS


def _ordinary_search(resource: UavResource, active_tasks: dict[str, TaskRecord]) -> bool:
    operation = _operation(resource)
    if operation not in _ORDINARY_SEARCH_OPERATIONS:
        return False
    if not resource.current_task_id:
        return False
    record = active_tasks.get(resource.current_task_id)
    return record is None or (
        record.kind in _SEARCH_TASK_KINDS and record.status in _ACTIVE_RECORD_STATUSES
    )


def _cooldown_error(resource: UavResource, snapshot: MissionSnapshot, cooldown: float) -> bool:
    elapsed = snapshot.sim_time_min - resource.last_reassigned_at_min
    return elapsed < cooldown - 1e-9


def _is_available(resource: UavResource, snapshot: MissionSnapshot) -> bool:
    if resource.uav_id not in snapshot.available_uav_ids:
        return False
    if _protected(resource):
        return False
    # A malformed snapshot must not turn an already assigned ordinary sortie
    # into an idle resource merely because its ID appears in the list.
    return resource.current_task_id is None or _operation(resource) in {"idle", "holding"}


def _is_preemptible(
    resource: UavResource,
    snapshot: MissionSnapshot,
    selection: MissionSelection,
    active_tasks: dict[str, TaskRecord],
    cooldown: float,
) -> bool:
    return (
        resource.uav_id in snapshot.preemptible_uav_ids
        and resource.uav_id in selection.preempt_uav_ids
        and not _protected(resource)
        and _ordinary_search(resource, active_tasks)
        and not _cooldown_error(resource, snapshot, cooldown)
    )


def _task_kind(task_id: str, candidates: dict[str, TaskCandidate], active: dict[str, TaskRecord]):
    task = active.get(task_id) or candidates.get(task_id)
    return task.kind if task is not None else None


def _edge_usable(
    edge: FeasibleEdge,
    task_id: str,
    candidates: dict[str, TaskCandidate],
    active: dict[str, TaskRecord],
    resources: dict[str, UavResource],
    snapshot: MissionSnapshot,
    selection: MissionSelection,
    cooldown: float,
    *,
    allow_probe_preempt_search: bool = True,
    allow_intent_preempt_search: bool = False,
) -> bool:
    resource = resources.get(edge.uav_id)
    if resource is None:
        return False
    task = active.get(task_id) or candidates.get(task_id)
    if task is None:
        return False
    if task_id in active and active[task_id].status != "approved":
        return False
    feasible_uav_ids = getattr(task, "feasible_uav_ids", ())
    if feasible_uav_ids and edge.uav_id not in feasible_uav_ids:
        return False
    values = (
        edge.transit_time_min,
        edge.mission_range_cells,
        edge.return_range_cells,
        edge.reserve_range_cells,
        resource.speed_cells_min,
        resource.remaining_range_cells,
    )
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in values):
        return False
    if any(value < 0 for value in values):
        return False
    required_range = (
        edge.transit_time_min * resource.speed_cells_min
        + edge.mission_range_cells
        + edge.return_range_cells
        + edge.reserve_range_cells
    )
    if required_range > resource.remaining_range_cells + 1e-9:
        return False
    active_tasks = {record.task_id: record for record in snapshot.active_tasks}
    is_available = _is_available(resource, snapshot)
    is_preemptible = _is_preemptible(
        resource, snapshot, selection, active_tasks, cooldown,
    )
    if not (is_available or is_preemptible):
        return False
    kind = task.kind
    if resource.uav_id in selection.preempt_uav_ids:
        # A declared preemption must actually launch a new contact task.
        if kind == "probe" and not allow_probe_preempt_search:
            return False
        if kind in _SEARCH_TASK_KINDS and not (
            allow_intent_preempt_search and bool(task.intent_ids)
        ):
            return False
        if kind not in {"probe", "track", *_SEARCH_TASK_KINDS}:
            return False
        if not is_preemptible:
            return False
    elif is_preemptible:
        # Busy ordinary search is only interrupted when explicitly declared.
        return False
    return True


def _edge_options(
    selection: MissionSelection,
    snapshot: MissionSnapshot,
    cooldown: float,
    *,
    allow_probe_preempt_search: bool = True,
    allow_intent_preempt_search: bool = False,
) -> dict[str, tuple[FeasibleEdge, ...]]:
    candidates, active = _task_maps(snapshot)
    resources = _resource_maps(snapshot)
    by_task: dict[str, list[FeasibleEdge]] = {
        task_id: [] for task_id in selection.selected_task_ids
    }
    for edge in snapshot.feasible_edges:
        if edge.task_id not in by_task:
            continue
        if _edge_usable(
            edge, edge.task_id, candidates, active, resources,
            snapshot,
            selection,
            cooldown,
            allow_probe_preempt_search=allow_probe_preempt_search,
            allow_intent_preempt_search=allow_intent_preempt_search,
        ):
            by_task[edge.task_id].append(edge)
    return {
        task_id: tuple(sorted(options, key=lambda edge: (edge.transit_time_min, edge.uav_id)))
        for task_id, options in by_task.items()
    }


def _maximum_matching(
    task_ids: tuple[str, ...], options: dict[str, tuple[FeasibleEdge, ...]]
) -> dict[str, FeasibleEdge]:
    """Return a maximum-cardinality matching using augmenting paths."""
    assigned_uav: dict[str, tuple[str, FeasibleEdge]] = {}

    def visit(task_id: str, seen: set[str]) -> bool:
        for edge in options.get(task_id, ()):
            if edge.uav_id in seen:
                continue
            seen.add(edge.uav_id)
            current = assigned_uav.get(edge.uav_id)
            if current is None or visit(current[0], seen):
                assigned_uav[edge.uav_id] = (task_id, edge)
                return True
        return False

    for task_id in task_ids:
        visit(task_id, set())
    return {task_id: edge for task_id, edge in assigned_uav.values()}


def _minimum_cost_matching(
    task_ids: tuple[str, ...], options: dict[str, tuple[FeasibleEdge, ...]],
    resources: dict[str, UavResource],
    required_preempt_uav_ids: frozenset[str] = frozenset(),
) -> dict[str, FeasibleEdge] | None:
    """Find an exact minimum-cost matching without relying on greedy fallback."""
    if not task_ids:
        return {}
    if len(task_ids) > len(resources):
        return None
    # Branch on the most constrained task first, then memoize each used-resource
    # state.  The result is projected back into model selection order so task
    # priority remains visible to callers.
    order = tuple(sorted(
        task_ids,
        key=lambda task_id: (len(options.get(task_id, ())), task_ids.index(task_id)),
    ))
    resource_ids = tuple(sorted(resources))
    resource_bits = {uav_id: 1 << index for index, uav_id in enumerate(resource_ids)}
    required_mask = 0
    for uav_id in required_preempt_uav_ids:
        bit = resource_bits.get(uav_id)
        if bit is None:
            return None
        required_mask |= bit

    @lru_cache(maxsize=None)
    def solve(index: int, used_mask: int):
        if index == len(order):
            if required_mask & used_mask != required_mask:
                return None
            return 0.0, ()

        task_id = order[index]
        best_result = None
        for edge in options.get(task_id, ()):
            bit = resource_bits.get(edge.uav_id)
            if bit is None or used_mask & bit:
                continue
            remainder = solve(index + 1, used_mask | bit)
            if remainder is None:
                continue
            candidate = (
                edge.transit_time_min + remainder[0],
                ((task_id, edge),) + remainder[1],
            )
            if best_result is None or candidate[0] < best_result[0] - 1e-12:
                best_result = candidate
        return best_result

    result = solve(0, 0)
    if result is None:
        return None
    return {task_id: edge for task_id, edge in result[1]}


def _minimum_cost_maximum_matching(
    task_ids: tuple[str, ...],
    options: dict[str, tuple[FeasibleEdge, ...]],
    resources: dict[str, UavResource],
) -> dict[str, FeasibleEdge]:
    """Find a deterministic minimum-cost matching of maximum cardinality."""
    if not task_ids or not resources:
        return {}
    order = tuple(sorted(
        task_ids,
        key=lambda task_id: (len(options.get(task_id, ())), task_id),
    ))
    resource_ids = tuple(sorted(resources))
    resource_bits = {uav_id: 1 << index for index, uav_id in enumerate(resource_ids)}

    @lru_cache(maxsize=None)
    def solve(index: int, used_mask: int):
        if index == len(order):
            return 0, 0.0, ()
        task_id = order[index]
        best = solve(index + 1, used_mask)
        for edge in options.get(task_id, ()):
            bit = resource_bits.get(edge.uav_id)
            if bit is None or used_mask & bit:
                continue
            remainder = solve(index + 1, used_mask | bit)
            candidate = (
                remainder[0] + 1,
                edge.transit_time_min + remainder[1],
                ((task_id, edge.uav_id, edge),) + remainder[2],
            )
            if (
                candidate[0] > best[0]
                or (
                    candidate[0] == best[0]
                    and (
                        candidate[1] < best[1] - 1e-12
                        or (
                            abs(candidate[1] - best[1]) <= 1e-12
                            and tuple((item[0], item[1]) for item in candidate[2])
                            < tuple((item[0], item[1]) for item in best[2])
                        )
                    )
                )
            ):
                best = candidate
        return best

    result = solve(0, 0)
    return {task_id: edge for task_id, _uav_id, edge in result[2]}


def match_task_ids(
    task_ids: tuple[str, ...] | list[str],
    options: dict[str, tuple[FeasibleEdge, ...]],
    resources: dict[str, UavResource],
    *,
    required_preempt_uav_ids: frozenset[str] = frozenset(),
    require_all: bool = True,
) -> dict[str, FeasibleEdge] | None:
    """Share deterministic task/resource matching across validation paths."""
    normalized_ids = tuple(task_ids)
    normalized_options = {
        task_id: tuple(sorted(
            options.get(task_id, ()),
            key=lambda edge: (edge.transit_time_min, edge.uav_id),
        ))
        for task_id in normalized_ids
    }
    if not require_all:
        return _minimum_cost_maximum_matching(
            normalized_ids, normalized_options, resources,
        )
    maximum = _maximum_matching(normalized_ids, normalized_options)
    if len(maximum) < len(normalized_ids):
        return None
    return _minimum_cost_matching(
        normalized_ids,
        normalized_options,
        resources,
        required_preempt_uav_ids,
    )


def _actual_preempted(
    matching: dict[str, FeasibleEdge],
    selection: MissionSelection,
    snapshot: MissionSnapshot,
    *,
    allow_intent_preempt_search: bool = False,
) -> set[str]:
    candidates, active = _task_maps(snapshot)
    resources = _resource_maps(snapshot)
    active_tasks = {record.task_id: record for record in snapshot.active_tasks}
    actual = set()
    for task_id, edge in matching.items():
        resource = resources[edge.uav_id]
        task = active.get(task_id) or candidates.get(task_id)
        if (
            task is not None
            and (
                task.kind in {"probe", "track"}
                or (
                    task.kind in _SEARCH_TASK_KINDS
                    and allow_intent_preempt_search
                    and bool(task.intent_ids)
                )
            )
            and resource.uav_id in snapshot.preemptible_uav_ids
            and _ordinary_search(resource, active_tasks)
        ):
            actual.add(resource.uav_id)
    return actual


def _validate_selection(
    payload,
    snapshot: MissionSnapshot,
    *,
    reassignment_cooldown_min: float = DEFAULT_REASSIGNMENT_COOLDOWN_MIN,
    allow_probe_preempt_search: bool = True,
    allow_intent_preempt_search: bool = False,
    visible_task_ids: frozenset[str] | None = None,
    _check_underutilization: bool = True,
) -> tuple[str, ...]:
    selection, errors = _selection_object(payload)
    if selection is None:
        return tuple(errors)
    if selection.schema_version != SELECTION_SCHEMA:
        errors.append("invalid_schema_version")
    if selection.snapshot_id != snapshot.snapshot_id:
        errors.append("stale_snapshot")
    if (selection.information_version
            and selection.information_version != snapshot.information_version):
        errors.append("stale_information_version")
    if not isinstance(selection.selected_task_ids, (tuple, list)):
        errors.append("selected_task_ids_must_be_array")
        selected_task_ids = ()
    else:
        selected_task_ids = tuple(selection.selected_task_ids)
        if any(not isinstance(item, str) or not item for item in selected_task_ids):
            errors.append("selected_task_ids_must_contain_nonempty_strings")
    if not isinstance(selection.preempt_uav_ids, (tuple, list)):
        errors.append("preempt_uav_ids_must_be_array")
        preempt_uav_ids = ()
    else:
        preempt_uav_ids = tuple(selection.preempt_uav_ids)
        if any(not isinstance(item, str) or not item for item in preempt_uav_ids):
            errors.append("preempt_uav_ids_must_contain_nonempty_strings")
    if not isinstance(selection.defer_reason, (str, type(None))) or (
        isinstance(selection.defer_reason, str) and not selection.defer_reason.strip()
    ):
        errors.append("invalid_defer_reason")
    if not isinstance(selection.notes, str):
        errors.append("notes_must_be_string")
    if all(isinstance(item, str) for item in selected_task_ids) and len(set(selected_task_ids)) != len(selected_task_ids):
        errors.append("duplicate_task_id")
    if all(isinstance(item, str) for item in preempt_uav_ids) and len(set(preempt_uav_ids)) != len(preempt_uav_ids):
        errors.append("duplicate_preempt_uav_id")

    candidates, active = _task_maps(snapshot)
    pending_search_ids = set(snapshot.pending_search_task_ids)
    # Pending ordinary searches are retained-region audit records.  They stay
    # in the snapshot for overlap and replay checks, but are not legal model
    # selections or new-candidate representatives.
    candidates = {
        task_id: task
        for task_id, task in candidates.items()
        if task_id not in pending_search_ids
    }
    active = {
        task_id: task
        for task_id, task in active.items()
        if task_id not in pending_search_ids
    }
    resources = _resource_maps(snapshot)
    visible = set(candidates) if visible_task_ids is None else set(visible_task_ids)
    if len(resources) != len(snapshot.resources):
        errors.append("duplicate_resource_id")
    known_tasks = set(candidates) | set(active)
    for task_id in selected_task_ids:
        if task_id in pending_search_ids:
            errors.append(f"pending_search_not_selectable:{task_id}")
        elif task_id not in known_tasks:
            errors.append(f"unknown_task_id: {task_id}")
        elif task_id in candidates and task_id not in visible:
            errors.append(f"selected_task_not_visible:{task_id}")
    for task_id, task in (*candidates.items(), *active.items()):
        if task.kind in _SEARCH_TASK_KINDS and task.bbox is None:
            errors.append(f"search_task_missing_bbox: {task_id}")
        if task.kind in {"probe", "track"} and task.contact_id is None:
            errors.append(f"contact_task_missing_contact: {task_id}")

    selected_tasks = [
        active.get(task_id) or candidates.get(task_id)
        for task_id in selected_task_ids
        if task_id in known_tasks
    ]
    coverage_constraint = snapshot.coverage_constraint
    if coverage_constraint is not None:
        selected_representatives = (
            set(selected_task_ids)
            & set(coverage_constraint.representative_task_ids)
        )
        floor_infeasible = coverage_constraint.infeasible_reason is not None
        ordinary_count = sum(
            task.kind == "search" and task.bbox is not None
            and task.task_id in candidates and task.task_id not in active
            for task in selected_tasks
        )
        if (
            not floor_infeasible
            and coverage_constraint.required_new_search_count
            != ordinary_count
        ):
            errors.append(
                "search_count_not_exact:"
                f"{coverage_constraint.required_new_search_count}:{ordinary_count}"
            )
        if (
            not floor_infeasible
            and coverage_constraint.required_new_search_count > 0
            and not set(coverage_constraint.must_service_task_ids).issubset(
                selected_representatives
            )
        ):
            errors.append("coverage_oldest_not_selected")
        for zone in coverage_constraint.zone_requirements:
            if zone.infeasible_reason is not None:
                continue
            selected = set(selected_task_ids) & set(zone.representative_task_ids)
            if len(selected) < zone.required_search_count:
                errors.append(f"zone_quota_not_met:{zone.zone_id}")
            if not set(zone.must_service_task_ids) <= selected:
                errors.append(f"zone_must_service_not_selected:{zone.zone_id}")
    selected_contacts = [task.contact_id for task in selected_tasks if task.contact_id]
    if len(set(selected_contacts)) != len(selected_contacts):
        errors.append("duplicate_contact")
    active_contact_ids = {
        task.contact_id
        for task in snapshot.active_tasks
        if (
            task.status in _ACTIVE_RECORD_STATUSES
            and task.contact_id
            and task.task_id not in selected_task_ids
        )
    }
    if active_contact_ids & set(selected_contacts):
        errors.append("duplicate_contact_with_active_task")

    searches = [task for task in selected_tasks if task.kind in _SEARCH_TASK_KINDS and task.bbox]
    for left_index, left in enumerate(searches):
        for right in searches[left_index + 1:]:
            if _overlap(left.bbox, right.bbox):
                errors.append(f"overlapping_search: {left.task_id}/{right.task_id}")
    active_searches = [
        task for task in snapshot.active_tasks
        if (
            task.status in _ACTIVE_RECORD_STATUSES
            and task.kind in _SEARCH_TASK_KINDS
            and task.bbox
            and task.task_id not in selected_task_ids
        )
    ]
    for candidate in searches:
        if any(_overlap(candidate.bbox, active_task.bbox) for active_task in active_searches):
            errors.append(f"overlapping_active_search: {candidate.task_id}")

    available = set(snapshot.available_uav_ids)
    preemptible = set(snapshot.preemptible_uav_ids)
    if available & preemptible:
        errors.append("resource_in_both_available_and_preemptible")
    for uav_id in preempt_uav_ids:
        if uav_id not in resources:
            errors.append(f"unknown_preempt_uav: {uav_id}")
        elif uav_id not in preemptible:
            errors.append(f"not_preemptible_uav: {uav_id}")
        elif _cooldown_error(resources[uav_id], snapshot, reassignment_cooldown_min):
            errors.append(f"reassignment_cooldown: {uav_id}")

    options = _edge_options(
        selection,
        snapshot,
        reassignment_cooldown_min,
        allow_probe_preempt_search=allow_probe_preempt_search,
        allow_intent_preempt_search=allow_intent_preempt_search,
    )
    if not selected_task_ids and preempt_uav_ids:
        errors.append("preempt_requires_selected_task")
    matching = None
    if selected_task_ids:
            matching = match_task_ids(
                selected_task_ids,
                options,
                resources,
                required_preempt_uav_ids=frozenset(preempt_uav_ids),
            )
            if matching is None:
                # Keep a local exact matching only to report unused declared
                # preemptions when the unconstrained assignment is legal.
                matching = match_task_ids(
                    selected_task_ids, options, resources,
                )
                if matching is None:
                    errors.append("infeasible_assignment")
    if matching is not None:
        actual_preempted = _actual_preempted(
            matching,
            selection,
            snapshot,
            allow_intent_preempt_search=allow_intent_preempt_search,
        )
        declared_preempted = set(preempt_uav_ids)
        for uav_id in sorted(declared_preempted - actual_preempted):
            errors.append(f"unused_preempt_uav: {uav_id}")
        for uav_id in sorted(actual_preempted - declared_preempted):
            errors.append(f"missing_preempt_uav: {uav_id}")

    if not errors and _check_underutilization:
        current_matching = matching or {}
        current_uav_ids = {
            edge.uav_id for edge in current_matching.values()
        }
        idle_uav_ids = set(snapshot.available_uav_ids)
        for candidate in sorted(candidates.values(), key=lambda item: item.task_id):
            if candidate.task_id in selected_task_ids or candidate.task_id not in visible:
                continue
            augmented = MissionSelection(
                SELECTION_SCHEMA,
                snapshot.snapshot_id,
                (*selected_task_ids, candidate.task_id),
                tuple(preempt_uav_ids),
                None,
                "underutilization witness check",
            )
            augmented_errors = _validate_selection(
                augmented,
                snapshot,
                reassignment_cooldown_min=reassignment_cooldown_min,
                allow_probe_preempt_search=allow_probe_preempt_search,
                allow_intent_preempt_search=allow_intent_preempt_search,
                visible_task_ids=frozenset(visible),
                _check_underutilization=False,
            )
            if augmented_errors:
                continue
            augmented_options = _edge_options(
                augmented,
                snapshot,
                reassignment_cooldown_min,
                allow_probe_preempt_search=allow_probe_preempt_search,
                allow_intent_preempt_search=allow_intent_preempt_search,
            )
            augmented_matching = _minimum_cost_matching(
                augmented.selected_task_ids,
                augmented_options,
                resources,
                frozenset(preempt_uav_ids),
            )
            if augmented_matching is None:
                continue
            added_idle_uav_ids = (
                {edge.uav_id for edge in augmented_matching.values()}
                - current_uav_ids
            ) & idle_uav_ids
            if added_idle_uav_ids:
                errors.append(
                    f"underutilized_feasible_work:{candidate.task_id}"
                )
                break
    return tuple(dict.fromkeys(errors))


def validate_selection(
    payload,
    snapshot: MissionSnapshot,
    *,
    visible_task_ids: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Validate a model response against one immutable mission snapshot."""
    return _validate_selection(
        payload,
        snapshot,
        visible_task_ids=visible_task_ids,
    )


def _selection_ids_for_log(selection) -> tuple[str, ...]:
    if isinstance(selection, MissionSelection):
        return tuple(selection.selected_task_ids)
    if isinstance(selection, dict):
        selected = selection.get("selected_task_ids", ())
        return tuple(selected) if isinstance(selected, (list, tuple)) else ()
    return ()


def _pair_selected_tasks(
    selection: MissionSelection | dict,
    snapshot: MissionSnapshot,
    *,
    reassignment_cooldown_min: float,
    allow_probe_preempt_search: bool,
    allow_intent_preempt_search: bool,
    visible_task_ids: frozenset[str] | None = None,
) -> PairingResult:
    """Validate and pair selected work without leaking post-validation exceptions."""
    try:
        errors = _validate_selection(
            selection,
            snapshot,
            reassignment_cooldown_min=reassignment_cooldown_min,
            allow_probe_preempt_search=allow_probe_preempt_search,
            allow_intent_preempt_search=allow_intent_preempt_search,
            visible_task_ids=visible_task_ids,
        )
        if errors:
            return PairingResult((), tuple(errors), False)
        parsed, parse_errors = _selection_object(selection)
        if parsed is None or parse_errors:
            return PairingResult((), tuple(parse_errors), False)
        options = _edge_options(
            parsed,
            snapshot,
            reassignment_cooldown_min,
            allow_probe_preempt_search=allow_probe_preempt_search,
            allow_intent_preempt_search=allow_intent_preempt_search,
        )
        matching = match_task_ids(
            parsed.selected_task_ids,
            options,
            _resource_maps(snapshot),
            required_preempt_uav_ids=frozenset(parsed.preempt_uav_ids),
        )
        if matching is None:
            return PairingResult((), ("infeasible_assignment",), False)
        resources = _resource_maps(snapshot)
        assignments = tuple(
            Assignment(
                task_id,
                matching[task_id].uav_id,
                resources[matching[task_id].uav_id].generation,
                resources[matching[task_id].uav_id].current_task_id,
            )
            for task_id in parsed.selected_task_ids
        )
        return PairingResult(assignments, (), True)
    except Exception as exc:
        snapshot_id = getattr(snapshot, "snapshot_id", "unknown")
        _LOGGER.exception(
            "pair_selected_tasks failed: exception_type=%s snapshot_id=%s "
            "selected_task_ids=%s",
            type(exc).__name__,
            snapshot_id,
            _selection_ids_for_log(selection),
        )
        return PairingResult(
            (),
            (f"pairing_exception:{type(exc).__name__}",),
            False,
        )


def pair_selected_tasks(
    selection: MissionSelection | dict,
    snapshot: MissionSnapshot,
) -> PairingResult:
    """Pair selected work on legal edges at minimum total transit cost."""
    return _pair_selected_tasks(
        selection,
        snapshot,
        reassignment_cooldown_min=DEFAULT_REASSIGNMENT_COOLDOWN_MIN,
        allow_probe_preempt_search=True,
        allow_intent_preempt_search=False,
    )


def _overlap(left, right) -> bool:
    return not (
        left[2] <= right[0]
        or right[2] <= left[0]
        or left[3] <= right[1]
        or right[3] <= left[1]
    )


class MissionScheduler:
    """LLM-backed selection facade with deterministic local pairing."""

    def __init__(
        self,
        gateway=None,
        *,
        llm_gateway=None,
        reassignment_cooldown_min: float = DEFAULT_REASSIGNMENT_COOLDOWN_MIN,
        max_tasks_in_prompt: int = 40,
        allow_probe_preempt_search: bool = True,
        allow_intent_preempt_search: bool = False,
        planning_deadline_seconds: float = DEFAULT_PLANNING_DEADLINE_SECONDS,
        postprocess_reserve_seconds: float = DEFAULT_POSTPROCESS_RESERVE_SECONDS,
        system_prompt_path: str | None = None,
        selection_provider=None,
        strategy_memory_store: StrategyMemoryStore | None = None,
    ):
        if gateway is not None and llm_gateway is not None:
            raise ValueError("pass gateway or llm_gateway, not both")
        self.gateway = gateway if gateway is not None else llm_gateway
        self.reassignment_cooldown_min = float(reassignment_cooldown_min)
        self.max_tasks_in_prompt = int(max_tasks_in_prompt)
        if not isinstance(allow_probe_preempt_search, bool):
            raise ValueError("allow_probe_preempt_search must be boolean")
        if not isinstance(allow_intent_preempt_search, bool):
            raise ValueError("allow_intent_preempt_search must be boolean")
        self.allow_probe_preempt_search = allow_probe_preempt_search
        self.allow_intent_preempt_search = allow_intent_preempt_search
        self.planning_deadline_seconds = float(planning_deadline_seconds)
        self.postprocess_reserve_seconds = float(postprocess_reserve_seconds)
        if self.reassignment_cooldown_min < 0 or not math.isfinite(self.reassignment_cooldown_min):
            raise ValueError("reassignment_cooldown_min must be finite and non-negative")
        if self.max_tasks_in_prompt <= 0:
            raise ValueError("max_tasks_in_prompt must be positive")
        if (
            self.planning_deadline_seconds <= 0
            or not math.isfinite(self.planning_deadline_seconds)
        ):
            raise ValueError("planning_deadline_seconds must be positive and finite")
        if (
            self.postprocess_reserve_seconds < 0
            or not math.isfinite(self.postprocess_reserve_seconds)
            or self.postprocess_reserve_seconds >= self.planning_deadline_seconds
        ):
            raise ValueError(
                "postprocess_reserve_seconds must be finite, non-negative, "
                "and below planning_deadline_seconds"
            )
        if system_prompt_path is None:
            system_prompt_path = os.path.join(
                os.path.dirname(__file__), "prompts", "mission_scheduler.txt"
            )
        with open(system_prompt_path, "r", encoding="utf-8") as stream:
            self.system_prompt = stream.read()
        self.selection_provider = selection_provider
        self.prompt_window = PromptWindow()
        self.strategy_memory_store = strategy_memory_store
        self.last_selection_payload: dict | None = None
        self.last_selection_response: dict | None = None
        self.last_selection_call_id: str | None = None
        self.last_selection_success = False
        self.last_selection_errors: tuple[str, ...] = ()
        self.last_selection_failure_category: str | None = None
        self.last_selection_failure_stage: str | None = None
        self.last_selection_timing: dict = {}

    def validate_selection(
        self,
        payload,
        snapshot: MissionSnapshot,
        *,
        visible_task_ids: frozenset[str] | None = None,
    ) -> tuple[str, ...]:
        return _validate_selection(
            payload,
            snapshot,
            reassignment_cooldown_min=self.reassignment_cooldown_min,
            allow_probe_preempt_search=self.allow_probe_preempt_search,
            allow_intent_preempt_search=self.allow_intent_preempt_search,
            visible_task_ids=visible_task_ids,
        )

    @staticmethod
    def _selection_schema_errors(payload) -> tuple[str, ...]:
        """Return only structural errors eligible for LLM correction."""
        _parsed, errors = _selection_object(payload)
        return tuple(errors)

    def pair_selected_tasks(
        self,
        selection: MissionSelection | dict,
        snapshot: MissionSnapshot,
        *,
        visible_task_ids: frozenset[str] | None = None,
    ) -> PairingResult:
        return _pair_selected_tasks(
            selection,
            snapshot,
            reassignment_cooldown_min=self.reassignment_cooldown_min,
            allow_probe_preempt_search=self.allow_probe_preempt_search,
            allow_intent_preempt_search=self.allow_intent_preempt_search,
            visible_task_ids=visible_task_ids,
        )

    def pair_approved_tasks(
        self,
        snapshot: MissionSnapshot,
        *,
        task_ids: tuple[str, ...] | None = None,
    ) -> tuple[Assignment, ...]:
        """Pair approved, currently unassigned work without a new model call."""
        requested = tuple(task_ids) if task_ids is not None else tuple(
            task.task_id
            for task in snapshot.active_tasks
            if task.status == "approved" and task.assigned_uav_id is None
        )
        if not requested:
            return ()
        selection = MissionSelection(
            SELECTION_SCHEMA,
            snapshot.snapshot_id,
            requested,
            (),
            None,
            "light approved-task pairing",
        )
        options = _edge_options(
            selection,
            snapshot,
            self.reassignment_cooldown_min,
            allow_probe_preempt_search=self.allow_probe_preempt_search,
            allow_intent_preempt_search=self.allow_intent_preempt_search,
        )
        maximum = _maximum_matching(requested, options)
        selected = tuple(task_id for task_id in requested if task_id in maximum)
        if not selected:
            return ()
        matching = _minimum_cost_matching(
            selected,
            options,
            _resource_maps(snapshot),
        )
        if matching is None:
            return ()
        resources = _resource_maps(snapshot)
        return tuple(
            Assignment(
                task_id,
                matching[task_id].uav_id,
                resources[matching[task_id].uav_id].generation,
                resources[matching[task_id].uav_id].current_task_id,
            )
            for task_id in selected
        )

    def decide(
        self,
        snapshot: MissionSnapshot,
        *,
        deadline_monotonic: float | None = None,
    ) -> AssignmentBatch | None:
        """Ask the decision-maker to select work, then pair it atomically."""
        if deadline_monotonic is None:
            deadline_monotonic = time.perf_counter() + self.planning_deadline_seconds
        elif (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(float(deadline_monotonic))
        ):
            raise ValueError("deadline_monotonic must be a finite number")
        started = time.perf_counter()
        prompt_started = time.perf_counter()
        payload = self._prompt_payload(snapshot)
        prompt_finished = time.perf_counter()
        prompt_bytes = len(json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
        ).encode("utf-8"))
        self.last_selection_timing = {
            "prompt_seconds": prompt_finished - prompt_started,
            "prompt_bytes": prompt_bytes,
            "llm_seconds": 0.0,
            "validation_seconds": 0.0,
            "matching_seconds": 0.0,
            "total_seconds": 0.0,
        }
        self.last_selection_payload = payload
        self.last_selection_response = None
        self.last_selection_call_id = None
        self.last_selection_success = False
        self.last_selection_errors = ()
        self.last_selection_failure_category = None
        self.last_selection_failure_stage = None
        if prompt_bytes > 100 * 1024:
            self._fail_selection(
                f"prompt_budget_exceeded:{_prompt_budget_field(payload)}",
                "prompt_budget",
                "prompt",
            )
            return None
        visible_task_ids = frozenset(
            candidate.get("task_id")
            for candidate in payload.get("snapshot", {}).get("candidates", ())
            if isinstance(candidate, dict) and candidate.get("task_id")
        )
        if self._deadline_expired(deadline_monotonic):
            self._fail_selection("decision_deadline_exceeded", "timeout", "preparation")
            return None
        gateway_started = time.perf_counter()
        if self.selection_provider is not None:
            raw = self.selection_provider(snapshot, payload)
            call_id = "selection-provider"
            success = True
            response = raw
            failure_category = None
        elif self.gateway is not None:
            result = self.gateway.request_json(
                role="decision_maker",
                snapshot_id=snapshot.snapshot_id,
                system_prompt=self.system_prompt,
                user_payload=payload,
                # Correct infeasible selections within the gateway's existing
                # retry limit and shared planning deadline; never relax rules.
                validate=lambda candidate: (
                    self._selection_schema_errors(candidate)
                    or self.validate_selection(
                        candidate, snapshot, visible_task_ids=visible_task_ids,
                    )
                ),
                post_validate=lambda candidate: self.validate_selection(
                    candidate,
                    snapshot,
                    visible_task_ids=visible_task_ids,
                ),
                deadline_monotonic=deadline_monotonic,
                transport_deadline_monotonic=(
                    deadline_monotonic - self.postprocess_reserve_seconds
                ),
                max_tokens=1536,
            )
            call_id = result.call_id
            success = result.success
            response = result.payload
            failure_category = result.failure_category
        else:
            self._fail_selection("model_selection_unavailable", "transport", "transport")
            return None
        gateway_finished = time.perf_counter()
        gateway_elapsed = gateway_finished - gateway_started
        gateway_validation = float(getattr(result, "validation_seconds", 0.0)) \
            if self.gateway is not None and self.selection_provider is None else 0.0
        self.last_selection_timing["llm_seconds"] = max(
            0.0, gateway_elapsed - gateway_validation,
        )
        self.last_selection_timing["validation_seconds"] += gateway_validation
        self.last_selection_call_id = call_id
        self.last_selection_response = response
        self.last_selection_success = bool(success)
        self.last_selection_failure_category = failure_category
        if self._deadline_expired(deadline_monotonic):
            self._fail_selection("decision_deadline_exceeded", "timeout", "transport")
            return None
        if not success or response is None:
            self.last_selection_errors = (
                ("decision_deadline_exceeded",)
                if failure_category == "timeout"
                else ("model_selection_unavailable",)
            )
            self.last_selection_failure_stage = (
                "validation" if failure_category == "validation" else "transport"
            )
            self.last_selection_timing["total_seconds"] = time.perf_counter() - started
            return None
        parsed, parse_errors = _selection_object(response)
        if self._deadline_expired(deadline_monotonic):
            self._fail_selection("decision_deadline_exceeded", "timeout", "validation")
            return None
        if parsed is None or parse_errors:
            self._finalize_selection(
                success=False,
                errors=tuple(parse_errors),
                category="validation",
                stage="validation",
            )
            self.last_selection_timing["total_seconds"] = time.perf_counter() - started
            return None
        matching_started = time.perf_counter()
        pairing = self.pair_selected_tasks(
            parsed,
            snapshot,
            visible_task_ids=visible_task_ids,
        )
        self.last_selection_timing["matching_seconds"] = (
            time.perf_counter() - matching_started
        )
        if self._deadline_expired(deadline_monotonic):
            self._fail_selection("decision_deadline_exceeded", "timeout", "matching")
            return None
        if not pairing.is_valid:
            self._finalize_selection(
                success=False,
                errors=("selection_post_validation_failed", *pairing.errors),
                category="validation",
                stage="matching",
            )
            self.last_selection_timing["total_seconds"] = time.perf_counter() - started
            return None
        assignments = pairing.assignments
        self._finalize_selection(success=True)
        self.last_selection_timing["total_seconds"] = time.perf_counter() - started
        return AssignmentBatch(
            snapshot.snapshot_id,
            assignments,
            call_id,
            _information_version=snapshot.information_version,
        )

    @staticmethod
    def _deadline_expired(deadline_monotonic: float) -> bool:
        return time.perf_counter() >= deadline_monotonic

    def _finalize_selection(
        self,
        *,
        success: bool,
        errors: tuple[str, ...] = (),
        category: str | None = None,
        stage: str | None = None,
    ) -> None:
        """Publish one authoritative final state for the selection trace."""
        self.last_selection_success = bool(success)
        self.last_selection_errors = tuple(errors)
        self.last_selection_failure_category = None if success else category
        self.last_selection_failure_stage = None if success else stage

    def _fail_selection(
        self,
        error: str,
        category: str,
        stage: str = "transport",
    ) -> None:
        self._finalize_selection(
            success=False,
            errors=(error,),
            category=category,
            stage=stage,
        )
        if self.last_selection_timing:
            self.last_selection_timing["total_seconds"] = (
                max(
                    self.last_selection_timing.get("total_seconds", 0.0),
                    self.last_selection_timing.get("prompt_seconds", 0.0)
                    + self.last_selection_timing.get("llm_seconds", 0.0)
                    + self.last_selection_timing.get("validation_seconds", 0.0)
                    + self.last_selection_timing.get("matching_seconds", 0.0),
                )
            )

    def selection_interaction(self) -> dict:
        """Return the complete, redacted decision trace for the public frame."""
        call = None
        call_id = self.last_selection_call_id
        logs = getattr(self.gateway, "call_log", ()) if self.gateway is not None else ()
        if call_id:
            for candidate in reversed(logs):
                if isinstance(candidate, dict) and candidate.get("call_id") == call_id:
                    call = deepcopy(candidate)
                    break
        if call is None:
            call = {}
        redact_log = getattr(self.gateway, "redact_log", None)
        if callable(redact_log):
            call = redact_log(call)

        attempts = []
        for original in call.get("attempts", ()):
            if not isinstance(original, dict):
                continue
            attempt = deepcopy(original)
            attempt["response"] = attempt.get("raw_output") or ""
            attempts.append(attempt)

        system_prompt = self.system_prompt
        user_prompt = json.dumps(
            self.last_selection_payload or {}, ensure_ascii=False, allow_nan=False,
        )
        if attempts:
            messages = attempts[0].get("messages") or []
            if len(messages) > 0 and isinstance(messages[0], dict):
                system_prompt = messages[0].get("content") or system_prompt
            if len(messages) > 1 and isinstance(messages[1], dict):
                user_prompt = messages[1].get("content") or user_prompt

        response = ""
        for attempt in reversed(attempts):
            if attempt.get("response"):
                response = attempt["response"]
                break
        if not response and self.last_selection_response is not None:
            response = json.dumps(
                _jsonable(self.last_selection_response),
                ensure_ascii=False,
                allow_nan=False,
            )

        model = call.get("model")
        if not model and self.gateway is not None:
            resolve_binding = getattr(self.gateway, "resolve_binding", None)
            if callable(resolve_binding):
                try:
                    model = resolve_binding("decision_maker").get("model")
                except (AttributeError, KeyError, TypeError):
                    model = None
        interaction = {
            "call_id": call_id,
            "model": model or "unknown",
            "attempts": attempts,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response": response,
            "validation": {
                "is_valid": bool(self.last_selection_success),
                "errors": list(self.last_selection_errors),
            },
            "success": bool(self.last_selection_success),
            "errors": list(self.last_selection_errors),
            "failure_category": self.last_selection_failure_category,
            "failure_stage": self.last_selection_failure_stage,
            "timing": dict(self.last_selection_timing),
        }
        for key in ("episode_id", "snapshot_id", "sim_time_min", "memory_version"):
            if key in call:
                interaction[key] = call[key]
        return interaction

    def _prompt_payload(self, snapshot: MissionSnapshot) -> dict:
        prompt_limit = min(self.max_tasks_in_prompt, 40)
        # Route feasibility alone does not authorize taking a busy aircraft.
        # Apply the same resource and preemption rules as the final matcher.
        candidates, active = _task_maps(snapshot)
        resources = _resource_maps(snapshot)
        legal_edges = []
        for edge in snapshot.feasible_edges:
            selection = MissionSelection(
                SELECTION_SCHEMA, snapshot.snapshot_id, (edge.task_id,),
                (edge.uav_id,) if edge.uav_id in snapshot.preemptible_uav_ids else (),
                None, "",
            )
            if _edge_usable(
                edge, edge.task_id, candidates, active, resources, snapshot,
                selection, self.reassignment_cooldown_min,
                allow_probe_preempt_search=self.allow_probe_preempt_search,
                allow_intent_preempt_search=self.allow_intent_preempt_search,
            ):
                legal_edges.append(edge)
        legal_uavs = {}
        for edge in legal_edges:
            legal_uavs.setdefault(edge.task_id, set()).add(edge.uav_id)
        all_candidates = tuple(
            task for task in snapshot.candidates if task.task_id in legal_uavs
        )
        candidate_count = len(snapshot.candidates)
        explicit_window = bool(snapshot.prompt_task_ids)
        prompt_sources: dict[str, str] = {}
        prompt_skip_cycles: dict[str, int] = {}
        fairness_bound_cycles = 0

        if explicit_window:
            candidate_by_id = {task.task_id: task for task in all_candidates}
            ordered_tasks = [
                candidate_by_id[task_id]
                for task_id in snapshot.prompt_task_ids
                if task_id in candidate_by_id
            ][:prompt_limit]
            prompt_sources = dict(snapshot.prompt_sources)
            fairness_bound_cycles = math.ceil(
                candidate_count / max(1, min(8, prompt_limit))
            ) if candidate_count else 0
            geometry_filtered = False
        else:
            edge_ids = {edge.task_id for edge in snapshot.feasible_edges}
            prompt_candidates = [
                task for task in all_candidates if task.task_id in edge_ids
            ] or list(all_candidates)
            if len(prompt_candidates) > prompt_limit:
                window = self.prompt_window.select(
                    prompt_candidates,
                    prompt_limit,
                    cycle=max(0, int(snapshot.sim_time_min)),
                )
                window_ids = {task.task_id for task in window.tasks}
                ordered_tasks = [
                    *window.tasks,
                    *(task for task in prompt_candidates if task.task_id not in window_ids),
                ]
                prompt_sources = window.sources
                prompt_skip_cycles = window.skip_cycles
                fairness_bound_cycles = window.fairness_bound_cycles
            else:
                ordered_tasks = prompt_candidates
            candidate_payloads = [
                _candidate_payload(task) for task in ordered_tasks
            ]
            selected_payloads, geometry_filtered = _filter_prompt_candidates(
                candidate_payloads, prompt_limit,
            )
            ordered_tasks = [
                task for task, candidate in zip(ordered_tasks, candidate_payloads)
                if candidate.get("task_id") in {
                    selected.get("task_id") for selected in selected_payloads
                }
            ]

        prompt_candidate_payloads = [
            _candidate_payload(task) for task in ordered_tasks
        ]
        for candidate in prompt_candidate_payloads:
            candidate["feasible_uav_ids"] = sorted(legal_uavs[candidate["task_id"]])
        prompt_task_ids = {
            candidate["task_id"]
            for candidate in prompt_candidate_payloads
            if candidate.get("task_id")
        }
        prompt_sources = {
            task_id: source
            for task_id, source in prompt_sources.items()
            if task_id in prompt_task_ids
        }
        prompt_skip_cycles = {
            task_id: value
            for task_id, value in prompt_skip_cycles.items()
            if task_id in prompt_task_ids
        }
        prompt_edges = [
            _jsonable(edge)
            for edge in legal_edges
            if edge.task_id in prompt_task_ids
        ]
        full = {
            "snapshot_id": snapshot.snapshot_id,
            "sim_time_min": snapshot.sim_time_min,
            "candidates": prompt_candidate_payloads,
            "candidate_count": candidate_count,
            "candidates_truncated": len(prompt_candidate_payloads) < candidate_count,
            "available_uav_ids": list(snapshot.available_uav_ids),
            "preemptible_uav_ids": list(snapshot.preemptible_uav_ids),
            "uav_generations": [list(item) for item in snapshot.uav_generations],
            "resources": [_jsonable(resource) for resource in snapshot.resources],
            "feasible_edges": _compact_prompt_edges(prompt_edges, prompt_task_ids),
            "feasible_edges_compacted": True,
            "active_tasks": [
                _active_task_payload(task) for task in snapshot.active_tasks
            ],
            "contacts": [
                _contact_payload(contact)
                for contact in snapshot.contacts[:20]
            ],
            "intents": [_jsonable(intent) for intent in snapshot.intents],
            "intent_statuses": [
                _jsonable(status) for status in snapshot.intent_statuses
            ],
            "memory_version": snapshot.memory_version,
            "planning_map_version": snapshot.planning_map_version,
            "reviewer_summary": str(snapshot.reviewer_summary)[:1200],
            "information_version": snapshot.information_version,
            "prompt_sources": prompt_sources,
            "prompt_skip_cycles": prompt_skip_cycles,
            "prompt_fairness_bound_cycles": fairness_bound_cycles,
            "prompt_geometry_filtered": geometry_filtered,
            "pending_search_task_ids": list(snapshot.pending_search_task_ids),
        }
        if snapshot.coverage_constraint is not None:
            full["coverage_constraint"] = _jsonable(snapshot.coverage_constraint)
            for key in ("zone_requirements", "zone_infeasible"):
                if not full["coverage_constraint"][key]:
                    full["coverage_constraint"].pop(key)
        if snapshot.coverage_summary is not None:
            full["coverage_summary"] = _jsonable(snapshot.coverage_summary)
        strategy_context = _strategy_context(snapshot)
        memories = ()
        if self.strategy_memory_store is not None:
            memories = self.strategy_memory_store.select_for_context(
                strategy_context, snapshot.memory_version,
            )
        full["strategy_memory_context"] = strategy_context
        full["strategy_memories"] = [_jsonable(memory) for memory in memories]
        return {
            "schema_version": SELECTION_SCHEMA,
            "instructions": self.system_prompt,
            "snapshot": full,
        }


def _compact_prompt_edges(edges: list[dict], task_ids: set[str]) -> list[dict]:
    """Keep model-facing route facts small; validation still uses full edges."""
    grouped: dict[str, list[dict]] = {}
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("task_id") not in task_ids:
            continue
        grouped.setdefault(edge["task_id"], []).append(edge)

    compacted = []
    for task_id in sorted(grouped):
        options = grouped[task_id]
        compacted.append({
            "task_id": task_id,
            "uav_options": [
                {
                    "uav_id": edge["uav_id"],
                    "transit_time_min": edge["transit_time_min"],
                    "total_range_cells": (
                        edge["mission_range_cells"]
                        + edge["return_range_cells"]
                        + edge["reserve_range_cells"]
                    ),
                }
                for edge in sorted(options, key=lambda item: item["uav_id"])
            ],
        })
    return compacted


def _filter_prompt_candidates(
    candidates: list[dict], limit: int,
) -> tuple[list[dict], bool]:
    """Avoid presenting mutually overlapping search rectangles to the model."""
    selected: list[dict] = []
    selected_bboxes: list[tuple[float, float, float, float]] = []
    filtered = False
    for candidate in candidates:
        bbox = candidate.get("bbox") if isinstance(candidate, dict) else None
        normalized = None
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                normalized = tuple(float(value) for value in bbox)
            except (TypeError, ValueError):
                normalized = None
        if normalized is not None and any(
            _overlap(normalized, existing) for existing in selected_bboxes
        ):
            filtered = True
            continue
        selected.append(candidate)
        if normalized is not None:
            selected_bboxes.append(normalized)
        if len(selected) >= limit:
            break
    return selected, filtered


__all__ = [
    "Assignment",
    "AssignmentBatch",
    "FeasibleEdge",
    "MissionScheduler",
    "MissionSelection",
    "MissionSnapshot",
    "PairingResult",
    "TaskRecord",
    "UavResource",
    "match_task_ids",
    "pair_selected_tasks",
    "validate_selection",
    ]


def _strategy_context(snapshot: MissionSnapshot) -> dict[str, str]:
    """Map a scheduler snapshot to the fixed memory condition vocabulary."""
    resource_count = len(snapshot.resources)
    available_fraction = (
        len(snapshot.available_uav_ids) / resource_count
        if resource_count else 0.0
    )
    live_contacts = sum(
        contact.state not in {"cleared", "lost", "departed"}
        for contact in snapshot.contacts
    )
    contact_denominator = max(resource_count, 3)

    def band(value: float) -> str:
        if value < 1.0 / 3.0:
            return "low"
        if value < 2.0 / 3.0:
            return "medium"
        return "high"

    return {
        "ais_contact_load": band(live_contacts / contact_denominator),
        "available_uav_fraction": band(available_fraction),
        "has_active_intent": (
            "yes" if any(intent.lifecycle == "active" for intent in snapshot.intents)
            else "no"
        ),
        # Obstacle impact is intentionally not inferred from free-form text;
        # the snapshot contract has no weather metric, so baseline is used.
        "weather_disruption": "low",
    }

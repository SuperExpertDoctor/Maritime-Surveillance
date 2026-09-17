"""Build stable, observation-only mission candidates.

The catalog owns candidate identity and age.  It deliberately does not change
UAV or contact state: a candidate becomes an execution fact only after the
mission scheduler approves and the coordinator commits it.
"""
from __future__ import annotations

import math
from src.mission.contracts import ContactSnapshot, Intent, TaskCandidate
from src.mission.intent_store import build_scheduling_value
from src.schedule.candidate_extractor import CandidateExtractor


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
_SEARCH_TASK_KINDS = {"search", "direction_search", "investigation"}
_PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2}


class TaskCatalog:
    """Maintain the complete stable candidate queue for one episode."""

    def __init__(self, *, candidate_extractor: CandidateExtractor | None = None):
        self.extractor = candidate_extractor or CandidateExtractor()
        self._task_ids: dict[tuple, str] = {}
        self._eligible_since: dict[tuple, float] = {}
        self._next_task_number = 0

    def build(
        self,
        state,
        contacts: tuple[ContactSnapshot, ...],
        intents: tuple[Intent, ...],
        now_min: float,
    ) -> tuple[TaskCandidate, ...]:
        """Return all currently legal candidates in deterministic order.

        Prompt limits belong to the caller.  Returning the full tuple here is
        important because an older candidate must keep its age even when it
        was omitted from one capped model request.
        """
        now = self._time(now_min)
        contacts = tuple(sorted(contacts, key=lambda contact: contact.contact_id))
        intents = tuple(sorted(intents, key=lambda intent: intent.intent_id))
        resources = self._resource_ids(state, now)

        tasks: list[TaskCandidate] = []
        for contact in contacts:
            if self._is_probe_candidate(contact, now):
                tasks.append(self._contact_task(
                    contact, "probe", resources, state, now,
                ))
            elif self._is_track_candidate(contact):
                tasks.append(self._contact_task(
                    contact, "track", resources, state, now,
                ))

        for candidate in self._search_candidates(state, intents, now):
            task = self._search_task(candidate, resources, state, intents, now)
            if task is not None:
                tasks.append(task)

        return tuple(tasks)

    @staticmethod
    def _time(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("now_min must be a finite non-negative number")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError("now_min must be a finite non-negative number")
        return value

    @staticmethod
    def _is_probe_candidate(contact: ContactSnapshot, now: float) -> bool:
        return (
            contact.state == "pending"
            and contact.vessel_class == "unknown"
            and contact.assigned_uav_id is None
            and contact.active_probe_id is None
            and now >= contact.next_probe_not_before_min
        )

    @staticmethod
    def _is_track_candidate(contact: ContactSnapshot) -> bool:
        return (
            (
                contact.vessel_class == "type_ii"
                or getattr(contact, "activity", "unknown")
                in {"suspected_violation", "confirmed_violation"}
            )
            and contact.state not in {"cleared", "lost", "departed"}
            and contact.assigned_uav_id is None
            and contact.active_probe_id is None
        )

    def _contact_task(
        self,
        contact: ContactSnapshot,
        kind: str,
        resources: tuple[tuple[str, tuple[float, float], float, float, str | None], ...],
        state,
        now: float,
    ) -> TaskCandidate:
        # A contact can legitimately move from probe to track. Keep the
        # operation identity distinct so a completed probe record cannot mask
        # the successor track candidate in scheduler snapshots.
        key = ("contact", kind, contact.contact_id)
        task_id = self._task_id(key)
        eligible_since = self._remember_age(key, contact.first_seen_min, now)
        wait = min(max(0.0, now - eligible_since) / 30.0, 1.0)
        task_duration = self._contact_duration(state)
        feasible = tuple(
            uav_id
            for uav_id, position, remaining, speed, _current_task_id in resources
            if self._within_range(
                position,
                contact.estimated_position_cells,
                remaining,
                speed,
                task_duration,
                self._return_distance(state, contact.estimated_position_cells),
            )
        )
        return TaskCandidate(
            task_id=task_id,
            kind=kind,
            bbox=None,
            contact_id=contact.contact_id,
            intent_ids=(),
            feasible_uav_ids=feasible,
            eligible_since_min=eligible_since,
            priority="high",
            estimated_duration_min=task_duration,
            utility=wait + 1.0,
            expected_information_gain=1.0 if kind == "probe" else 0.0,
            _information_version=int(getattr(state, "information_version", 0)),
        )

    def _search_task(
        self,
        candidate: dict,
        resources,
        state,
        intents: tuple[Intent, ...],
        now: float,
    ) -> TaskCandidate | None:
        bbox = self._bbox(candidate.get("bbox"))
        if bbox is None:
            return None
        key = ("search", bbox)
        supplied_task_id = candidate.get("task_id")
        if isinstance(supplied_task_id, str) and supplied_task_id:
            task_id = supplied_task_id
        else:
            task_id = self._task_id(key)
        supplied_age = candidate.get("eligible_since_min", now)
        eligible_since = self._remember_age(key, supplied_age, now)
        intent_ids = self._intent_ids(candidate, bbox, intents)
        priority = self._search_priority(candidate, intent_ids, intents)
        avg_info = self._finite(candidate.get("avg_info", 0.0), 0.0)
        total_value = self._finite(candidate.get("total_value", 0.0), 0.0)
        cell_count = candidate.get("cell_count")
        if not isinstance(cell_count, int) or isinstance(cell_count, bool) or cell_count <= 0:
            cell_count = max(1, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        utility = total_value / cell_count
        duration = self._finite(candidate.get("estimated_duration_min", 5.0), 5.0)
        duration = max(0.1, duration)
        target = self._bbox_center(bbox)
        feasible = tuple(
            uav_id
            for uav_id, position, remaining, speed, current_task_id in resources
            if self._within_range(
                position,
                target,
                remaining,
                speed,
                duration,
                self._return_distance(state, target),
            )
        )
        kind = candidate.get("kind", "search")
        if kind not in _SEARCH_TASK_KINDS:
            kind = "search"
        return TaskCandidate(
            task_id=task_id,
            kind=kind,
            bbox=bbox,
            contact_id=None,
            intent_ids=intent_ids,
            feasible_uav_ids=feasible,
            eligible_since_min=eligible_since,
            priority=priority,
            estimated_duration_min=duration,
            utility=utility,
            expected_information_gain=max(0.0, min(1.0, 1.0 - avg_info)),
            _information_version=int(getattr(state, "information_version", 0)),
        )

    @staticmethod
    def _passive_investigation_candidates(state, now: float) -> tuple[dict, ...]:
        getter = getattr(state, "get_passive_positions", None)
        if not callable(getter):
            return ()
        grid = getattr(state.config, "grid", None)
        resolution = tuple(getattr(grid, "resolution", (0, 0)))
        if len(resolution) != 2:
            return ()
        width = max(1, int(math.ceil(math.sqrt(
            max(1, int(getattr(grid, "search_min_cells", 1)))
        ))))
        result = []
        for position in getter(now):
            x, y = position.position_cells
            col = int(math.floor(x - width / 2.0))
            row = int(math.floor(y - width / 2.0))
            col = max(1, min(col, resolution[0] - width - 1))
            row = max(1, min(row, resolution[1] - width - 1))
            bbox = (col, row, col + width, row + width)
            result.append({
                "task_id": f"investigation:{position.emitter_track_id}",
                "kind": "investigation",
                "bbox": bbox,
                "cell_count": width * width,
                "avg_info": 0.0,
                "total_value": 1.0,
                "eligible_since_min": min(now, position.observed_at_min),
                "priority": "high",
            })
        return tuple(result)

    @staticmethod
    def _passive_direction_candidates(state, now: float) -> tuple[dict, ...]:
        getter = getattr(state, "get_passive_observations", None)
        if not callable(getter):
            return ()
        grid = state.config.grid
        resolution = tuple(grid.resolution)
        width = max(1, int(math.ceil(math.sqrt(max(1, grid.search_min_cells)))))
        position_getter = getattr(state, "get_passive_positions", None)
        positions = position_getter(now) if callable(position_getter) else ()
        point_observations = {
            observation_id
            for position in positions
            for observation_id in position.source_observation_ids
        }
        result = []
        for observation in getter(now):
            if observation.observation_id in point_observations:
                continue
            angle = math.radians(observation.bearing_deg)
            center = (
                observation.observer_position_cells[0] + 3.0 * math.cos(angle),
                observation.observer_position_cells[1] + 3.0 * math.sin(angle),
            )
            col = int(math.floor(center[0] - width / 2.0))
            row = int(math.floor(center[1] - width / 2.0))
            col = max(1, min(col, resolution[0] - width - 1))
            row = max(1, min(row, resolution[1] - width - 1))
            result.append({
                "task_id": f"direction:{observation.observation_id}",
                "kind": "direction_search",
                "bbox": (col, row, col + width, row + width),
                "cell_count": width * width,
                "avg_info": 0.0,
                "total_value": 0.6,
                "eligible_since_min": min(now, observation.observed_at_min),
                "priority": "high",
            })
        return tuple(result)

    def _search_candidates(
        self, state, intents: tuple[Intent, ...], now: float,
    ) -> tuple[dict, ...]:
        supplied = getattr(state, "candidate_result", None)
        if supplied is None:
            getter = getattr(state, "get_candidate_result", None)
            if callable(getter):
                supplied = getter()

        if supplied is None:
            scheduling_value = None
            if intents and all(
                hasattr(state, name)
                for name in ("get_value_matrix", "get_searchable_mask")
            ) and callable(getattr(state, "get_last_scan_matrix", None)):
                try:
                    scheduling_value = build_scheduling_value(
                        state.get_value_matrix(),
                        intents,
                        state.get_last_scan_matrix(),
                        now,
                        searchable_mask=state.get_searchable_mask(),
                    )
                except (AttributeError, TypeError, ValueError):
                    scheduling_value = None
            extract_pool = getattr(self.extractor, "extract_pool", None)
            if callable(extract_pool):
                supplied = extract_pool(state, scheduling_value=scheduling_value)
            else:
                supplied = self.extractor.extract(
                    state,
                    scheduling_value=scheduling_value,
                    intents=intents,
                )

        raw_candidates = (
            supplied.candidate_regions
            if hasattr(supplied, "candidate_regions")
            else getattr(supplied, "candidates", supplied)
        )
        merged: dict[tuple[int, int, int, int], dict] = {}
        candidates = [
            *self._passive_investigation_candidates(state, now),
            *self._passive_direction_candidates(state, now),
        ]
        candidates.extend(raw_candidates or ())
        for raw in candidates:
            if isinstance(raw, dict):
                item = dict(raw)
            elif hasattr(raw, "bbox"):
                item = {
                    "task_id": getattr(raw, "task_id", None),
                    "bbox": getattr(raw, "bbox", None),
                    "cell_count": len(getattr(raw, "cells", ())),
                    "avg_info": getattr(raw, "mean_value", 0.0),
                    "total_value": getattr(raw, "total_value", 0.0),
                    "eligible_since_min": getattr(raw, "eligible_since_min", now),
                }
            else:
                continue
            bbox = self._bbox(item.get("bbox"))
            if bbox is None:
                continue
            item["bbox"] = bbox
            item["intent_ids"] = tuple(item.get("intent_ids", ()))
            previous = merged.get(bbox)
            if previous is None:
                merged[bbox] = item
                continue
            previous["intent_ids"] = tuple(dict.fromkeys(
                (*previous.get("intent_ids", ()), *item["intent_ids"]),
            ))
            for name in ("total_value", "avg_info", "cell_count", "eligible_since_min"):
                if name not in previous and name in item:
                    previous[name] = item[name]
                elif name == "total_value" and name in item:
                    previous[name] = max(
                        self._finite(previous.get(name), 0.0),
                        self._finite(item.get(name), 0.0),
                    )
        return tuple(merged.values())

    def _resource_ids(self, state, now: float):
        del now
        resources = []
        getter = getattr(state, "get_all_uavs", None)
        for uav in getter() if callable(getter) else ():
            status = str(getattr(uav, "status", "idle")).lower()
            operation = str(getattr(uav, "operation_mode", status)).lower()
            current_task_id = getattr(uav, "assigned_region_id", None)
            if operation in _PROTECTED_OPERATIONS or status in _PROTECTED_OPERATIONS:
                continue
            if status not in {"idle", "holding", "transit", "searching"} and operation not in {
                "idle", "holding", *_ORDINARY_SEARCH_OPERATIONS,
            }:
                continue
            position = getattr(uav, "position", (0.0, 0.0))
            if hasattr(position, "col") and hasattr(position, "row"):
                position = (float(position.col), float(position.row))
            else:
                values = tuple(position)
                position = (
                    float(values[0]) if values else 0.0,
                    float(values[1]) if len(values) > 1 else 0.0,
                )
            speed = self._uav_speed(state)
            remaining = self._uav_remaining_range(state, uav)
            resources.append((uav.id, position, remaining, speed, current_task_id))
        return tuple(sorted(resources, key=lambda item: item[0]))

    @staticmethod
    def _uav_speed(state) -> float:
        config = getattr(state, "config", None)
        uav_config = getattr(config, "uav", None)
        grid = getattr(config, "grid", None)
        speed = getattr(uav_config, "cruise_speed_kmh", 1.0)
        cell_size = getattr(grid, "cell_size_km", 1.0)
        return max(1e-6, float(speed) / 60.0 / float(cell_size))

    @classmethod
    def _uav_remaining_range(cls, state, uav) -> float:
        direct = getattr(uav, "remaining_range_cells", None)
        if direct is not None:
            try:
                return max(0.0, float(direct))
            except (TypeError, ValueError):
                pass
        config = getattr(state, "config", None)
        uav_config = getattr(config, "uav", None)
        grid = getattr(config, "grid", None)
        total = (
            float(getattr(uav_config, "sortie_endurance_h", 1.0))
            * float(getattr(uav_config, "cruise_speed_kmh", 1.0))
            / float(getattr(grid, "cell_size_km", 1.0))
        )
        return max(0.0, total * float(getattr(uav, "fuel_remaining_pct", 1.0)))

    def _return_distance(self, state, position) -> float:
        if state is None:
            return 0.0
        getter = getattr(state, "get_base_positions", None)
        if not callable(getter):
            return 0.0
        bases = getter()
        if not bases:
            return 0.0
        return min(math.dist(position, tuple(map(float, base))) for base in bases)

    @staticmethod
    def _within_range(position, target, remaining, speed, duration, return_distance):
        transit = math.dist(position, target)
        mission = max(0.0, duration) * max(0.0, speed)
        return transit + mission + max(0.0, return_distance) <= remaining + 1e-9

    @staticmethod
    def _contact_duration(state) -> float:
        config = getattr(state, "config", None)
        contact = getattr(getattr(config, "mission", None), "contact", None)
        baseline = getattr(contact, "baseline_duration_min", 0.0)
        near = getattr(contact, "near_duration_min", 0.0)
        return max(1.0, float(baseline) + float(near))

    def _task_id(self, key: tuple) -> str:
        existing = self._task_ids.get(key)
        if existing is not None:
            return existing
        self._next_task_number += 1
        task_id = f"Q{self._next_task_number:04d}"
        self._task_ids[key] = task_id
        return task_id

    def _remember_age(self, key: tuple, value, now: float) -> float:
        try:
            age = float(value)
        except (TypeError, ValueError):
            age = now
        if not math.isfinite(age):
            age = now
        age = max(0.0, min(now, age))
        if key not in self._eligible_since:
            self._eligible_since[key] = age
        else:
            self._eligible_since[key] = min(self._eligible_since[key], age)
        return self._eligible_since[key]

    @staticmethod
    def _bbox(value) -> tuple[int, int, int, int] | None:
        if not isinstance(value, (tuple, list)) or len(value) != 4:
            return None
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            return None
        c0, r0, c1, r1 = value
        if c0 >= c1 or r0 >= r1:
            return None
        return c0, r0, c1, r1

    @staticmethod
    def _bbox_center(bbox):
        return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)

    @staticmethod
    def _finite(value, default: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    @staticmethod
    def _intent_ids(candidate, bbox, intents):
        explicit = tuple(dict.fromkeys(
            item for item in candidate.get("intent_ids", ()) if isinstance(item, str)
        ))
        if explicit:
            return explicit
        c0, r0, c1, r1 = bbox
        result = []
        for intent in intents:
            i0, j0, i1, j1 = intent.bbox
            if c0 < i1 and i0 < c1 and r0 < j1 and j0 < r1:
                result.append(intent.intent_id)
        return tuple(result)

    @staticmethod
    def _search_priority(candidate, intent_ids, intents):
        values = [
            intent.priority
            for intent in intents
            if intent.intent_id in intent_ids and intent.lifecycle == "active"
        ]
        candidate_priority = candidate.get("priority", "medium")
        if candidate_priority not in _PRIORITY_ORDER:
            candidate_priority = "medium"
        if values:
            return max((*values, candidate_priority), key=_PRIORITY_ORDER.get)
        return candidate_priority


__all__ = ["TaskCatalog"]

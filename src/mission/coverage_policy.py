"""Deterministic whole-domain responsibility policy for SAR coverage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any

import numpy as np


_CATEGORY_ORDER = (
    "fresh",
    "deferred_weather",
    "assigned_due",
    "geometry",
    "waiting_due",
)
_UNSEEN = -np.inf


@dataclass(frozen=True)
class CoverageCandidateWindow:
    """One immutable candidate set shared by route checks and the prompt."""

    tasks: tuple[Any, ...]
    representative_task_ids: tuple[str, ...] = ()
    sources: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        tasks = tuple(self.tasks)
        ids = tuple(
            getattr(task, "task_id", None)
            for task in tasks
        )
        if any(not isinstance(task_id, str) or not task_id for task_id in ids):
            raise ValueError("coverage window tasks require non-empty task IDs")
        if len(set(ids)) != len(ids):
            raise ValueError("coverage window task IDs must be unique")
        representative = tuple(self.representative_task_ids)
        if any(task_id not in ids for task_id in representative):
            raise ValueError("coverage representatives must be in the window")
        sources = tuple((str(task_id), str(source)) for task_id, source in self.sources)
        if len({task_id for task_id, _source in sources}) != len(sources):
            raise ValueError("coverage window sources must be unique")
        if any(task_id not in ids for task_id, _source in sources):
            raise ValueError("coverage window source task must be in the window")
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "representative_task_ids", representative)
        object.__setattr__(self, "sources", sources)


class CoveragePolicy:
    """Partition a fixed SAR domain and rank its search candidates.

    The policy intentionally uses only actual SAR timestamps for freshness.
    Other scheduling matrices can decide feasibility and assignment, but they
    cannot make a cell fresh.
    """

    def __init__(self, fixed_mask: np.ndarray, *, primary_window_min: int = 60) -> None:
        fixed = _mask(fixed_mask, "fixed_mask")
        if isinstance(primary_window_min, bool) or not isinstance(primary_window_min, Integral):
            raise ValueError("primary_window_min must be a positive integer")
        primary_window_min = int(primary_window_min)
        if primary_window_min <= 0:
            raise ValueError("primary_window_min must be a positive integer")

        self._fixed_mask = fixed.copy()
        self._fixed_mask.setflags(write=False)
        self.primary_window_min = primary_window_min

    @property
    def fixed_mask(self) -> np.ndarray:
        """Return a detached copy of the fixed responsibility domain."""
        return self._fixed_mask.copy()

    def classify(
        self,
        *,
        now_min: float,
        last_sar: np.ndarray,
        feasible_mask: np.ndarray,
        assigned_mask: np.ndarray,
        legal_candidate_mask: np.ndarray,
    ) -> dict[str, np.ndarray]:
        """Return an exhaustive, mutually exclusive responsibility partition.

        Freshness follows the left-open/right-closed interval
        ``(now_min - primary_window_min, now_min]``.  The remaining fixed
        cells are assigned in precedence order: weather, assigned work,
        geometry exclusion, then waiting for a legal candidate.
        """
        now = _time(now_min, "now_min")
        last = _last_sar(last_sar, self._fixed_mask.shape, now)
        feasible = _mask_like(feasible_mask, self._fixed_mask.shape, "feasible_mask")
        assigned = _mask_like(assigned_mask, self._fixed_mask.shape, "assigned_mask")
        legal = _mask_like(
            legal_candidate_mask,
            self._fixed_mask.shape,
            "legal_candidate_mask",
        )

        fixed = self._fixed_mask
        fresh = fixed & np.isfinite(last) & (last > now - self.primary_window_min)
        remaining = fixed & ~fresh
        deferred_weather = remaining & ~feasible
        remaining &= feasible
        assigned_due = remaining & assigned
        remaining &= ~assigned_due
        geometry = remaining & ~legal
        waiting_due = remaining & legal

        return {
            "fresh": fresh.copy(),
            "deferred_weather": deferred_weather.copy(),
            "assigned_due": assigned_due.copy(),
            "geometry": geometry.copy(),
            "waiting_due": waiting_due.copy(),
        }

    @staticmethod
    def rank_search_candidates(
        candidates: Iterable[Any],
        *,
        now_min: float,
        last_sar: np.ndarray,
        estimated_minutes: Mapping[str, Real],
        primary_window_min: int = 60,
    ) -> tuple[Any, ...]:
        """Return candidates ordered by unseen work, age, density, then bbox."""
        return rank_search_candidates(
            candidates,
            now_min=now_min,
            last_sar=last_sar,
            estimated_minutes=estimated_minutes,
            primary_window_min=primary_window_min,
        )

    def select_window(
        self,
        ranked_candidates: Iterable[Any],
        *,
        ordinary_reserve: int,
        capacity: int,
        now_min: float,
        zones=None,
    ) -> CoverageCandidateWindow:
        """Reserve ordinary coverage slots before filling urgent work.

        ``ranked_candidates`` is already ordered by cell responsibility.  The
        method only chooses from that sequence; it never performs geometry or
        changes task identity.  This makes the returned IDs safe to reuse for
        edge construction and prompt serialization.
        """
        if isinstance(ordinary_reserve, bool) or not isinstance(ordinary_reserve, int):
            raise ValueError("ordinary_reserve must be a non-negative integer")
        if ordinary_reserve < 0:
            raise ValueError("ordinary_reserve must be a non-negative integer")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        _time(now_min, "now_min")

        normalized: list[Any] = []
        seen_ids: set[str] = set()
        for task in ranked_candidates:
            task_id = getattr(task, "task_id", None)
            if not isinstance(task_id, str) or not task_id or task_id in seen_ids:
                continue
            seen_ids.add(task_id)
            normalized.append(task)
        # Only ordinary SAR rectangles consume the standing coverage reserve.
        # Direction and passive-investigation tasks are urgent information
        # work even though they carry a search-shaped bbox.
        search_kinds = {"search"}
        ordinary = [
            task for task in normalized
            if getattr(task, "kind", "search") in search_kinds
        ]
        ordinary_ids = {task.task_id for task in ordinary}
        urgent = [task for task in normalized if task.task_id not in ordinary_ids]
        reserve_count = min(ordinary_reserve, capacity, len(ordinary))
        urgent_count = min(capacity - reserve_count, len(urgent))

        if zones is not None:
            # Scan each zone until a usable candidate is found; blocked rounds
            # must not hide later non-overlapping work. Contained candidates
            # precede cross-tile candidates so quotas remain representable.
            # Visit zones in the order of their highest-ranked candidate.
            # Sorting by zone ID would let fresh low-ID zones displace unseen
            # or overdue work when the window is smaller than the zone count.
            grouped = {}
            for task in ordinary:
                if task.bbox is not None:
                    zone = zones.zone_of_bbox(task.bbox)
                    if zone is not None:
                        grouped.setdefault(zone, []).append(task)
            for zone, tasks in grouped.items():
                tasks.sort(key=lambda task: not zones.contains_bbox(zone, task.bbox))
            indices = dict.fromkeys(grouped, 0)
            rotated, boxes = [], []
            while any(indices[z] < len(grouped[z]) for z in grouped):
                for zone, tasks in grouped.items():
                    while indices[zone] < len(tasks):
                        task = tasks[indices[zone]]
                        indices[zone] += 1
                        if any(_boxes_overlap(task.bbox, box) for box in boxes):
                            continue
                        rotated.append(task)
                        boxes.append(task.bbox)
                        break
            ordinary = rotated

        representative_ordinary: list[Any] = []
        representative_boxes: list[tuple[float, float, float, float]] = []
        for task in ordinary:
            if len(representative_ordinary) >= reserve_count:
                break
            bbox = getattr(task, "bbox", None)
            if bbox is None:
                continue
            normalized_bbox = tuple(float(value) for value in bbox)
            if any(_boxes_overlap(normalized_bbox, other) for other in representative_boxes):
                continue
            representative_ordinary.append(task)
            representative_boxes.append(normalized_bbox)
        selected = [*urgent[:urgent_count], *representative_ordinary]
        selected_ids = {task.task_id for task in selected}

        def task_box(task: Any) -> tuple[float, float, float, float] | None:
            bbox = getattr(task, "bbox", None)
            if bbox is None:
                return None
            return tuple(float(value) for value in bbox)

        selected_boxes = [
            box for task in selected if (box := task_box(task)) is not None
        ]

        # Keep the prompt useful after the representative reserve.  A dense
        # candidate pool often contains many overlapping windows around the
        # same representative; spending the remaining capacity on those
        # windows leaves otherwise healthy UAVs without legal successor work.
        for task in normalized:
            if len(selected) >= capacity or task.task_id in selected_ids:
                continue
            box = task_box(task)
            if task.task_id not in ordinary_ids or box is None:
                continue
            if any(_boxes_overlap(box, other) for other in selected_boxes):
                continue
            selected.append(task)
            selected_ids.add(task.task_id)
            selected_boxes.append(box)

        for task in normalized:
            if len(selected) >= capacity:
                break
            if task.task_id in selected_ids or task.task_id in ordinary_ids:
                continue
            selected.append(task)
            selected_ids.add(task.task_id)

        ordinary_selected = [
            task for task in selected if task.task_id in ordinary_ids
        ]
        representatives: list[str] = []
        representative_boxes: list[tuple[float, float, float, float]] = []
        for task in ordinary_selected:
            bbox = getattr(task, "bbox", None)
            if bbox is None:
                continue
            normalized_bbox = tuple(float(value) for value in bbox)
            if any(_boxes_overlap(normalized_bbox, other) for other in representative_boxes):
                continue
            representatives.append(task.task_id)
            representative_boxes.append(normalized_bbox)

        sources = []
        urgent_ids = {task.task_id for task in urgent}
        for task in selected:
            source = "urgent" if task.task_id in urgent_ids else "ordinary"
            sources.append((task.task_id, source))
        return CoverageCandidateWindow(
            tasks=tuple(selected),
            representative_task_ids=tuple(representatives),
            sources=tuple(sources),
        )


def adaptive_search_fraction(gap_pct: float, minimum: float = 0.4, maximum: float = 1.0) -> float:
    """Interpolate the ordinary-search share from actual whole-domain SAR gap."""
    for value in (gap_pct, minimum, maximum):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError("coverage fractions require finite numbers")
    if not 0 <= gap_pct <= 100 or not 0 < minimum <= maximum <= 1:
        raise ValueError("invalid gap or search fraction range")
    return float(minimum + (maximum - minimum) * gap_pct / 100)


def build_coverage_constraint(
    *,
    active_search_count: int,
    reserved_search_count: int | None = None,
    matchable_pending_count: int = 0,
    available_ids: Iterable[str],
    representatives: Iterable[Any],
    edges: Iterable[Any],
    fraction: float,
    zone_requirements_input=(),
    healthy_count: int | None = None,
):
    """Build a healthy-fleet budget with one quota-first maximum matching.

    The return type is imported lazily to keep this policy module independent
    from the broader mission contract graph.
    ``healthy_count`` anchors the desired total search count. ``available_ids``
    remains the idle, legally matchable set for new work; active search work is
    subtracted only from the desired total, never from the healthy denominator.
    When omitted, the idle set remains the compatibility denominator.
    """
    from src.mission.contracts import CoverageConstraint, ZoneCoverageRequirement

    if any(
        isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0
        for value in (
            (active_search_count, matchable_pending_count)
            if healthy_count is None
            else (healthy_count, active_search_count, matchable_pending_count)
        )
    ):
        raise ValueError(
            "healthy_count, active_search_count, and matchable_pending_count "
            "must be non-negative integers"
        )
    if reserved_search_count is None:
        reserved_search_count = int(active_search_count) + int(matchable_pending_count)
    if (
        isinstance(reserved_search_count, bool)
        or not isinstance(reserved_search_count, Integral)
        or int(reserved_search_count) < 0
    ):
        raise ValueError("reserved_search_count must be a non-negative integer")
    reserved_search_count = int(reserved_search_count)
    if int(active_search_count) > reserved_search_count:
        raise ValueError("active_search_count exceeds reserved_search_count")
    if int(active_search_count) + int(matchable_pending_count) > reserved_search_count:
        raise ValueError(
            "matchable_pending_count exceeds reserved pending capacity"
        )
    if isinstance(fraction, bool) or not isinstance(fraction, Real):
        raise ValueError("fraction must be finite and in (0, 1]")
    fraction = float(fraction)
    if not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be finite and in (0, 1]")

    available = tuple(dict.fromkeys(str(item) for item in available_ids))
    available_set = set(available)
    representative_ids = tuple(
        getattr(item, "task_id", item) for item in representatives
    )
    if any(not isinstance(item, str) or not item for item in representative_ids):
        raise ValueError("representatives must contain task IDs or task objects")
    representative_ids = tuple(dict.fromkeys(representative_ids))
    healthy = len(available) if healthy_count is None else int(healthy_count)
    desired = int(math.ceil(healthy * fraction))
    residual_target = max(
        0,
        desired - int(active_search_count) - int(matchable_pending_count),
    )
    quota_inputs = tuple(zone_requirements_input)[:residual_target]
    if len({item.zone_id for item in quota_inputs}) != len(quota_inputs):
        raise ValueError("duplicate quota zone")
    representative_ids = tuple(dict.fromkeys((*representative_ids,
        *(task_id for item in quota_inputs for task_id in item.candidate_task_ids))))
    adjacency = {task_id: [] for task_id in representative_ids}
    minimum_transit: dict[str, float] = {}
    edges = tuple(edges)
    for edge in edges:
        if isinstance(edge, Mapping):
            task_id = edge.get("task_id")
            uav_id = edge.get("uav_id")
            transit = edge.get("transit_time_min")
        else:
            task_id = getattr(edge, "task_id", None)
            uav_id = getattr(edge, "uav_id", None)
            transit = getattr(edge, "transit_time_min", None)
        if task_id in adjacency and uav_id in available_set:
            adjacency[task_id].append(uav_id)
            if isinstance(transit, Real) and math.isfinite(float(transit)):
                minimum_transit[task_id] = min(
                    minimum_transit.get(task_id, float("inf")),
                    float(transit),
                )
    for task_id in adjacency:
        adjacency[task_id] = sorted(set(adjacency[task_id]))

    transit_by_uav: dict[str, dict[str, float]] = {}
    for edge in edges:
        if isinstance(edge, Mapping):
            task_id = edge.get("task_id")
            uav_id = edge.get("uav_id")
            transit = edge.get("transit_time_min")
        else:
            task_id = getattr(edge, "task_id", None)
            uav_id = getattr(edge, "uav_id", None)
            transit = getattr(edge, "transit_time_min", None)
        if (
            task_id in adjacency
            and uav_id in available_set
            and isinstance(transit, Real)
            and math.isfinite(float(transit))
        ):
            transit_by_uav.setdefault(task_id, {})[uav_id] = min(
                transit_by_uav.get(task_id, {}).get(uav_id, float("inf")),
                float(transit),
            )

    def quota_order(task_ids: Iterable[str]) -> tuple[str, ...]:
        """Start short-transit legal work early while preserving deterministic ties."""
        ordered = tuple(task_ids)
        return tuple(
            task_id for _index, task_id in sorted(
                enumerate(ordered),
                key=lambda item: (
                    0 if item[1] in minimum_transit else 1,
                    minimum_transit.get(item[1], float("inf")),
                    item[0],
                ),
            )
        )

    matched_uav: dict[str, str] = {}

    def visit(task_id: str, seen: set[str]) -> bool:
        for uav_id in adjacency[task_id]:
            if uav_id in seen:
                continue
            seen.add(uav_id)
            previous = matched_uav.get(uav_id)
            if previous is None or visit(previous, seen):
                matched_uav[uav_id] = task_id
                return True
        return False

    zone_matched, zone_infeasible = [], []
    matched_tasks = set()

    # Solve the quota subproblem as a min-cost maximum flow.  A greedy zone
    # order can consume the only short route for a later zone, even when a
    # full and cheaper assignment exists.
    quota_graph: list[list[dict[str, Any]]] = []

    def add_quota_node() -> int:
        quota_graph.append([])
        return len(quota_graph) - 1

    def add_quota_edge(
        source: int,
        target: int,
        cost: float,
    ) -> tuple[int, int]:
        forward_index = len(quota_graph[source])
        reverse_index = len(quota_graph[target])
        quota_graph[source].append({
            "to": target,
            "reverse": reverse_index,
            "capacity": 1,
            "cost": float(cost),
        })
        quota_graph[target].append({
            "to": source,
            "reverse": forward_index,
            "capacity": 0,
            "cost": -float(cost),
        })
        return source, forward_index

    quota_source = add_quota_node()
    quota_zone_nodes = [add_quota_node() for _item in quota_inputs]
    quota_task_entry_nodes = {
        task_id: add_quota_node()
        for task_id in representative_ids
    }
    quota_task_exit_nodes = {
        task_id: add_quota_node()
        for task_id in representative_ids
    }
    quota_uav_nodes = {
        uav_id: add_quota_node()
        for uav_id in available
    }
    quota_sink = add_quota_node()
    quota_zone_task_edges: dict[tuple[int, str], tuple[int, int]] = {}
    quota_task_uav_edges: dict[tuple[str, str], tuple[int, int]] = {}
    finite_transits = [
        transit
        for costs in transit_by_uav.values()
        for transit in costs.values()
    ]
    unknown_transit_cost = max(finite_transits, default=0.0) + 1_000_000.0

    for zone_index, item in enumerate(quota_inputs):
        zone_node = quota_zone_nodes[zone_index]
        add_quota_edge(quota_source, zone_node, 0.0)
        for task_id in quota_order(item.candidate_task_ids):
            if task_id not in quota_task_entry_nodes:
                continue
            quota_zone_task_edges[(zone_index, task_id)] = add_quota_edge(
                zone_node,
                quota_task_entry_nodes[task_id],
                0.0,
            )
    for task_id in representative_ids:
        # A representative can fulfill at most one zone quota, even when
        # a boundary candidate appears in more than one zone input.
        add_quota_edge(
            quota_task_entry_nodes[task_id],
            quota_task_exit_nodes[task_id],
            0.0,
        )
        task_node = quota_task_exit_nodes[task_id]
        for uav_id in adjacency[task_id]:
            transit = transit_by_uav.get(task_id, {}).get(
                uav_id,
                unknown_transit_cost,
            )
            quota_task_uav_edges[(task_id, uav_id)] = add_quota_edge(
                task_node,
                quota_uav_nodes[uav_id],
                transit,
            )
    for uav_node in quota_uav_nodes.values():
        add_quota_edge(uav_node, quota_sink, 0.0)

    while True:
        distances = [float("inf")] * len(quota_graph)
        previous: list[tuple[int, int] | None] = [None] * len(quota_graph)
        distances[quota_source] = 0.0
        for _iteration in range(len(quota_graph) - 1):
            changed = False
            for node, node_distance in enumerate(distances):
                if not math.isfinite(node_distance):
                    continue
                for edge_index, edge in enumerate(quota_graph[node]):
                    if edge["capacity"] <= 0:
                        continue
                    candidate_distance = node_distance + edge["cost"]
                    if candidate_distance < distances[edge["to"]] - 1e-12:
                        distances[edge["to"]] = candidate_distance
                        previous[edge["to"]] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if previous[quota_sink] is None:
            break
        node = quota_sink
        while node != quota_source:
            source, edge_index = previous[node]
            edge = quota_graph[source][edge_index]
            edge["capacity"] = 0
            quota_graph[node][edge["reverse"]]["capacity"] = 1
            node = source

    for zone_index, item in enumerate(quota_inputs):
        selected_task = None
        selected_uav = None
        for task_id in quota_order(item.candidate_task_ids):
            handle = quota_zone_task_edges.get((zone_index, task_id))
            if handle is None:
                continue
            source, edge_index = handle
            if quota_graph[source][edge_index]["capacity"] != 0:
                continue
            for uav_id in adjacency[task_id]:
                task_handle = quota_task_uav_edges[(task_id, uav_id)]
                task_source, task_edge_index = task_handle
                if quota_graph[task_source][task_edge_index]["capacity"] == 0:
                    selected_task = task_id
                    selected_uav = uav_id
                    break
            if selected_task is not None:
                break
        if selected_task is None or selected_uav is None:
            zone_infeasible.append((item.zone_id, "no_feasible_zone_representative"))
            continue
        matched_tasks.add(selected_task)
        matched_uav[selected_uav] = selected_task
        zone_matched.append(ZoneCoverageRequirement(
            item.zone_id, 1, item.candidate_task_ids, (selected_task,)))
    for task_id in representative_ids:
        if task_id not in matched_tasks and visit(task_id, set()):
            matched_tasks.add(task_id)
    feasible_slots = len(matched_uav)
    required = min(residual_target, feasible_slots)
    infeasible_reason = (
        None
        if int(active_search_count) + int(matchable_pending_count) + feasible_slots
        >= desired
        else "insufficient_available_resources"
    )
    # Quota witnesses already consume budget; never add an incompatible extra
    # oldest obligation when every budget slot belongs to a different zone.
    must_service = tuple(task for zone in zone_matched for task in zone.must_service_task_ids)
    if not must_service and required:
        must_service = (next(task for task in representative_ids if task in matched_tasks),)
    return CoverageConstraint(
        desired_search_count=desired,
        active_search_count=int(active_search_count),
        required_new_search_count=required,
        representative_task_ids=representative_ids,
        must_service_task_ids=must_service,
        infeasible_reason=infeasible_reason,
        zone_requirements=tuple(zone_matched),
        zone_infeasible=tuple(zone_infeasible),
        reserved_search_count=reserved_search_count,
        matchable_pending_count=int(matchable_pending_count),
    )


def rank_search_candidates(
    candidates: Iterable[Any],
    *,
    now_min: float,
    last_sar: np.ndarray,
    estimated_minutes: Mapping[str, Real],
    primary_window_min: int = 60,
) -> tuple[Any, ...]:
    """Rank search candidates without changing their identity or order data.

    The key is ``(unseen first, oldest due time, regular work, due density,
    shorter task, bbox)``.  An unseen cell has episode age zero; otherwise age
    is represented by its actual SAR timestamp.  Residual ``fragment:*``
    candidates are deliberately considered after complete search rectangles
    at the same freshness so short edge strips do not consume a sortie before
    the broad domain pass is complete.  Missing route estimates use the
    candidate cell count as a conservative one-cell-per-minute estimate.
    """
    now = _time(now_min, "now_min")
    if (
        isinstance(primary_window_min, bool)
        or not isinstance(primary_window_min, Integral)
        or int(primary_window_min) <= 0
    ):
        raise ValueError("primary_window_min must be a positive integer")
    primary_window_min = int(primary_window_min)
    last = _last_sar(last_sar, np.asarray(last_sar).shape, now)
    if not isinstance(estimated_minutes, Mapping):
        raise ValueError("estimated_minutes must be a mapping")

    normalized: list[
        tuple[Any, str, tuple[int, int, int, int], tuple[tuple[int, int], ...], np.ndarray]
    ] = []
    for candidate in tuple(candidates):
        task_id = _candidate_value(candidate, "task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("candidate task_id must be a non-empty string")
        bbox = _bbox(_candidate_value(candidate, "bbox"), task_id)
        cells = _candidate_cells(candidate, bbox, last.shape, task_id)
        normalized.append((
            candidate,
            task_id,
            bbox,
            cells,
            np.asarray(cells, dtype=np.intp),
        ))

    ranked = sorted(
        normalized,
        key=lambda item: _rank_key_vectorized(
            item[1],
            item[2],
            item[3],
            item[4],
            now,
            last,
            estimated_minutes,
            primary_window_min,
        ),
    )
    return tuple(item[0] for item in ranked)


def _rank_key(
    task_id: str,
    bbox: tuple[int, int, int, int],
    cells: tuple[tuple[int, int], ...],
    now: float,
    last: np.ndarray,
    estimated_minutes: Mapping[str, Real],
    primary_window_min: int,
) -> tuple[int, float, int, float, int, tuple[int, int, int, int]]:
    return _rank_key_vectorized(
        task_id,
        bbox,
        cells,
        np.asarray(cells, dtype=np.intp),
        now,
        last,
        estimated_minutes,
        primary_window_min,
    )


def _rank_key_vectorized(
    task_id: str,
    bbox: tuple[int, int, int, int],
    cells: tuple[tuple[int, int], ...],
    coordinates: np.ndarray,
    now: float,
    last: np.ndarray,
    estimated_minutes: Mapping[str, Real],
    primary_window_min: int,
) -> tuple[int, float, int, float, int, tuple[int, int, int, int]]:
    timestamps = last[coordinates[:, 0], coordinates[:, 1]]
    unseen = ~np.isfinite(timestamps)
    due = unseen | (timestamps <= now - primary_window_min)
    unseen_count = int(np.count_nonzero(unseen))
    due_count = int(np.count_nonzero(due))
    if unseen_count:
        oldest_due = 0.0
    elif due_count:
        oldest_due = float(np.min(timestamps[due]))
    else:
        oldest_due = now

    estimate = estimated_minutes.get(task_id)
    if estimate is None:
        estimate_value = float(max(1, len(cells)))
    else:
        if isinstance(estimate, bool) or not isinstance(estimate, Real):
            raise ValueError(f"estimated_minutes[{task_id!r}] must be finite and positive")
        estimate_value = float(estimate)
        if not math.isfinite(estimate_value) or estimate_value <= 0:
            raise ValueError(f"estimated_minutes[{task_id!r}] must be finite and positive")

    density = due_count / max(estimate_value, 1e-6)
    fragment = int(task_id.startswith("fragment:"))
    return (
        0 if unseen_count else 1,
        oldest_due,
        fragment,
        -density,
        len(cells),
        bbox,
    )


def _boxes_overlap(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return not (
        left[2] <= right[0]
        or right[2] <= left[0]
        or left[3] <= right[1]
        or right[3] <= left[1]
    )


def _candidate_value(candidate: Any, name: str) -> Any:
    if isinstance(candidate, Mapping):
        if name not in candidate:
            raise ValueError(f"candidate missing {name}")
        return candidate[name]
    try:
        return getattr(candidate, name)
    except AttributeError as exc:
        raise ValueError(f"candidate missing {name}") from exc


def _candidate_cells(
    candidate: Any,
    bbox: tuple[int, int, int, int],
    shape: tuple[int, ...],
    task_id: str,
) -> tuple[tuple[int, int], ...]:
    raw_cells: Any
    if isinstance(candidate, Mapping):
        raw_cells = candidate.get("cells")
    else:
        raw_cells = getattr(candidate, "cells", None)
    if raw_cells is None:
        raw_cells = tuple(
            (col, row)
            for col in range(bbox[0], bbox[2])
            for row in range(bbox[1], bbox[3])
        )

    try:
        cells = tuple(raw_cells)
    except TypeError as exc:
        raise ValueError(f"candidate {task_id!r} cells must be iterable") from exc
    if not cells:
        raise ValueError(f"candidate {task_id!r} must contain at least one cell")

    cols, rows = shape
    try:
        coordinates = np.asarray(cells)
    except (TypeError, ValueError):
        coordinates = np.asarray((), dtype=object)
    if (
        coordinates.ndim == 2
        and coordinates.shape[1] == 2
        and np.issubdtype(coordinates.dtype, np.integer)
    ):
        if (
            np.any(coordinates[:, 0] < 0)
            or np.any(coordinates[:, 0] >= cols)
            or np.any(coordinates[:, 1] < 0)
            or np.any(coordinates[:, 1] >= rows)
        ):
            raise ValueError(f"candidate {task_id!r} cell is outside last_sar")
        encoded = coordinates[:, 0] * rows + coordinates[:, 1]
        if np.unique(encoded).size != len(cells):
            raise ValueError(f"candidate {task_id!r} contains duplicate cells")
        if isinstance(cells, tuple) and all(
            isinstance(cell, tuple)
            and len(cell) == 2
            and all(isinstance(value, (int, np.integer)) for value in cell)
            for cell in cells
        ):
            return cells
        return tuple(
            (int(col), int(row))
            for col, row in coordinates.tolist()
        )

    normalized: list[tuple[int, int]] = []
    for cell in cells:
        if not isinstance(cell, (tuple, list)) or len(cell) != 2:
            raise ValueError(f"candidate {task_id!r} has invalid cell")
        col, row = cell
        if (
            isinstance(col, bool)
            or isinstance(row, bool)
            or not isinstance(col, Integral)
            or not isinstance(row, Integral)
            or not 0 <= int(col) < cols
            or not 0 <= int(row) < rows
        ):
            raise ValueError(f"candidate {task_id!r} cell is outside last_sar")
        normalized.append((int(col), int(row)))
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"candidate {task_id!r} contains duplicate cells")
    return tuple(normalized)


def _bbox(value: Any, task_id: str) -> tuple[int, int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError(f"candidate {task_id!r} bbox must contain four integers")
    values = tuple(value)
    if any(isinstance(item, bool) or not isinstance(item, Integral) for item in values):
        raise ValueError(f"candidate {task_id!r} bbox must contain four integers")
    result = tuple(int(item) for item in values)
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError(f"candidate {task_id!r} bbox must have positive area")
    return result


def _mask(value: Any, name: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=bool)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a two-dimensional mask") from exc
    if array.ndim != 2:
        raise ValueError(f"{name} must be a two-dimensional mask")
    return array


def _mask_like(value: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    array = _mask(value, name)
    if array.shape != shape:
        raise ValueError(f"{name} shape must match fixed_mask")
    return array.copy()


def _last_sar(value: Any, shape: tuple[int, ...], now: float) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("last_sar must be a numeric two-dimensional matrix") from exc
    if array.ndim != 2:
        raise ValueError("last_sar must be a numeric two-dimensional matrix")
    if array.shape != shape:
        raise ValueError("last_sar shape must match fixed_mask")
    if np.isnan(array).any() or np.isposinf(array).any():
        raise ValueError("last_sar must contain finite non-negative times or -inf")
    finite = np.isfinite(array)
    if np.any(array[finite] < 0) or np.any(array[finite] > now):
        raise ValueError("last_sar timestamps must be between zero and now_min")
    return array.copy()


def _time(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


__all__ = [
    "CoverageCandidateWindow",
    "CoveragePolicy",
    "build_coverage_constraint",
    "adaptive_search_fraction",
    "rank_search_candidates",
]

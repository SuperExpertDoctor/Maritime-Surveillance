"""Multi-UAV path conflict detection and resolution.

Checks planned paths across the fleet for spatiotemporal conflicts
(two airframes occupying the same cell within the same time window)
and resolves them by replanning the lower-priority UAV.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class PathConflict:
    uav_a: str
    uav_b: str
    cell: tuple[int, int]
    step_offset_a: int
    step_offset_b: int
    distance_cells: float


@dataclass
class ConflictReport:
    conflicts: list[PathConflict] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)  # uav_ids that were replanned


def detect_conflicts(
    uavs: Sequence[dict],
    cell_size_km: float = 10.0,
    time_horizon_steps: int = 30,
    min_separation_cells: float = 0.5,
    min_prediction_offset: int = 5,
) -> list[PathConflict]:
    """Detect spatiotemporal conflicts across UAV planned paths.

    Args:
        uavs: List of dicts, each with at least ``id``, ``status``,
              ``planned_path`` (list of (col, row, heading) poses).
        cell_size_km: Grid cell size in km (for distance checks).
        time_horizon_steps: Maximum number of future steps to check.
        min_separation_cells: Minimum allowed separation between two
            airframes (0.5 = same-cell, 1.0 = adjacent cells).

    Returns:
        List of detected PathConflict objects.
    """
    active = [
        uav for uav in uavs
        if uav.get("status") not in ("idle", "refueling", "holding")
        and uav.get("planned_path")
    ]
    if len(active) < 2:
        return []

    # Build per-step occupancy maps (step_offset → cell → [uav_ids])
    occupancy: dict[int, dict[tuple[int, int], list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for uav in active:
        path = uav["planned_path"]
        for offset, pose in enumerate(path[:time_horizon_steps]):
            # Offset zero is the UAV's already-occupied current pose.  A
            # common base launch or crossing at that instant cannot be
            # resolved by resetting a route; only future conflicts with
            # enough lead time are actionable.
            if offset < min_prediction_offset:
                continue
            cell = (int(round(pose[0])), int(round(pose[1])))
            occupancy[offset][cell].append(uav["id"])

    conflicts: list[PathConflict] = []
    seen_pairs: set[tuple[str, str]] = set()

    for offset in sorted(occupancy):
        for cell, uav_ids in occupancy[offset].items():
            if len(uav_ids) < 2:
                continue
            for i in range(len(uav_ids)):
                for j in range(i + 1, len(uav_ids)):
                    pair = tuple(sorted((uav_ids[i], uav_ids[j])))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)

                    # Compute actual distance at this step
                    uav_a = next(u for u in active if u["id"] == uav_ids[i])
                    uav_b = next(u for u in active if u["id"] == uav_ids[j])
                    pos_a = uav_a["planned_path"][offset][:2] if offset < len(uav_a["planned_path"]) else None
                    pos_b = uav_b["planned_path"][offset][:2] if offset < len(uav_b["planned_path"]) else None
                    dist = math.dist(pos_a, pos_b) if pos_a and pos_b else 0.0

                    if dist < min_separation_cells:
                        conflicts.append(PathConflict(
                            uav_a=uav_ids[i],
                            uav_b=uav_ids[j],
                            cell=cell,
                            step_offset_a=offset,
                            step_offset_b=offset,
                            distance_cells=round(dist, 3),
                        ))

    return conflicts


def resolve_conflicts(
    conflicts: list[PathConflict],
    uav_entities: dict,
    priority_key: str = "fuel_remaining_pct",
) -> list[str]:
    """Resolve detected conflicts by selecting which UAV to replan.

    Strategy: for each conflicting pair, the UAV with lower priority
    (lower fuel / later assignment) is flagged for replanning.
    The other UAV keeps its current path.

    Args:
        conflicts: Detected conflicts from ``detect_conflicts``.
        uav_entities: Dict of uav_id → UAVEntity for priority comparison.
        priority_key: Attribute to compare for priority (higher = keep path).

    Returns:
        List of uav_ids that should be replanned.
    """
    replan: set[str] = set()
    handled_pairs: set[tuple[str, str]] = set()

    for conflict in conflicts:
        pair = tuple(sorted((conflict.uav_a, conflict.uav_b)))
        if pair in handled_pairs:
            continue
        handled_pairs.add(pair)

        entity_a = uav_entities.get(conflict.uav_a)
        entity_b = uav_entities.get(conflict.uav_b)

        priority_a = getattr(entity_a, priority_key, 0.5) if entity_a else 0.5
        priority_b = getattr(entity_b, priority_key, 0.5) if entity_b else 0.5

        # Tracking UAVs get priority over searching/transit UAVs
        status_a = entity_a.status if entity_a else "idle"
        status_b = entity_b.status if entity_b else "idle"
        if status_a == "tracking" and status_b != "tracking":
            priority_a = 2.0  # absolute priority
        elif status_b == "tracking" and status_a != "tracking":
            priority_b = 2.0

        if priority_a >= priority_b:
            replan.add(conflict.uav_b)
        else:
            replan.add(conflict.uav_a)

    return sorted(replan)


__all__ = ["ConflictReport", "PathConflict", "detect_conflicts", "resolve_conflicts"]

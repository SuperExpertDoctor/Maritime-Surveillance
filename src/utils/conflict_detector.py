"""Multi-UAV path conflict detection and resolution.

Checks planned paths across the fleet for spatiotemporal conflicts
(two airframes occupying the same cell within the same time window)
and resolves them by replanning the lower-priority UAV.
"""
from __future__ import annotations

import math
import re
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
    min_prediction_offset: int = 1,
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

    conflicts: list[PathConflict] = []
    for index, uav_a in enumerate(active):
        for uav_b in active[index + 1:]:
            path_a = uav_a["planned_path"][:time_horizon_steps]
            path_b = uav_b["planned_path"][:time_horizon_steps]
            horizon = min(len(path_a), len(path_b))
            for offset in range(min_prediction_offset, horizon):
                current_a = path_a[offset][:2]
                current_b = path_b[offset][:2]
                distance = math.dist(current_a, current_b)
                closest = (
                    ((current_a[0] + current_b[0]) / 2.0),
                    ((current_a[1] + current_b[1]) / 2.0),
                )

                # A same-tick comparison alone misses two UAVs exchanging
                # sides between adjacent samples.  Measure the minimum
                # separation of their simultaneous segments as well so a
                # crossing in transit or inside a scan region is actionable.
                if offset > 0:
                    previous_a = path_a[offset - 1][:2]
                    previous_b = path_b[offset - 1][:2]
                    segment_distance, segment_point = _moving_segment_distance(
                        previous_a, current_a, previous_b, current_b
                    )
                    if segment_distance < distance:
                        distance, closest = segment_distance, segment_point

                if distance < min_separation_cells:
                    conflicts.append(PathConflict(
                        uav_a=uav_a["id"],
                        uav_b=uav_b["id"],
                        cell=(int(round(closest[0])), int(round(closest[1]))),
                        step_offset_a=offset,
                        step_offset_b=offset,
                        distance_cells=round(distance, 3),
                    ))
                    # One earliest conflict is sufficient to choose a
                    # deconfliction action for this pair this planning cycle.
                    break

    return conflicts


def _moving_segment_distance(
    start_a: Sequence[float],
    end_a: Sequence[float],
    start_b: Sequence[float],
    end_b: Sequence[float],
) -> tuple[float, tuple[float, float]]:
    """Closest separation of two linearly moving UAVs in one time slice."""
    relative_start = (start_a[0] - start_b[0], start_a[1] - start_b[1])
    relative_delta = (
        (end_a[0] - start_a[0]) - (end_b[0] - start_b[0]),
        (end_a[1] - start_a[1]) - (end_b[1] - start_b[1]),
    )
    denominator = relative_delta[0] ** 2 + relative_delta[1] ** 2
    if denominator <= 1e-12:
        ratio = 0.0
    else:
        ratio = max(0.0, min(1.0, -(
            relative_start[0] * relative_delta[0]
            + relative_start[1] * relative_delta[1]
        ) / denominator))
    point_a = (
        start_a[0] + (end_a[0] - start_a[0]) * ratio,
        start_a[1] + (end_a[1] - start_a[1]) * ratio,
    )
    point_b = (
        start_b[0] + (end_b[0] - start_b[0]) * ratio,
        start_b[1] + (end_b[1] - start_b[1]) * ratio,
    )
    return math.dist(point_a, point_b), (
        (point_a[0] + point_b[0]) / 2.0,
        (point_a[1] + point_b[1]) / 2.0,
    )


def resolve_conflicts(
    conflicts: list[PathConflict],
    uav_entities: dict,
    priority_key: str = "uav_id",
) -> list[str]:
    """Resolve detected conflicts by selecting which UAV to replan.

    Strategy: for each conflicting pair, the numerically larger UAV ID has
    priority.  The lower-ID airframe is flagged to yield and keeps its task
    assignment while the main thread inserts a continuous detour.

    Args:
        conflicts: Detected conflicts from ``detect_conflicts``.
        uav_entities: Dict of uav_id → UAVEntity for priority comparison.
        priority_key: Retained for compatibility; UAV ID priority is always used.

    Returns:
        List of uav_ids that should be replanned.
    """
    replan: set[str] = set()
    handled_pairs: set[tuple[str, str]] = set()
    # This result deliberately ignores mutable aircraft state so every
    # lockstep tick makes the same priority decision.
    _ = uav_entities, priority_key

    for conflict in conflicts:
        pair = tuple(sorted((conflict.uav_a, conflict.uav_b)))
        if pair in handled_pairs:
            continue
        handled_pairs.add(pair)

        if uav_id_priority(conflict.uav_a) >= uav_id_priority(conflict.uav_b):
            replan.add(conflict.uav_b)
        else:
            replan.add(conflict.uav_a)

    return sorted(replan)


def uav_id_priority(uav_id: str) -> tuple[int, str]:
    """Return a deterministic priority where a larger numeric UAV ID wins."""
    matches = re.findall(r"\d+", str(uav_id))
    return (int(matches[-1]) if matches else -1, str(uav_id))


__all__ = [
    "ConflictReport", "PathConflict", "detect_conflicts", "resolve_conflicts",
    "uav_id_priority",
]

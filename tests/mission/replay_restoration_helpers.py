"""Test-only fixtures shared by replay restoration scenarios."""
from __future__ import annotations

from src.mission.contracts import Assignment, AssignmentBatch


def probe_batch(engine, count=6):
    """Build an independent probe batch without changing engine state."""
    snapshot = engine.allocator.build_mission_snapshot(engine.clock.time)
    generations = dict(snapshot.uav_generations)
    used = set()
    items = []
    for candidate in snapshot.candidates:
        if candidate.kind != "probe":
            continue
        edge = next(
            (
                edge
                for edge in snapshot.feasible_edges
                if edge.task_id == candidate.task_id and edge.uav_id not in used
            ),
            None,
        )
        if edge is None:
            continue
        used.add(edge.uav_id)
        items.append(
            Assignment(
                candidate.task_id,
                edge.uav_id,
                generations[edge.uav_id],
                None,
            )
        )
        if len(items) == count:
            break
    assert len(items) == count, (
        "fixture cannot build the requested independent probe batch"
    )
    return AssignmentBatch(
        snapshot.snapshot_id,
        tuple(items),
        "fixture-probe-batch",
    )


__all__ = ["probe_batch"]

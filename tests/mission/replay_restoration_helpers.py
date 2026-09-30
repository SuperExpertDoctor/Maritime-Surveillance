"""Test-only fixtures shared by replay restoration scenarios."""
from __future__ import annotations

from src.mission.contracts import Assignment, AssignmentBatch, VisualDetection


def _probe_eligible_count(engine) -> int:
    contacts = engine.allocator.sm.contacts
    return len([
        contact for contact in contacts.list_snapshots()
        if contact.state == "pending" and contact.vessel_class == "unknown"
    ])


def _ensure_probe_contacts(engine, count: int) -> None:
    """Inject synthetic visual detections until enough pending contacts exist.

    Ship-population caps (``max_active``) make organic contact growth too slow
    for fixtures that need several independent probe tasks, so the fixture
    feeds the contact boundary the same VisualDetection shape SAR ingest uses.
    Positions are spread well beyond the association gate on searchable water.
    """
    sm = engine.allocator.sm
    contacts = sm.contacts
    gate = max(3.0, float(getattr(contacts.config, "association_gate_cells", 3.0)) + 1.0)
    spacing = int(gate) + 2
    mask = sm.get_searchable_mask()
    land = getattr(sm, "land_mask", None)
    now = sm.current_time
    injected = 0
    col, row = 6, 6
    while _probe_eligible_count(engine) < count:
        if land is not None and land[col, row]:
            col += 1
            continue
        if not mask[col, row]:
            row += 1
            if row >= mask.shape[1]:
                col, row = col + 1, 6
            continue
        position = (float(col), float(row))
        contacts.ingest_visual(VisualDetection(
            sample_id=f"fixture-probe-{injected}",
            observed_at_min=now,
            source="sar",
            source_id="fixture",
            position_cells=position,
            velocity_cells_min=None,
            position_uncertainty_cells=1.0,
            observer_position_cells=position,
            measured_range_cells=1.0,
            navigation_context="open_water",
        ))
        injected += 1
        row += spacing
        if row >= mask.shape[1]:
            col, row = col + 1, 6
        assert col < mask.shape[0], "fixture ran out of water cells for contacts"


def probe_batch(engine, count=6):
    """Build an independent probe batch without changing engine state."""
    _ensure_probe_contacts(engine, count)
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
                if (
                    edge.task_id == candidate.task_id
                    and edge.uav_id not in used
                    and edge.uav_id in snapshot.available_uav_ids
                )
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

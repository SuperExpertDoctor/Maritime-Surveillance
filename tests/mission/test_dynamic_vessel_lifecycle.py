from itertools import count
import math

import pytest

from src.control.common.contracts import ControlTask, OperationMode
from src.mission.contracts import ProbeSession, TaskRecord, VesselCommand


_COMMANDS = count(1)


def enqueue_and_step(engine, command):
    """Submit a vessel command through the public simulation boundary."""
    engine.vessel_commands.enqueue(command)
    engine.step()
    result = engine.vessel_command_result(command.command_id)
    assert result is not None
    assert result.status in {"applied", "rejected"}
    return result


def apply_create(engine, *, vessel_class, position):
    command = VesselCommand(
        command_id=f"dynamic-create-{next(_COMMANDS)}",
        episode_id=engine.episode_id,
        operation="create",
        vessel_class=vessel_class,
        position_cells=position,
    )
    return enqueue_and_step(engine, command)


def apply_ais(engine, vessel_id, revision, enabled):
    command = VesselCommand(
        command_id=f"dynamic-ais-{next(_COMMANDS)}",
        episode_id=engine.episode_id,
        operation="set_ais",
        vessel_id=vessel_id,
        expected_revision=revision,
        ais_enabled=enabled,
    )
    return enqueue_and_step(engine, command)


def apply_delete(engine, vessel_id, revision):
    command = VesselCommand(
        command_id=f"dynamic-delete-{next(_COMMANDS)}",
        episode_id=engine.episode_id,
        operation="delete",
        vessel_id=vessel_id,
        expected_revision=revision,
    )
    return enqueue_and_step(engine, command)


def _free_position(engine):
    cols, rows = engine.config.grid.resolution
    for col in range(2, cols - 2):
        for row in range(2, rows - 2):
            position = (col + 0.5, row + 0.5)
            cell = (col, row)
            if engine.ship_land_mask[cell] or engine.obstacle_mask[cell]:
                continue
            if any(
                math.dist(position, ship.float_position) < 1.0
                for ship in engine.ships
            ):
                continue
            return position
    raise AssertionError("fixture has no free water position")


def test_runtime_create_toggle_delete_is_atomic(scenario_factory):
    engine = scenario_factory.engine("mixed-ais", seed=42)
    engine.step()

    created = apply_create(engine, vessel_class="type_ii", position=(12.5, 8.5))
    assert created.status == "applied"
    changed = apply_ais(engine, created.vessel_id, created.revision, False)
    assert changed.status == "applied"
    removed = apply_delete(engine, created.vessel_id, changed.revision)
    assert removed.status == "applied"

    assert created.vessel_id not in {ship.id for ship in engine.ships}
    assert created.vessel_id not in engine._ship_position_history
    assert created.vessel_id not in engine._emitter_track_ids
    event_types = [event["type"] for event in engine.allocator.sm.get_recent_events(0)]
    assert "vessel_created" in event_types
    assert "ais_transmission_changed" in event_types
    assert "vessel_removed" in event_types


def test_type_i_cannot_disable_ais_and_keeps_revision(scenario_factory):
    engine = scenario_factory.engine("mixed-ais", seed=42)
    created = apply_create(engine, vessel_class="type_i", position=(12.5, 8.5))

    result = apply_ais(engine, created.vessel_id, created.revision, False)

    assert result.status == "rejected"
    assert result.error_code == "type_i_ais_required"
    assert result.revision == created.revision
    ship = next(ship for ship in engine.ships if ship.id == created.vessel_id)
    assert ship.ais_enabled is True


def test_stale_revision_is_rejected_without_mutating_vessel(scenario_factory):
    engine = scenario_factory.engine("mixed-ais", seed=42)
    created = apply_create(engine, vessel_class="type_ii", position=(12.5, 8.5))
    changed = apply_ais(engine, created.vessel_id, created.revision, False)

    stale = apply_delete(engine, created.vessel_id, created.revision)

    assert stale.status == "rejected"
    assert stale.error_code == "revision_conflict"
    assert stale.revision == changed.revision
    assert any(ship.id == created.vessel_id for ship in engine.ships)


def test_invalid_create_does_not_change_runtime_collections(scenario_factory):
    engine = scenario_factory.engine("mixed-ais", seed=42)
    before = {
        "ships": tuple(ship.id for ship in engine.ships),
        "history": tuple(sorted(engine._ship_position_history)),
        "emitters": tuple(sorted(engine._emitter_track_ids)),
        "revisions": tuple(sorted(engine._vessel_revisions.items())),
    }

    result = apply_create(engine, vessel_class="type_ii", position=(0.0, 0.0))

    assert result.status == "rejected"
    assert result.error_code == "invalid_position"
    assert {
        "ships": tuple(ship.id for ship in engine.ships),
        "history": tuple(sorted(engine._ship_position_history)),
        "emitters": tuple(sorted(engine._emitter_track_ids)),
        "revisions": tuple(sorted(engine._vessel_revisions.items())),
    } == before


def test_delete_releases_probe_track_handoff_and_preserves_evidence(scenario_factory):
    engine = scenario_factory.engine("mixed-ais", seed=42)
    created = apply_create(engine, vessel_class="type_ii", position=(12.5, 8.5))
    vessel_id = created.vessel_id
    sm = engine.allocator.sm
    uav = engine.uavs[0]

    # Bind a real observed contact to the physical vessel only on the
    # evaluator side; the evidence itself belongs to the observation store.
    sm.record_target_observation(vessel_id, next(
        ship for ship in engine.ships if ship.id == vessel_id
    ).position, uav.id, observed_at=engine.clock.time)
    contact_id = sm.resolve_contact_id(vessel_id)
    sm.contacts.reserve(contact_id, uav.id, "probe-1")
    sm.set_probe_session(ProbeSession(
        "probe-1", contact_id, uav.id, "near", engine.clock.time,
        engine.clock.time, engine.clock.time, (), (), 1.0, None,
    ))
    track = sm.create_track_region(contact_id, next(
        ship for ship in engine.ships if ship.id == vessel_id
    ).position)
    track.assigned_uav_id = uav.id
    uav.target_group_id = contact_id
    uav.assigned_region = track.bbox
    task = ControlTask(
        f"track:{contact_id}", OperationMode.TRACK, target_contact_id=contact_id,
    )
    engine._coordinator_tasks[uav.id] = task
    engine._mission_task_records[task.task_id] = TaskRecord(
        task.task_id, "track", "executing", None, contact_id, (), uav.id,
        None, engine.clock.time, engine.clock.time, None, None,
    )
    handoff = engine.handoff_manager.require(
        contact_id, source_uav_id=uav.id, required_at_min=engine.clock.time,
        last_position=tuple(float(value) for value in next(
            ship for ship in engine.ships if ship.id == vessel_id
        ).float_position),
    )
    evidence = engine.handoff_manager.evidence(handoff.handoff_id)

    removed = apply_delete(engine, vessel_id, created.revision)

    assert removed.status == "applied"
    assert sm.get_probe_session("probe-1") is None
    assert sm.get_track_region_for_group(contact_id) is None
    assert uav.target_group_id is None
    assert engine._coordinator_tasks.get(uav.id) is None
    assert engine._mission_task_records[task.task_id].finished_at_min is not None
    assert engine.handoff_manager.attempts()[0].state == "failed"
    assert engine.handoff_manager.evidence(handoff.handoff_id) == evidence


@pytest.mark.parametrize("seed", range(50))
def test_runtime_vessel_motion_stays_in_bounds_across_seeds(scenario_factory, seed):
    engine = scenario_factory.engine("mixed-ais", seed=seed)
    created = apply_create(
        engine, vessel_class="type_ii", position=_free_position(engine),
    )
    assert created.status == "applied"
    vessel = next(ship for ship in engine.ships if ship.id == created.vessel_id)

    for _ in range(480):
        vessel.step(1.0, current_time=float(_ + 1))
        assert 0.0 <= vessel.float_position[0] < engine.config.grid.resolution[0]
        assert 0.0 <= vessel.float_position[1] < engine.config.grid.resolution[1]

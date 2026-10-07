from __future__ import annotations

import numpy as np

from src.control.common.contracts import ControlTask, ControlOwner, OperationMode
from src.env.base_station import BaseStation
from src.env.simulation import SimulationEngine
from src.mission.contracts import ContactSnapshot, ProbeSession, TaskRecord
from src.mission.llm_gateway import ModelResult
from src.mission.task_catalog import TaskCatalog
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord, Region


class OfflineGateway:
    def request_json(self, **_kwargs):
        return ModelResult(
            call_id="offline", success=False, payload=None,
            errors=("offline",), failure_category="transport",
        )

    def request_text(self, **_kwargs):
        return ModelResult(
            call_id="offline-text", success=False, payload=None,
            errors=("offline",), failure_category="transport",
        )


def _engine() -> SimulationEngine:
    return SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=OfflineGateway())


def _coverage_fixture() -> tuple[SimulationEngine, object, ControlTask, int]:
    engine = _engine()
    fixed = engine._intent_searchable_mask()
    col, row = next(
        (col, row)
        for col in range(fixed.shape[0] - 1)
        for row in range(fixed.shape[1])
        if fixed[col, row] and fixed[col + 1, row]
    )
    uav = engine.uavs[0]
    task = ControlTask(
        "failure-fixture", OperationMode.COVERAGE,
        BBox(col, row, col + 2, row + 1),
    )
    lease = engine.control_coordinator.start_work(
        uav.id, sortie_number=1, current_time=0.0, dt_min=1.0, task=task,
    )
    engine._coordinator_tasks[uav.id] = task
    engine.allocator.sm.set_search_regions([
        Region(task.task_id, task.region_bbox, "search", assigned_uav_id=uav.id),
    ])
    engine._mission_task_records[task.task_id] = TaskRecord(
        task.task_id, "search", "executing", tuple(task.region_bbox), None, (),
        uav.id, "fixture", 0.0, 0.0, None, None,
    )
    engine._start_coverage_service_task(uav, task, 0.0, generation=lease.generation)
    return engine, uav, task, lease.generation


def test_base_station_remove_uav_is_idempotent_and_does_not_count_refuel():
    base = BaseStation(GridCoord(2, 3), refuel_time_min=5.0, capacity=1)
    assert base.land_uav("UAV-5")
    assert base.remove_uav("UAV-5") is True
    assert base.remove_uav("UAV-5") is False
    assert base.occupancy == 0
    assert base.hangar == ()
    assert base.refuel_count == 0


def test_failed_uav_is_not_a_scheduler_resource_or_task_edge():
    engine = _engine()
    failed = engine.allocator.sm.get_uav("UAV-1")
    failed.operational_status = "failed"
    failed.failure_reason = "test_failure"

    assert not engine.allocator.sm.is_uav_operational(failed.id)
    assert failed not in engine.allocator.sm.get_available_uavs()
    resources = engine.allocator._mission_resources()
    assert failed.id not in {resource.uav_id for resource in resources}

    class Source:
        def extract(self, *_args, **_kwargs):
            return type("Result", (), {
                "candidate_regions": [{
                    "bbox": BBox(8, 8, 10, 10),
                    "cell_count": 4,
                    "avg_info": 0.2,
                    "total_value": 4.0,
                }],
            })()

    task = TaskCatalog(candidate_extractor=Source()).build(
        engine.allocator.sm, (), (), 0.0,
    )[0]
    assert failed.id not in task.feasible_uav_ids


def test_recovery_wait_park_releases_task_service_and_never_fails():
    engine, uav, task, generation = _coverage_fixture()
    old_position = uav.float_position

    engine._park_for_recovery_retry(uav, 0.0, "no_safe_recovery_path")
    engine._park_for_recovery_retry(uav, 0.0, "no_safe_recovery_path")

    state = engine.allocator.sm.get_uav(uav.id)
    record = engine._mission_task_records[task.task_id]
    assert state.operational_status == "recovery_wait"
    assert uav.status == "holding"
    assert uav.float_position == old_position
    assert record.status == "approved"
    assert record.assigned_uav_id is None
    assert engine.control_coordinator.controller(uav.id) is not None
    assert engine.control_coordinator.operation_mode(uav.id) is OperationMode.HOLDING
    assert engine.control_coordinator.current_lease(uav.id).owner is ControlOwner.SYSTEM
    assert uav.id not in engine._emergency_failures
    assert not engine.allocator.sm.is_uav_operational(uav.id)
    region = engine.allocator.sm.get_search_regions()[0]
    assert region.status == "active"
    assert region.assigned_uav_id is None
    assert engine.allocator.sm.coverage_service.progress(
        task.task_id, generation, uav_id=uav.id,
    ) is not None
    waits = [
        event for event in engine.allocator.sm.get_recent_events(0.0)
        if event["type"] == "recovery_wait"
    ]
    assert len(waits) == 2
    assert not any(
        event["type"] == "emergency_failure"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )


def test_stale_binding_on_grounded_uav_is_released_and_retaskable():
    """A record that names a grounded airframe it is not running must not
    keep that airframe out of the schedulable pool."""
    engine = _engine()
    uav = engine.uavs[0]
    assert uav.status == "idle"
    engine._mission_task_records["probe-leaked"] = TaskRecord(
        "probe-leaked", "probe", "executing", None, "C0007", (),
        uav.id, "fixture", 0.0, 0.0, None, None,
    )

    released = engine._release_stale_task_bindings(5.0)

    assert released == 1
    record = engine._mission_task_records["probe-leaked"]
    assert record.status == "blocked"
    assert record.assigned_uav_id is None
    assert record.release_reason == "stale_binding"
    assert any(
        event["type"] == "mission_task_released"
        and event["data"]["task_id"] == "probe-leaked"
        and event["data"]["reason"] == "stale_binding"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )
    assert uav.id in {
        item.id for item in engine.allocator.sm.get_available_uavs()
    }


def test_release_stale_task_bindings_keeps_matching_active_task():
    """Grounded or not, a record whose task the coordinator is still
    running is a live binding and must survive the sweep."""
    engine, uav, task, _generation = _coverage_fixture()
    uav.status = "idle"

    assert engine._release_stale_task_bindings(0.0) == 0
    record = engine._mission_task_records[task.task_id]
    assert record.status == "executing"
    assert record.assigned_uav_id == uav.id


def test_probe_timeout_releases_record_bound_to_swapped_airframe():
    """A probe session that outlives a return/holding swap must close the
    orphaned record it left bound to the airframe."""
    engine = _engine()
    uav = engine.uavs[0]
    engine._mission_task_records["probe-orphan"] = TaskRecord(
        "probe-orphan", "probe", "executing", None, "C0007", (),
        uav.id, "fixture", 0.0, 0.0, None, None,
    )
    contact = ContactSnapshot(
        "C0007", 1, "queued", "unknown", None, 0.0, 1.0,
        (5.0, 5.0), None, 0.5, uav.id, "P0001", None, None, 0.0, (),
    )
    probe = ProbeSession(
        "P0001", "C0007", uav.id, "finished",
        0.0, 0.0, 0.0, (), (), 0.0, "approach_timeout",
    )

    engine._finish_probe_session(contact, probe, 5.0)

    record = engine._mission_task_records["probe-orphan"]
    assert record.status == "blocked"
    assert record.assigned_uav_id is None
    assert record.release_reason == "approach_timeout"
    assert any(
        event["type"] == "mission_task_released"
        and event["data"]["task_id"] == "probe-orphan"
        and event["data"]["probe_id"] == "P0001"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )


def test_parked_recovery_wait_uav_skips_control_ticks_and_keeps_window():
    """A parked airframe must not fault-and-repark every tick: control
    ticks are skipped and the retry window keeps its original start."""
    engine, uav, task, generation = _coverage_fixture()
    engine._park_for_recovery_retry(uav, 0.0, "no_safe_recovery_path")
    engine._park_for_recovery_retry(uav, 1.0, "controller_fault")
    assert engine._hold_started_at[uav.id] == 0.0

    waits_before = sum(
        event["type"] == "recovery_wait"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )
    assert engine._step_controlled_uav(uav, 2.0) is False
    waits_after = sum(
        event["type"] == "recovery_wait"
        for event in engine.allocator.sm.get_recent_events(0.0)
    )
    assert waits_after == waits_before


def test_control_fault_on_parked_uav_does_not_repark():
    """A further fault while parked leaves the retry loop in charge — no
    fresh recovery_wait event and no reset of the window."""
    engine, uav, task, generation = _coverage_fixture()
    engine._park_for_recovery_retry(uav, 0.0, "no_safe_recovery_path")
    lease = engine.control_coordinator.current_lease(uav.id)

    engine._handle_control_fault(uav, 1.0, RuntimeError("boom"), lease)

    assert engine._hold_started_at[uav.id] == 0.0
    assert sum(
        event["type"] == "recovery_wait"
        for event in engine.allocator.sm.get_recent_events(0.0)
    ) == 1


def test_storm_spawn_never_strands_an_airframe():
    """A spawned storm must not bury an airborne UAV in its safety margin
    nor seal its last corridor to a base — static storms make such traps
    permanent."""
    from src.env.obstacle import Thunderstorm

    engine = _engine()
    uav = engine.uavs[0]
    uav._col, uav._row = 15.5, 10.5
    obstacles = list(engine.obstacles)

    buried = Thunderstorm(
        center=(16.0, 11.0), size=2, move_vector=(0.0, 0.0),
        lifetime=-1.0, intensity=0.5, id="storm-x",
    )
    assert not engine._uavs_keep_escape_corridor(buried, obstacles)

    far = Thunderstorm(
        center=(25.0, 25.0), size=1, move_vector=(0.0, 0.0),
        lifetime=-1.0, intensity=0.5, id="storm-y",
    )
    assert engine._uavs_keep_escape_corridor(far, obstacles)


def test_coverage_install_fault_holds_position_and_cools_retry():
    """A storm-blocked install must not fly the airframe home."""
    engine, uav, task, generation = _coverage_fixture()
    lease = engine.control_coordinator.current_lease(uav.id)
    assert lease.owner is ControlOwner.HEURISTIC

    engine._handle_control_fault(
        uav, 3.0, ValueError("coverage planner produced no obstacle-safe swaths"), lease,
    )

    assert uav.status == "holding"
    assert engine.control_coordinator.operation_mode(uav.id) is OperationMode.HOLDING
    assert engine.control_coordinator.current_lease(uav.id).owner is ControlOwner.SYSTEM
    record = engine._mission_task_records[task.task_id]
    assert record.status == "approved"
    assert record.assigned_uav_id is None
    region = engine.allocator.sm.get_search_regions()[0]
    assert region.status == "active"
    assert region.assigned_uav_id is None
    assert engine._coverage_install_cooldown[task.task_id] == (
        3.0 + engine.config.mission.coverage.install_retry_cooldown_min
    )
    cooled = engine.allocator.build_pending_search_batch(
        3.5,
        active_tasks=tuple(
            record
            for record in engine._mission_task_records.values()
            if engine._coverage_install_cooldown.get(
                record.task_id, float("-inf")
            )
            <= 3.5
        ),
    )
    assert cooled is None or all(
        assignment.task_id != task.task_id for assignment in cooled.assignments
    )

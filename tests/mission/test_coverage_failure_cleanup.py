from __future__ import annotations

import numpy as np

from src.control.common.contracts import ControlTask, ControlOwner, OperationMode
from src.env.base_station import BaseStation
from src.env.simulation import SimulationEngine
from src.mission.contracts import TaskRecord
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


def test_emergency_failure_releases_task_service_and_does_not_return_or_hold():
    engine, uav, task, generation = _coverage_fixture()
    old_position = uav.float_position

    engine._enter_emergency_failure(
        uav, "no_safe_recovery_path", RuntimeError("blocked")
    )
    engine._enter_emergency_failure(
        uav, "no_safe_recovery_path", RuntimeError("duplicate")
    )

    state = engine.allocator.sm.get_uav(uav.id)
    record = engine._mission_task_records[task.task_id]
    assert state.operational_status == "failed"
    assert state.failure_reason == "no_safe_recovery_path"
    assert uav.status == "failed"
    assert uav.float_position == old_position
    assert record.status == "blocked"
    assert record.assigned_uav_id is None
    assert engine.control_coordinator.controller(uav.id) is None
    assert engine.control_coordinator.active_task(uav.id) is None
    assert engine.control_coordinator.current_lease(uav.id).owner is ControlOwner.SYSTEM
    assert engine.control_coordinator.route_snapshot(uav.id).route.status == "cleared"
    assert engine.allocator.sm.get_search_regions()[0].assigned_uav_id is None
    assert engine.allocator.sm.coverage_service.progress(
        task.task_id, generation, uav_id=uav.id,
    ) is not None
    failures = [
        event for event in engine.allocator.sm.get_recent_events(0.0)
        if event["type"] == "emergency_failure"
    ]
    assert len(failures) == 1

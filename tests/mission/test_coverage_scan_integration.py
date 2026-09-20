from dataclasses import replace

import numpy as np
import pytest

from scripts.evaluate_mixed_maritime import _FixtureGateway
from src.control.common.contracts import ControlTask, OperationMode
from src.env.simulation import SimulationEngine
from src.mission.contracts import VisualDetection
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord
from src.schedule.state_manager import StateManager
from tests.mission.coverage_helpers import oracle_coverage


class _FixedSar:
    detection_probability = 0.0

    def __init__(self, cells):
        self.cells = list(cells)

    def compute_swath_footprint(self, *_args, **_kwargs):
        return list(self.cells)


def _engine(*, uav_count=2):
    config = ConfigLoader.load()
    config = replace(
        config,
        environment=replace(
            config.environment,
            island_count_min=0,
            island_count_max=0,
            thunderstorm_count_min=0,
            thunderstorm_count_max=0,
        ),
        uav=replace(config.uav, count_max=uav_count),
    )
    return SimulationEngine(
        config,
        seed=42,
        llm_gateway=_FixtureGateway(),
        episode_id="coverage-sensor-test",
    )


def _window(snapshot, minutes=60):
    return next(item for item in snapshot["windows"] if item["minutes"] == minutes)


def _assert_event_oracle(engine):
    from src.vis.backend.frame_builder import build_frame

    sm = engine.allocator.sm
    events = [
        {"time": event["time"], "source": "sar", "cells": event["data"]["cells"]}
        for event in sm.get_recent_events(0.0) if event["type"] == "sar_scan"
    ]
    frame = build_frame(sm, 0, engine.config, include_matrices=False)
    assert frame["coverage_metrics"]["as_of_min"] == frame["sim_time_min"]
    for window in frame["coverage_metrics"]["windows"]:
        expected = oracle_coverage(
            events, np.argwhere(engine._intent_searchable_mask()),
            now_min=sm.current_time, window_min=window["minutes"],
            cell_size_km=engine.config.grid.cell_size_km,
        )
        assert {key: window[key] for key in expected} == expected


def test_state_manager_coverage_is_opt_in_for_legacy_fixtures():
    state = StateManager(ConfigLoader.load())

    assert state.coverage_metrics is None
    assert state.get_persistent_coverage_stats() is None


def test_real_sensor_boundary_records_only_deduplicated_sar_footprints():
    engine = _engine()
    sm = engine.allocator.sm
    cell = GridCoord(10, 10)
    task_bbox = BBox(5, 5, 25, 25)
    expected_generations = {}

    for index, uav in enumerate(engine.uavs):
        task = ControlTask(
            f"coverage-task-{index + 1}", OperationMode.COVERAGE, task_bbox
        )
        lease = engine.control_coordinator.start_work(
            uav.id,
            sortie_number=1,
            current_time=0.0,
            dt_min=1.0,
            task=task,
        )
        expected_generations[uav.id] = lease.generation
        uav.status = "searching"
        uav.sensor_mode = "sar"
        uav.sar_imaging = True
        uav.sar_look_direction = "right"
        uav.sar_sensor = _FixedSar((cell, cell))

    sm.current_time = 1.0
    engine._update_sensors_and_detections(1.0)

    first = sm.get_persistent_coverage_stats()
    assert first["ever_scanned_cells"] == 1
    assert _window(first)["covered_cells"] == 1
    assert sm.get_last_scan_matrix()[cell.col, cell.row] == 1.0
    _assert_event_oracle(engine)
    sar_events = [
        event for event in sm.get_recent_events(0.0) if event["type"] == "sar_scan"
    ]
    assert len(sar_events) == 2
    assert {event["data"]["uav_id"] for event in sar_events} == {
        uav.id for uav in engine.uavs
    }
    assert all(event["data"]["cells"] == [[cell.col, cell.row]] for event in sar_events)
    from scripts.capture_coverage_baseline import _event_identity

    assert len({
        _event_identity(engine.episode_id, event)
        for event in sar_events * 2
    }) == 2
    assert {
        event["data"]["uav_id"]: (
            event["data"]["task_id"], event["data"]["generation"]
        )
        for event in sar_events
    } == {
        uav.id: (f"coverage-task-{index + 1}", expected_generations[uav.id])
        for index, uav in enumerate(engine.uavs)
    }

    ship = engine.ships[0]
    ship._col, ship._row = float(cell.col), float(cell.row)
    engine.ships = [ship]
    contact_id = sm.contacts.ingest_visual(VisualDetection(
        "EO-COVERAGE-SETUP",
        31.0,
        "eo",
        engine.uavs[0].id,
        (float(cell.col), float(cell.row)),
        (0.0, 0.0),
        0.0,
        (float(cell.col - 1), float(cell.row)),
        1.0,
        "open_water",
    ))
    observer = engine.uavs[0]
    observer._col, observer._row = float(cell.col - 1), float(cell.row)
    observer.heading_rad = 0.0
    observer.status = "tracking"
    observer.sensor_mode = "eo"
    observer.sar_imaging = False
    observer.target_group_id = contact_id
    engine.uavs[1].status = "idle"
    engine.uavs[1].sar_imaging = False

    sm.current_time = 31.0
    info_before_eo = sm.get_info_matrix()[cell.col, cell.row]
    engine._update_sensors_and_detections(31.0)

    assert sm.get_info_matrix()[cell.col, cell.row] > info_before_eo
    assert sm.get_last_scan_matrix()[cell.col, cell.row] == 31.0
    assert sm.coverage_metrics.last_scan_matrix()[cell.col, cell.row] == 1.0
    sm.current_time = 61.0
    expired = sm.get_persistent_coverage_stats()
    assert expired["ever_scanned_cells"] == 1
    assert _window(expired)["covered_cells"] == 0
    _assert_event_oracle(engine)
    assert len([
        event for event in sm.get_recent_events(0.0) if event["type"] == "sar_scan"
    ]) == 2

    observer.status = "searching"
    observer.sensor_mode = "sar"
    observer.sar_imaging = True
    sm.current_time = 62.0
    engine._update_sensors_and_detections(62.0)

    refreshed = sm.get_persistent_coverage_stats()
    assert refreshed["ever_scanned_cells"] == 1
    assert _window(refreshed)["covered_cells"] == 1
    assert sm.coverage_metrics.last_scan_matrix()[cell.col, cell.row] == 62.0
    _assert_event_oracle(engine)
    assert len([
        event for event in sm.get_recent_events(0.0) if event["type"] == "sar_scan"
    ]) == 3

    before_weather = sm.get_persistent_coverage_stats()
    blocked = np.zeros(engine.config.grid.resolution, dtype=bool)
    blocked[cell.col, cell.row] = True
    sm.set_environment_obstacles([], blocked)
    after_weather = sm.get_persistent_coverage_stats()
    assert after_weather["fixed_searchable_cells"] == before_weather["fixed_searchable_cells"]
    assert after_weather["ever_scanned_cells"] == before_weather["ever_scanned_cells"]
    assert _window(after_weather)["covered_cells"] == _window(before_weather)["covered_cells"]
    assert after_weather["currently_searchable_cells"] == before_weather["currently_searchable_cells"] - 1
    assert after_weather["weather_blocked_cells"] == before_weather["weather_blocked_cells"] + 1
    _assert_event_oracle(engine)


def test_duplicate_sar_cells_refresh_legacy_information_once_per_uav(monkeypatch):
    engine = _engine(uav_count=1)
    sm = engine.allocator.sm
    uav = engine.uavs[0]
    uav.status = "searching"
    uav.sar_imaging = True
    uav.sar_look_direction = "right"
    uav.sar_sensor = _FixedSar((GridCoord(10, 10), GridCoord(10, 10)))
    scanned = []
    original_scan = sm.scan_cell

    def scan_cell(cell, current_time, is_track=False):
        scanned.append(cell)
        return original_scan(cell, current_time, is_track=is_track)

    monkeypatch.setattr(sm, "scan_cell", scan_cell)
    before_version = sm.information_policy.version
    sm.current_time = 1.0

    engine._update_sensors_and_detections(1.0)

    assert scanned == [GridCoord(10, 10)]
    assert sm.information_policy.version == before_version + 1
    assert sm.get_info_matrix()[10, 10] == 1.0
    assert sm.coverage_metrics.last_scan_matrix()[10, 10] == 1.0


def test_empty_sar_footprint_preserves_metrics_while_engine_time_advances(monkeypatch):
    engine = _engine(uav_count=1)
    sm = engine.allocator.sm
    uav = engine.uavs[0]
    uav.sar_sensor = _FixedSar(())
    metric = sm.coverage_metrics
    metric.record_sar(((10, 10),), at_min=0.0)
    before = sm.get_persistent_coverage_stats()
    scans = metric.last_scan_matrix()
    sensor_boundary = engine._update_sensors_and_detections
    observed_times = []

    def empty_scan(current_time):
        uav.status = "searching"
        uav.sensor_mode = "sar"
        uav.sar_imaging = True
        uav.sar_look_direction = "right"
        observed_times.append(current_time)
        sensor_boundary(current_time)

    monkeypatch.setattr(engine, "_update_sensors_and_detections", empty_scan)
    engine.step()

    assert observed_times == [1.0]
    assert engine.clock.time == sm.current_time == 1.0
    assert not any(event["type"] == "sar_scan" for event in sm.get_recent_events(0.0))
    np.testing.assert_array_equal(metric.last_scan_matrix(), scans)
    # A pure snapshot at the last real scan time remains legal: no empty
    # observation may move the metric's internal monotonic-time boundary.
    assert metric.snapshot(now_min=0.0, feasible_mask=sm.get_searchable_mask()) == before
    after = sm.get_persistent_coverage_stats()
    assert after == {**before, "as_of_min": 1.0}


@pytest.mark.parametrize("class_name", ["CoverageFixtureGateway", "_FixtureGateway"])
def test_fixture_hash_covers_both_defining_sources(monkeypatch, class_name):
    from scripts import persistent_coverage_scenarios as scenarios

    before = scenarios.coverage_fixture_source_hash()
    getsource = scenarios.inspect.getsource
    changed_class = getattr(scenarios, class_name)

    def changed_source(obj):
        source = getsource(obj)
        return source + "\n# changed defining source\n" if obj is changed_class else source

    monkeypatch.setattr(scenarios.inspect, "getsource", changed_source)
    assert scenarios.coverage_fixture_source_hash() != before


def test_shared_coverage_scenarios_and_constraint_aware_fixture():
    from scripts.persistent_coverage_scenarios import (
        CoverageFixtureGateway,
        build_coverage_scenario,
    )

    open_water = build_coverage_scenario(
        "coverage-open-water", seed=42, transport="fixture"
    )
    mixed = build_coverage_scenario(
        "coverage-mixed-weather", seed=42, transport="fixture"
    )
    assert open_water.config.uav.count == 10
    assert open_water.config.environment.base_count == 2
    assert open_water.config.ship.population.total_count == 0
    assert open_water.config.environment.island_count_max == 0
    assert open_water.config.environment.thunderstorm_count_max == 0
    assert mixed.config.ship.population.total_count == ConfigLoader.load().ship.population.total_count
    assert isinstance(open_water.allocator.llm_client.gateway, CoverageFixtureGateway)

    gateway = CoverageFixtureGateway()
    snapshot = {
        "snapshot_id": "coverage-fixture",
        "information_version": 7,
        "candidates": [
            {"task_id": "probe-1", "kind": "probe", "priority": "high"},
            {"task_id": "search-old", "kind": "search", "priority": "normal"},
            {"task_id": "search-new", "kind": "search", "priority": "normal"},
        ],
        "feasible_edges": [
            {"task_id": "probe-1", "uav_id": "UAV-1"},
            {"task_id": "search-old", "uav_id": "UAV-2"},
            {"task_id": "search-new", "uav_id": "UAV-3"},
        ],
        "coverage_constraint": {
            "required_new_search_count": 2,
            "representative_task_ids": ["search-old", "search-new"],
            "must_service_task_ids": ["search-old"],
        },
    }

    def validate(payload):
        selected = payload["selected_task_ids"]
        errors = []
        if "search-old" not in selected:
            errors.append("coverage_oldest_not_selected")
        if len({"search-old", "search-new"} & set(selected)) < 2:
            errors.append("coverage_floor_not_met:2")
        return errors

    result = gateway.request_json(
        role="decision_maker",
        snapshot_id="coverage-fixture",
        user_payload={"snapshot": snapshot},
        validate=validate,
    )
    assert result.success
    assert {"search-old", "search-new"} <= set(result.payload["selected_task_ids"])
    assert len(result.payload["notes"]) <= 160

    constrained_snapshot = {
        **snapshot,
        "candidates": [snapshot["candidates"][1]],
        "feasible_edges": [snapshot["feasible_edges"][1]],
    }
    result = gateway.request_json(
        role="decision_maker",
        snapshot_id="coverage-fixture",
        user_payload={"snapshot": constrained_snapshot},
        validate=validate,
    )
    assert not result.success
    assert "coverage_floor_not_met:2" in result.errors
    assert result.payload["selected_task_ids"] == ["search-old"]


def test_coverage_fixture_spreads_visible_bounded_search_work():
    from scripts.persistent_coverage_scenarios import CoverageFixtureGateway

    candidates = [
        {
            "task_id": f"search-{index}",
            "kind": "search",
            "priority": "normal",
            "bbox": [index * 4, 0, index * 4 + 2, 2],
        }
        for index in range(6)
    ]
    ordered = CoverageFixtureGateway._spread_representatives(
        tuple(item["task_id"] for item in candidates),
        {item["task_id"]: item for item in candidates},
        ("search-0",),
        3,
    )

    assert ordered[:3] == (
        "search-0", "search-5", "search-2",
    )


def test_coverage_fixture_respects_zero_exact_search_budget():
    from scripts.persistent_coverage_scenarios import CoverageFixtureGateway

    candidates = [
        {
            "task_id": f"search-{index}",
            "kind": "search",
            "priority": "normal",
            "bbox": [index * 4, 0, index * 4 + 2, 2],
        }
        for index in range(6)
    ]
    snapshot = {
        "snapshot_id": "coverage-no-floor",
        "information_version": 1,
        "available_uav_ids": ["UAV-1", "UAV-2", "UAV-3"],
        "candidates": candidates,
        "feasible_edges": [
            {"task_id": item["task_id"], "uav_id": f"UAV-{index % 3 + 1}"}
            for index, item in enumerate(candidates)
        ],
        "coverage_constraint": {
            "required_new_search_count": 0,
            "representative_task_ids": [item["task_id"] for item in candidates],
            "must_service_task_ids": [],
        },
    }

    result = CoverageFixtureGateway().request_json(
        role="decision_maker",
        snapshot_id=snapshot["snapshot_id"],
        user_payload={"snapshot": snapshot},
        validate=lambda _payload: (),
    )

    assert result.success
    assert result.payload["selected_task_ids"] == []

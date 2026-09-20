from __future__ import annotations

from dataclasses import replace

import pytest

from src.control.common.contracts import (
    ControlRouteSnapshot,
    ControlTask,
    OperationMode,
    UavRouteSnapshot,
)
from src.env.uav_entity import UAVEntity
from src.env.simulation import SimulationEngine
from src.mission.contracts import ProbeSession
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, GridCoord
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_builder import (
    _transit_progress,
    build_frame,
    remaining_route,
    sample_route_overview,
)


def _entity() -> UAVEntity:
    entity = UAVEntity(
        "UAV-1", GridCoord(1, 1), endurance_h=8.0, cruise_speed_kmh=160.0,
    )
    entity._col = 2.25
    entity._row = 2.5
    entity.planned_path = [(99.0, 99.0, 0.0)]
    entity.mission_route = [(98.0, 98.0, 0.0)]
    return entity


def _state(*, generation=3) -> StateManager:
    state = StateManager(ConfigLoader.load())
    state.episode_id = "episode-visual"
    state.update_uav_control(
        "UAV-1", "heuristic", "heuristic", "coverage", generation, False,
    )
    return state


def _route(*, task_type="coverage", phase="transit", status="ready", next_index=11):
    poses = tuple((float(index), float(index % 17), 0.01 * index) for index in range(1000))
    return ControlRouteSnapshot(
        "task-visual", task_type, phase, "contact-1" if task_type == "probe" else None,
        poses, next_index, 4, 2, status,
    )


def _frame(state: StateManager, entity: UAVEntity, *, realtime=True):
    return build_frame(
        state,
        cycle=0,
        config=ConfigLoader.load(),
        uav_entities=[entity],
        realtime=realtime,
        include_matrices=False,
    )


def test_route_sampling_preserves_continuity_and_both_endpoints():
    poses = [(float(index), 0.0, 0.0) for index in range(1000)]

    overview = sample_route_overview(poses, 200)
    remaining = remaining_route((2.25, 2.5, 0.0), poses, 11, 100)

    assert len(overview) == 200
    assert overview[0] == [0.0, 0.0, 0.0]
    assert overview[-1] == [999.0, 0.0, 0.0]
    assert len(remaining) == 100
    assert remaining[0] == [2.25, 2.5, 0.0]
    assert remaining[1] == list(poses[11])
    assert remaining[1] != list(poses[-100])


def test_route_frame_uses_controller_snapshot_for_bounded_paths_and_metadata():
    state = _state()
    route = _route()
    state.set_control_route("UAV-1", UavRouteSnapshot("episode-visual", 3, route))

    frame = _frame(state, _entity())
    uav = frame["uavs"][0]

    assert frame["visual_schema_version"] == "mission-visual/v1"
    assert len(uav["planned_path"]) == 100
    assert uav["planned_path"][0] == [2.25, 2.5, -1.5707963267948966]
    assert uav["planned_path"][1] == list(route.route[11])
    assert len(uav["mission_route"]) == 200
    assert uav["mission_route"][0] == list(route.route[0])
    assert uav["mission_route"][-1] == list(route.route[-1])
    assert uav["task_visual"] == {
        "task_id": "task-visual",
        "task_type": "coverage",
        "phase": "transit",
        "contact_id": None,
        "generation": 3,
        "route_revision": 4,
        "planning_map_version": 2,
        "route_status": "ready",
        "route_source": "controller",
        "observation_started": None,
        "coverage_progress": None,
    }


@pytest.mark.parametrize("status", ["cleared", "unavailable"])
def test_explicit_controller_status_does_not_fallback_to_legacy_entity_routes(status):
    state = _state()
    route = _route(status=status, next_index=0)
    state.set_control_route("UAV-1", UavRouteSnapshot("episode-visual", 3, route))

    uav = _frame(state, _entity())["uavs"][0]

    if status == "cleared":
        assert uav["planned_path"] == []
        assert uav["mission_route"] == []
    else:
        assert uav["planned_path"]
        assert uav["mission_route"]
    assert uav["task_visual"]["route_source"] == "controller"
    assert uav["task_visual"]["route_status"] == status


def test_stale_controller_snapshot_is_dropped_without_legacy_revival():
    state = _state(generation=3)
    route = _route()
    state.set_control_route("UAV-1", UavRouteSnapshot("episode-visual", 2, route))

    uav = _frame(state, _entity())["uavs"][0]

    assert uav["planned_path"] == []
    assert uav["mission_route"] == []
    assert uav["task_visual"]["route_source"] == "none"
    assert uav["task_visual"]["route_status"] == "unavailable"
    assert uav["route_diagnostic"] == "stale_controller_snapshot"


def test_route_frame_reads_probe_observation_started_from_public_session_only():
    state = _state()
    state.update_uav_control(
        "UAV-1", "heuristic", "heuristic", "probe", 3, False,
    )
    route = _route(task_type="probe", phase="baseline", next_index=1)
    state.set_control_route("UAV-1", UavRouteSnapshot("episode-visual", 3, route))
    state.set_probe_session(
        ProbeSession(
            "probe-1", "contact-1", "UAV-1", "baseline", 1.0, None, 1.0,
            (), (), 0.0, None,
        )
    )

    approaching = _frame(state, _entity())["uavs"][0]
    assert approaching["task_visual"]["observation_started"] is False

    state.set_probe_session(
        replace(state.get_probe_session("probe-1"), baseline_started_at_min=2.0)
    )
    started = _frame(state, _entity())["uavs"][0]
    assert started["task_visual"]["observation_started"] is True


def test_transit_progress_is_none_when_no_real_transit_boundary_exists():
    entity = _entity()
    entity.status = "transit"

    assert _transit_progress(entity) is None


def test_simulation_publishes_current_route_envelopes_at_runtime_boundaries():
    engine = SimulationEngine(ConfigLoader.load(), seed=17, llm_gateway=object())
    state = engine.allocator.sm

    initial = [state.get_control_route(uav.id) for uav in engine.uavs]
    assert all(snapshot is not None for snapshot in initial)
    assert all(snapshot.route.status == "cleared" for snapshot in initial)

    task = ControlTask(
        "coverage:published",
        OperationMode.COVERAGE,
        region_bbox=BBox(4, 4, 8, 8),
    )
    lease = engine.control_coordinator.start_work(
        "UAV-1",
        sortie_number=1,
        current_time=0.0,
        dt_min=engine.clock.dt_min,
        task=task,
    )
    engine._publish_control_routes()

    published = state.get_control_route("UAV-1")
    assert published is not None
    assert published.generation == lease.generation
    assert published.route.task_id == task.task_id
    assert published.route.task_type == "coverage"
    assert published.route.status == "pending"

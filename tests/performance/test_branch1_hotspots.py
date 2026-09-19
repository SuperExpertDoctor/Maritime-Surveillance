from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np

from src.env.dubins import DubinsPath
from src.mission.coverage_policy import rank_search_candidates
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox
from src.schedule.state_manager import StateManager
from src.schedule.task_allocator import TaskAllocator
from src.vis.backend import frame_builder


@dataclass(frozen=True)
class _RankCandidate:
    task_id: str
    bbox: tuple[int, int, int, int]

    @property
    def cells(self):
        return tuple(
            (col, row)
            for col in range(self.bbox[0], self.bbox[2])
            for row in range(self.bbox[1], self.bbox[3])
        )


def _large_rank_fixture():
    # Keep the full 374k-cell information fixture stable while limiting the
    # candidate count to the scheduler's bounded prompt window.
    shape = (614, 611)
    last_sar = np.full(shape, -np.inf, dtype=float)
    last_sar[::7, ::11] = 0.0
    candidates = []
    estimated = {}
    for index in range(256):
        col = 2 + (index * 17) % 570
        row = 2 + (index * 23) % 560
        candidate = _RankCandidate(f"search:{index}", (col, row, col + 24, row + 24))
        candidates.append(candidate)
        estimated[candidate.task_id] = 12.0
    return candidates, last_sar, estimated


def test_rank_search_candidates_vectorized_budget(benchmark):
    candidates, last_sar, estimated = _large_rank_fixture()

    ranked = benchmark(
        rank_search_candidates,
        candidates,
        now_min=120.0,
        last_sar=last_sar,
        estimated_minutes=estimated,
    )

    assert len(ranked) == len(candidates)
    assert {candidate.task_id for candidate in ranked} == {
        candidate.task_id for candidate in candidates
    }
    assert benchmark.stats["mean"] < 0.5


def test_info_value_grid_cache_invalidates_on_planning_map_version():
    config = ConfigLoader.load()
    state = StateManager(config)
    bbox = BBox(2, 2, 12, 12)

    first_info = state.get_avg_info_in_bbox(bbox)
    first_value = state.get_avg_value_in_bbox(bbox)
    second_info = state.get_avg_info_in_bbox(bbox)
    second_value = state.get_avg_value_in_bbox(bbox)

    assert (first_info, first_value) == (second_info, second_value)
    assert len(state._bbox_info_cache) == 1
    assert len(state._bbox_value_cache) == 1

    state.set_environment_obstacles([], np.ones(config.grid.resolution, dtype=bool))
    assert not state._bbox_info_cache
    assert not state._bbox_value_cache


def test_route_caches_are_bounded_and_evicted_on_version_change(monkeypatch):
    config = ConfigLoader.load()
    allocator = TaskAllocator(config, llm_gateway=object())
    resource = SimpleNamespace(
        uav_id="UAV-1",
        generation=1,
        position_cells=(5.0, 5.0),
        heading_rad=0.0,
    )
    fake_path = SimpleNamespace(total_length=3.0, waypoints=[(0.0, 0.0, 0.0)])
    monkeypatch.setattr(DubinsPath, "compute", lambda *_args, **_kwargs: fake_path)
    monkeypatch.setattr(allocator._mission_navigator, "_path_is_safe", lambda *_args: True)

    for index in range(600):
        allocator._return_route_distance(
            (float(index % 30), float(index // 30)),
            resource,
            (1.0, 1.0),
            planning_map_version=1,
        )

    assert len(allocator._mission_route_cache) <= 512
    assert len(allocator._mission_route_metrics_cache) <= 512
    allocator._return_route_distance((1.0, 2.0), resource, (1.0, 1.0), 2)
    assert all(key[1] == 2 for key in allocator._mission_route_cache)


def test_task_cells_are_reused_when_generation_unchanged():
    region = SimpleNamespace(id="search:cached", bbox=BBox(2, 3, 16, 18))

    frame_builder._task_cells_cached.cache_clear()
    first = frame_builder._task_cells(region)
    second = frame_builder._task_cells(region)

    assert first == second
    cache_info = frame_builder._task_cells_cached.cache_info()
    assert cache_info.hits >= 1
    assert cache_info.maxsize == 2048


def test_mission_snapshot_light_path_skips_full_rebuild(monkeypatch):
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    snapshot = allocator.build_mission_snapshot(0.0)

    def fail_rebuild(*_args, **_kwargs):
        raise AssertionError("light trigger rebuilt the full mission snapshot")

    monkeypatch.setattr(allocator, "build_mission_snapshot", fail_rebuild)
    result, batch = allocator._handle_light_mission_trigger(
        1.0,
        SimpleNamespace(),
        snapshot.active_tasks,
        intents=snapshot.intents,
        intent_statuses=snapshot.intent_statuses,
    )

    assert batch is None
    assert result["trigger_type"] == "light"
    assert allocator.last_mission_snapshot.snapshot_id == snapshot.snapshot_id

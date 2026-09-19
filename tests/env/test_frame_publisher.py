import json
from types import SimpleNamespace

from src.control.common.contracts import ControlRouteSnapshot, UavRouteSnapshot
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend.frame_builder import build_frame
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend.frame_publisher import FramePublisher


def _engine_with_time(config, state):
    return SimpleNamespace(
        allocator=SimpleNamespace(sm=state),
        config=config,
        ships=[],
        uavs=[],
        obstacles=[],
        bases=[],
    )


def test_frame_publisher_persists_every_immutable_step_snapshot(tmp_path):
    config = ConfigLoader.load()
    state = StateManager(config)
    publisher = FramePublisher(FrameLogger(str(tmp_path)))
    engine = _engine_with_time(config, state)

    for step in (1, 2, 3):
        state.current_time = float(step)
        publisher.push_snapshot(engine, {}, total_steps=3)

    assert publisher.flush(timeout=5)
    publisher.close()
    lines = publisher.logger.path
    frames = [json.loads(line) for line in open(lines, encoding="utf-8")]

    assert publisher.record_count == 3
    assert [frame["frame_id"] for frame in frames] == [1, 2, 3]


def test_compact_live_frame_omits_matrices_without_changing_replay_shape():
    config = ConfigLoader.load()
    state = StateManager(config)

    compact = build_frame(state, 0, config, realtime=True, include_matrices=False)
    replay = build_frame(state, 0, config)

    assert "info_matrix" not in compact
    assert "value_matrix" not in compact
    assert len(replay["info_matrix"]) == config.grid.resolution[0]
    assert len(replay["value_matrix"]) == config.grid.resolution[0]


def test_publishing_a_frame_while_a_coverage_route_exists(tmp_path):
    """A recorded coverage route must not break frame publication.

    Regression: the snapshot deep-copy raised
    ``TypeError: cannot pickle 'mappingproxy' object`` on the first frame after
    a coverage route was recorded, killing the whole simulation process.
    """
    config = ConfigLoader.load()
    state = StateManager(config)
    state.episode_id = "episode-1"
    state.update_uav_control(
        "UAV-2", "heuristic", "heuristic", "coverage", 1, False,
    )
    route = ControlRouteSnapshot(
        task_id="task-1",
        task_type="coverage",
        phase="coverage",
        target_contact_id=None,
        route=((1.0, 2.0, 0.0), (3.0, 4.0, 0.0)),
        next_index=0,
        route_revision=1,
        planning_map_version=1,
        status="ready",
        coverage_progress={"phase": "coverage", "progress_cells": 12.0},
    )
    state.set_control_route("UAV-2", UavRouteSnapshot("episode-1", 1, route))
    publisher = FramePublisher(FrameLogger(str(tmp_path)))
    engine = _engine_with_time(config, state)

    publisher.push_snapshot(engine, {}, total_steps=1)

    assert publisher.flush(timeout=5)
    publisher.close()
    assert publisher.record_count == 1

from dataclasses import FrozenInstanceError

import pytest

from src.control.common.contracts import (
    ControlRouteSnapshot,
    ControlMode,
    UavRouteSnapshot,
)
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from tests.control.test_coordinator import DeterministicController, make_runtime


def _route(*, revision=1, status="ready"):
    return ControlRouteSnapshot(
        task_id="search-1",
        task_type="coverage",
        phase="transit",
        target_contact_id=None,
        route=((1.0, 2.0, 0.0), (3.0, 4.0, 0.5)),
        next_index=1,
        route_revision=revision,
        planning_map_version=7,
        status=status,
    )


def _envelope(*, generation=1, route=None, episode="episode-1"):
    return UavRouteSnapshot(
        episode,
        generation,
        _route() if route is None else route,
    )


def test_route_contract_freezes_nested_route_and_validates_progress():
    poses = [[1.0, 2.0, 0.0], [3.0, 4.0, 0.5]]
    route = ControlRouteSnapshot(
        "task-1", "probe", "baseline", "contact-1", poses, 1, 2, 4, "ready",
    )
    poses[0][0] = 99.0

    assert route.route == ((1.0, 2.0, 0.0), (3.0, 4.0, 0.5))
    with pytest.raises(FrozenInstanceError):
        route.next_index = 0
    with pytest.raises(TypeError):
        route.route[0][0] = 9.0

    with pytest.raises(ValueError, match="next_index"):
        ControlRouteSnapshot("t", "probe", "baseline", None, (), 1, 0, None, "ready")
    with pytest.raises(ValueError, match="route_revision"):
        ControlRouteSnapshot("t", "probe", "baseline", None, (), 0, -1, None, "ready")
    with pytest.raises(ValueError, match="status"):
        ControlRouteSnapshot("t", "probe", "baseline", None, (), 0, 0, None, "unknown")


def test_state_manager_rejects_stale_route_lifecycle_and_blocks_old_revival():
    state = StateManager(ConfigLoader.load())
    state.episode_id = "episode-1"
    state.set_control_route("UAV-1", _envelope(generation=2, route=_route(revision=3)))

    with pytest.raises(ValueError, match="generation"):
        state.set_control_route("UAV-1", _envelope(generation=1))
    with pytest.raises(ValueError, match="revision"):
        state.set_control_route(
            "UAV-1", _envelope(generation=2, route=_route(revision=2))
        )

    cleared = _route(revision=1, status="cleared")
    state.set_control_route("UAV-1", _envelope(generation=2, route=cleared))
    assert state.get_control_route("UAV-1").route.status == "cleared"
    with pytest.raises(ValueError, match="cleared"):
        state.set_control_route(
            "UAV-1", _envelope(generation=2, route=_route(revision=2))
        )

    state.clear_control_routes()
    state.episode_id = "episode-2"
    state.set_control_route("UAV-1", _envelope(generation=0, episode="episode-2"))
    assert state.get_control_route("UAV-1").episode_id == "episode-2"


def test_coordinator_route_snapshot_uses_current_episode_and_lease_generation():
    controller = DeterministicController(ControlMode.BC)
    coordinator, _, state, _, _ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    state.episode_id = "episode-live"

    cleared = coordinator.route_snapshot("UAV-1")
    assert isinstance(cleared, UavRouteSnapshot)
    assert cleared.episode_id == "episode-live"
    assert cleared.generation == 0
    assert cleared.route.status == "cleared"

    coordinator.start_work(
        "UAV-1", sortie_number=1, current_time=0.0, dt_min=1.0,
    )
    unavailable = coordinator.route_snapshot("UAV-1")
    assert unavailable.episode_id == "episode-live"
    assert unavailable.generation == coordinator.current_lease("UAV-1").generation
    assert unavailable.route.status == "unavailable"


def test_coordinator_uses_controller_route_without_exposing_controller_context():
    controller = DeterministicController(ControlMode.BC)
    controller.route_snapshot = lambda: _route(revision=4)
    coordinator, _, state, _, _ = make_runtime(
        {"UAV-1": ControlMode.BC}, {"UAV-1": controller}
    )
    state.episode_id = "episode-live"
    coordinator.start_work(
        "UAV-1", sortie_number=2, current_time=0.0, dt_min=1.0,
    )

    first = coordinator.route_snapshot("UAV-1")
    second = coordinator.route_snapshot("UAV-1")

    assert first == second
    assert first.route == _route(revision=4)
    assert first.generation == coordinator.current_lease("UAV-1").generation

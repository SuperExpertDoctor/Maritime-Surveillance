import pytest
import numpy as np

from dataclasses import replace

from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord, BBox, Region, UAVState
from src.mission.contracts import ProbeSession
from src.schedule.state_manager import StateManager


@pytest.fixture
def config():
    return ConfigLoader.load()


@pytest.fixture
def sm(config):
    return StateManager(config)


def test_init_creates_uavs(sm, config):
    """初始化后 UAV 列表已填充并为 idle 状态。"""
    uavs = sm.get_all_uavs()
    assert len(uavs) == config.uav.count_max
    assert all(u.status == "idle" for u in uavs)


def test_create_track_region(sm):
    """创建跟踪区后可从 get_track_regions 获取。"""
    sm.current_time = 100.0
    sm.create_track_region("G1", GridCoord(15, 15))
    tracks = sm.get_track_regions()
    assert len(tracks) == 1
    assert tracks[0].type == "track"
    assert tracks[0].bbox == BBox(13, 13, 17, 17)  # ±2 格


def test_release_track_region_adds_marker(sm):
    """释放跟踪区时创建标记点。"""
    sm.current_time = 200.0
    region = sm.create_track_region("G1", GridCoord(10, 10))
    sm.release_track_region(region.id, source_uav_id="UAV-1")
    markers = sm.get_active_markers()
    assert len(markers) == 1
    assert markers[0].source_uav_id == "UAV-1"


def test_release_track_region_marker_does_not_mutate_information_value(sm):
    before = sm.get_value_matrix()
    region = sm.create_track_region("G1", GridCoord(10, 10))
    sm.release_track_region(region.id, source_uav_id="UAV-1")

    assert np.array_equal(before, sm.get_value_matrix())


def test_add_event(sm):
    """事件记录到事件流中。"""
    sm.add_event("target_found", {"group_id": "G1", "position": GridCoord(5, 5)})
    events = sm.get_recent_events(since_time=0.0)
    assert len(events) == 1
    assert events[0]["type"] == "target_found"


def test_get_available_uavs(sm):
    """Only SYSTEM-owned idle/holding heuristic UAVs are schedulable."""
    sm.current_time = 50.0
    sm.update_uav_status("UAV-1", "searching", GridCoord(5, 5), assigned_region_id="S1")
    sm.update_uav_status("UAV-2", "transit", GridCoord(10, 10), assigned_region_id="S2")
    available = sm.get_available_uavs()
    assert len(available) == sm.config.uav.count_max - 2


def test_ordinary_search_region_queries_are_sorted_and_side_effect_free(sm):
    regions = [
        Region("track", BBox(0, 0, 2, 2), "track"),
        Region("stale", BBox(2, 0, 4, 2), "search", status="stale"),
        Region("pending", BBox(4, 0, 6, 2), "search"),
        Region("assigned", BBox(6, 0, 8, 2), "search", assigned_uav_id="UAV-2"),
        Region("completed", BBox(8, 0, 10, 2), "search", status="completed"),
    ]
    sm.set_search_regions(regions)
    before = [
        (region.id, region.status, region.assigned_uav_id)
        for region in sm.get_search_regions()
    ]

    assert tuple(region.id for region in sm.get_pending_search_regions()) == (
        "pending",
    )
    assert tuple(region.id for region in sm.get_assigned_search_regions()) == (
        "assigned",
    )
    assert tuple(region.id for region in sm.get_unfinished_search_regions()) == (
        "assigned",
        "pending",
    )
    assert [
        (region.id, region.status, region.assigned_uav_id)
        for region in sm.get_search_regions()
    ] == before


def test_available_uavs_excludes_refueling_despite_idle_operation_snapshot(sm):
    sm.update_uav_status("UAV-1", "refueling", GridCoord(5, 5))

    uav = sm.get_uav("UAV-1")

    assert uav.operation_mode == "idle"
    assert "UAV-1" not in {item.id for item in sm.get_available_uavs()}


def test_uav_control_snapshot_defaults_preserve_construction_compatibility():
    uav = UAVState("UAV-X", "idle", GridCoord(1, 2))

    assert uav.control_mode == "heuristic"
    assert uav.control_owner == "system"
    assert uav.operation_mode == "idle"
    assert uav.controller_generation == 0
    assert uav.safety_intervened is False


def test_update_uav_control_requires_and_updates_all_five_fields(sm):
    sm.update_uav_control("UAV-1", "bc", "learning", "coverage", 4, True)
    uav = sm.get_uav("UAV-1")

    assert (
        uav.control_mode,
        uav.control_owner,
        uav.operation_mode,
        uav.controller_generation,
        uav.safety_intervened,
    ) == ("bc", "learning", "coverage", 4, True)

    with pytest.raises(TypeError):
        sm.update_uav_control("UAV-1", "heuristic", "system", "idle", 5)


@pytest.mark.parametrize(
    ("control_mode", "control_owner", "operation_mode"),
    [
        ("bc", "system", "idle"),
        ("rl", "system", "holding"),
        ("heuristic", "learning", "idle"),
        ("heuristic", "heuristic", "holding"),
        ("heuristic", "system", "coverage"),
        ("heuristic", "system", "transit"),
    ],
)
def test_available_uavs_excludes_ineligible_control_snapshot(
    sm, control_mode, control_owner, operation_mode
):
    sm.update_uav_control(
        "UAV-1",
        control_mode,
        control_owner,
        operation_mode,
        1,
        False,
    )

    assert "UAV-1" not in {uav.id for uav in sm.get_available_uavs()}


@pytest.mark.parametrize("operation_mode", ["idle", "holding"])
def test_available_uavs_accepts_system_heuristic_idle_or_holding(
    sm, operation_mode
):
    sm.update_uav_control(
        "UAV-1", "heuristic", "system", operation_mode, 2, False
    )

    assert "UAV-1" in {uav.id for uav in sm.get_available_uavs()}


def test_probe_session_updates_same_owner_but_rejects_owner_conflict(sm):
    original = ProbeSession(
        "P0001", "C0001", "UAV-1", "baseline", 0.0, None, 0.0,
        (), (), 0.0, None,
    )
    advanced = replace(original, phase="near", baseline_started_at_min=1.0)
    sm.set_probe_session(original)
    sm.set_probe_session(advanced)
    assert sm.get_probe_session("P0001") == advanced

    with pytest.raises(ValueError, match="probe_id_owner_conflict"):
        sm.set_probe_session(replace(original, contact_id="C0002"))
    with pytest.raises(ValueError, match="probe_id_owner_conflict"):
        sm.set_probe_session(replace(original, uav_id="UAV-2"))


def test_searchability_keeps_task_edges_and_coast_but_respects_obstacles(sm):
    cols, rows = sm.config.grid.resolution
    obstacle_mask = np.zeros((cols, rows), dtype=bool)
    obstacle_mask[8, 8] = True
    sm.set_environment_obstacles([], obstacle_mask)
    land_mask = np.zeros((cols, rows), dtype=bool)
    land_mask[:5, :] = True
    sm.set_land_mask(land_mask)
    sm.scan_cell(GridCoord(7, 7), 10.0)
    sm.scan_cell(GridCoord(8, 8), 10.0)
    sm.scan_cell(GridCoord(0, 0), 10.0)

    stats = sm.get_coverage_stats()

    assert stats["searchable_cells"] == cols * rows - 1
    assert stats["scanned_searchable_cells"] == 2
    assert stats["coverage_pct"] == pytest.approx(
        200 / stats["searchable_cells"]
    )

import pytest
from schedule.config_loader import ConfigLoader
from schedule.datatypes import GridCoord, BBox, UAVState, Region
from schedule.state_manager import StateManager


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
    region = sm.create_track_region("G1", GridCoord(15, 15))
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


def test_add_event(sm):
    """事件记录到事件流中。"""
    sm.add_event("target_found", {"group_id": "G1", "position": GridCoord(5, 5)})
    events = sm.get_recent_events(since_time=0.0)
    assert len(events) == 1
    assert events[0]["type"] == "target_found"


def test_get_available_uavs(sm):
    """只返回 idle 状态的 UAV。"""
    sm.current_time = 50.0
    sm.update_uav_status("UAV-1", "searching", GridCoord(5, 5), assigned_region_id="S1")
    sm.update_uav_status("UAV-2", "transit", GridCoord(10, 10), assigned_region_id="S2")
    available = sm.get_available_uavs()
    assert len(available) == sm.config.uav.count_max - 2

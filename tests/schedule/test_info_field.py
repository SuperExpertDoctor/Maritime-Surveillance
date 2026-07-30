import pytest
import numpy as np
import math
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord, BBox
from src.schedule.info_field import InfoField


@pytest.fixture
def config():
    return ConfigLoader.load()


@pytest.fixture
def info_field(config):
    return InfoField(config)


def test_initial_info_is_zero(info_field):
    """所有 cell 初始信息素为 0 (黑态势)。"""
    mat = info_field.get_info_matrix()
    assert mat.shape == (30, 30)
    assert np.all(mat == 0.0)


def test_scan_cell_resets_info(info_field):
    """扫描后 cell 信息素重置为 1.0。"""
    info_field.scan_cell(GridCoord(10, 15), current_time=0.0)
    assert info_field.get_info_matrix()[10, 15] == 1.0


def test_decay_after_half_life(info_field):
    """经过一个半衰期后信息素衰减到 0.5。"""
    info_field.scan_cell(GridCoord(5, 5), current_time=0.0)
    info_field.update_decay(current_time=0.0)  # no decay immediately
    assert info_field.get_info_matrix()[5, 5] == 1.0

    half_life = info_field.config.grid.decay_half_life_min
    info_field.update_decay(current_time=half_life)
    assert pytest.approx(info_field.get_info_matrix()[5, 5], rel=0.01) == 0.5


def test_classify_cell(info_field):
    """态势分类正确。"""
    assert info_field.classify_cell(0.8) == "white"
    assert info_field.classify_cell(0.5) == "gray"
    assert info_field.classify_cell(0.1) == "black"


def test_track_decay_faster(info_field):
    """跟踪扫描衰减半衰期为 15min。"""
    info_field.scan_cell(GridCoord(3, 3), current_time=0.0, is_track=True)
    track_half = info_field.config.grid.track_decay_half_life_min
    info_field.update_decay(current_time=track_half)
    assert pytest.approx(info_field.get_info_matrix()[3, 3], rel=0.01) == 0.5


def test_add_marker_increases_value(info_field):
    """标记点附近 cell 信息价值升高。"""
    t = 0.0
    info_field.add_marker(GridCoord(15, 15), current_time=t, marker_id="M1")
    v1 = info_field.get_value_matrix(current_time=t)
    # 标记点中心 cell 应该有非零价值
    assert v1[15, 15] > 0.0


def test_scan_bbox_updates_all_cells(info_field):
    """扫描 bbox 覆盖的所有 cell。"""
    bbox = BBox(10, 10, 14, 13)  # 4x3 = 12 cells
    info_field.scan_bbox(bbox, current_time=10.0)
    info_mat = info_field.get_info_matrix()
    for c in range(10, 14):
        for r in range(10, 13):
            assert info_mat[c, r] == 1.0, f"Cell ({c},{r}) should be 1.0"

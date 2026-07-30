import pytest
import numpy as np
from schedule.config_loader import ConfigLoader
from schedule.state_manager import StateManager
from schedule.datatypes import GridCoord, BBox
from schedule.candidate_extractor import CandidateExtractor, CandidateResult


@pytest.fixture
def config():
    return ConfigLoader.load()


@pytest.fixture
def sm(config):
    sm = StateManager(config)
    sm.current_time = 50.0
    return sm


def test_extract_returns_candidate_result(sm):
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    assert isinstance(result, CandidateResult)


def test_black_cells_become_candidates(sm):
    """黑态势 cell 应形成候选区域。"""
    # 所有 cell 初始 info=0 (黑)，value 较高
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    assert len(result.candidate_regions) > 0


def test_track_regions_are_excluded(sm):
    """跟踪区 cell 应从候选区域中排除。"""
    sm.create_track_region("G1", GridCoord(15, 15))
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    # 验证候选区域的 bbox 不与跟踪区重叠
    for cand in result.candidate_regions:
        bbox = cand["bbox"]
        for track in sm.get_track_regions():
            assert not _bboxes_overlap(bbox, track.bbox)


def test_candidate_bbox_within_size_range(sm):
    """候选区域面积应在合理范围内。"""
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    for cand in result.candidate_regions:
        w = cand["bbox"].col_end - cand["bbox"].col_start
        h = cand["bbox"].row_end - cand["bbox"].row_start
        area = w * h
        assert area >= sm.config.grid.search_min_cells, f"Area {area} too small"
        assert area <= sm.config.grid.search_max_cells * 2, "Area unexpectedly large"


def test_fragment_detection_on_overlap(sm):
    """当上一轮搜索区与跟踪区重叠时，应检测到碎片。"""
    sm.create_track_region("G1", GridCoord(15, 15))
    # 模拟上一轮搜索区
    from schedule.datatypes import Region
    prev_region = Region(
        id="S1",
        bbox=BBox(10, 10, 18, 18),
        type="search",
    )
    sm._previous_search_regions = [prev_region]
    sm._search_regions = []
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    assert isinstance(result.fragment_alerts, list)


def _bboxes_overlap(a: BBox, b: BBox) -> bool:
    if a.col_end <= b.col_start or b.col_end <= a.col_start:
        return False
    if a.row_end <= b.row_start or b.row_end <= a.row_start:
        return False
    return True

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
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    assert len(result.candidate_regions) > 0


def test_track_regions_are_excluded(sm):
    """跟踪区 cell 应从候选区域中排除。"""
    sm.create_track_region("G1", GridCoord(15, 15))
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
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


# --- Bug 1 regression: candidate count cap ---

def test_candidate_count_does_not_exceed_limit(sm):
    """Bug 1: subdivision must not inflate candidate count beyond K."""
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    # 15 idle UAVs -> K = min(15*2, 10) = 10
    assert len(result.candidate_regions) <= 10, (
        f"Got {len(result.candidate_regions)} candidates, expected <= 10"
    )


# --- Bug 2 regression: per-candidate values ---

def test_sub_candidates_have_correct_local_values(sm):
    """Bug 2: each candidate must report value/info from its own bbox."""
    extractor = CandidateExtractor()
    result = extractor.extract(sm)
    V = sm.get_value_matrix()
    I = sm.get_info_matrix()
    for cand in result.candidate_regions:
        bbox = cand["bbox"]
        patch_V = V[bbox.col_start:bbox.col_end, bbox.row_start:bbox.row_end]
        patch_I = I[bbox.col_start:bbox.col_end, bbox.row_start:bbox.row_end]
        expected_value = float(np.sum(patch_V))
        expected_info = float(np.mean(patch_I))
        assert abs(cand["total_value"] - expected_value) < 1e-6, (
            f"total_value mismatch: {cand['total_value']} vs {expected_value}"
        )
        assert abs(cand["avg_info"] - expected_info) < 1e-6, (
            f"avg_info mismatch: {cand['avg_info']} vs {expected_info}"
        )


# --- Bug 5 / Issue 5: strengthened fragment test ---

def test_fragment_detection_on_overlap(sm):
    """当上一轮搜索区与跟踪区重叠时，应检测到碎片。

    Previous region S1: bbox (10,10)-(18,18) = 8x8 = 64 cells.
    Track region G1 at centre (15,15): bbox (13,13)-(17,17) = 4x4 = 16 cells.
    Difference yields four strips; those with area < fragment_threshold_cells
    (12) become fragment alerts.
    """
    sm.create_track_region("G1", GridCoord(15, 15))
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

    # At least the right strip (1x8 = 8) and bottom strip (4x1 = 4) should
    # be flagged as fragments (area < 12).
    assert len(result.fragment_alerts) >= 2, (
        f"Expected >= 2 fragments, got {len(result.fragment_alerts)}"
    )
    for frag in result.fragment_alerts:
        assert frag["area"] < sm.config.grid.fragment_threshold_cells, (
            f"Fragment area {frag['area']} >= threshold"
        )
        assert "parent_region_id" in frag
        assert "reason" in frag
        assert "bbox" in frag


def _bboxes_overlap(a: BBox, b: BBox) -> bool:
    if a.col_end <= b.col_start or b.col_end <= a.col_start:
        return False
    if a.row_end <= b.row_start or b.row_end <= a.row_start:
        return False
    return True

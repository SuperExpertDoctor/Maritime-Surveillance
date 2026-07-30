import pytest
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, Region
from src.schedule.output_validator import validate, ValidationResult


@pytest.fixture
def config():
    return ConfigLoader.load()


def make_output(bboxes):
    return {
        "cycle": 1,
        "search_regions": [
            {"id": f"S{i+1}", "bbox": list(b), "priority": "high", "reason": "test"}
            for i, b in enumerate(bboxes)
        ],
        "notes": "test"
    }


def test_valid_output_passes(config):
    result = validate(make_output([[0, 0, 5, 6], [10, 10, 15, 16]]), config, [], [])
    assert result.is_valid


def test_bbox_out_of_bounds_fails(config):
    result = validate(make_output([[-5, 0, 5, 6]]), config, [], [])
    assert not result.is_valid
    assert any("out of bounds" in e.lower() for e in result.errors)


def test_area_too_small_fails(config):
    result = validate(make_output([[0, 0, 2, 3]]), config, [], [])  # 6 cells < 20
    assert not result.is_valid
    assert any("area" in e.lower() for e in result.errors)


def test_area_too_large_fails(config):
    result = validate(make_output([[0, 0, 10, 10]]), config, [], [])  # 100 cells > 40
    assert not result.is_valid


def test_aspect_ratio_fails(config):
    result = validate(make_output([[0, 0, 8, 2]]), config, [], [])  # 8:2 = 4:1 > 2:1
    assert not result.is_valid


def test_overlap_fails(config):
    result = validate(make_output([[0, 0, 5, 5], [3, 3, 8, 8]]), config, [], [])
    assert not result.is_valid
    assert any("overlap" in e.lower() for e in result.errors)


def test_overlap_with_track_region_fails(config):
    track = Region(id="T1", bbox=BBox(12, 12, 16, 16), type="track")
    result = validate(make_output([[10, 10, 15, 14]]), config, [track], [])
    assert not result.is_valid


def test_too_many_regions_fails(config):
    bboxes = [[i, 0, i+1, 6] for i in range(11)]  # 11 regions > 10
    result = validate(make_output(bboxes), config, [], [])
    assert not result.is_valid

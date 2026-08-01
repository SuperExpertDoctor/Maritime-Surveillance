import numpy as np
import pytest

from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, Region
from src.schedule.output_validator import validate


@pytest.fixture
def config():
    return ConfigLoader.load()


def make_output(bboxes):
    return {
        "cycle": 1,
        "search_regions": [
            {"id": f"S{i + 1}", "bbox": list(bbox), "priority": "high", "reason": "test"}
            for i, bbox in enumerate(bboxes)
        ],
        "notes": "test",
    }


def test_valid_output_passes(config):
    result = validate(make_output([[0, 0, 5, 6], [10, 10, 15, 16]]), config, [], [])
    assert result.is_valid


def test_scan_pattern_collision_outside_bbox_is_rejected(config):
    mask = np.zeros(config.grid.resolution, dtype=bool)
    mask[4, 7] = True
    result = validate(make_output([[5, 5, 11, 11]]), config, [], [], mask)
    assert not result.is_valid
    assert any("scan pattern" in error for error in result.errors)


def test_bbox_out_of_bounds_fails(config):
    result = validate(make_output([[-5, 0, 5, 6]]), config, [], [])
    assert not result.is_valid
    assert any("out of bounds" in error.lower() for error in result.errors)


def test_area_too_small_fails(config):
    result = validate(make_output([[0, 0, 2, 3]]), config, [], [])
    assert not result.is_valid
    assert any("area" in error.lower() for error in result.errors)


def test_area_too_large_fails(config):
    result = validate(make_output([[0, 0, 10, 10]]), config, [], [])
    assert not result.is_valid


def test_aspect_ratio_fails(config):
    result = validate(make_output([[0, 0, 8, 2]]), config, [], [])
    assert not result.is_valid


def test_overlap_fails(config):
    result = validate(make_output([[0, 0, 5, 5], [3, 3, 8, 8]]), config, [], [])
    assert not result.is_valid
    assert any("overlap" in error.lower() for error in result.errors)


def test_overlap_with_track_region_fails(config):
    track = Region(id="T1", bbox=BBox(12, 12, 16, 16), type="track")
    result = validate(make_output([[10, 10, 15, 14]]), config, [track], [])
    assert not result.is_valid


def test_too_many_regions_fails(config):
    bboxes = [[index, 0, index + 1, 6] for index in range(11)]
    result = validate(make_output(bboxes), config, [], [])
    assert not result.is_valid


def test_reused_display_id_does_not_imply_region_mutation(config):
    previous = [Region(id="S1", bbox=BBox(1, 1, 5, 7), type="search")]
    output = make_output([[20, 20, 24, 26]])

    result = validate(output, config, [], previous)

    assert result.is_valid


def test_region_too_close_to_land_base_is_rejected(config):
    result = validate(
        make_output([[10, 2, 15, 6]]),
        config,
        [],
        [],
        base_positions=[(12, 0)],
    )

    assert not result.is_valid
    assert any("land base" in error for error in result.errors)

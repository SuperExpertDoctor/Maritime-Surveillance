import builtins

import pytest

from src.schedule.datatypes import GridCoord, BBox
from src.schedule.hungarian import AssignmentBackendUnavailable, hungarian_pair


def test_hungarian_basic():
    uavs = [
        {"id": "UAV-1", "position": GridCoord(0, 0)},
        {"id": "UAV-2", "position": GridCoord(20, 0)},
    ]
    regions = [
        {"id": "S1", "bbox": BBox(0, 0, 5, 5)},
        {"id": "S2", "bbox": BBox(20, 0, 25, 5)},
    ]
    pairs = hungarian_pair(uavs, regions)
    assert ("UAV-1", "S1") in pairs
    assert ("UAV-2", "S2") in pairs


def test_hungarian_more_uavs_than_regions():
    uavs = [
        {"id": "UAV-1", "position": GridCoord(0, 0)},
        {"id": "UAV-2", "position": GridCoord(10, 10)},
    ]
    regions = [{"id": "S1", "bbox": BBox(15, 15, 20, 20)}]
    pairs = hungarian_pair(uavs, regions)
    assert len(pairs) == 1


def test_hungarian_more_regions_than_uavs():
    uavs = [{"id": "UAV-1", "position": GridCoord(5, 5)}]
    regions = [
        {"id": "S1", "bbox": BBox(0, 0, 5, 5)},
        {"id": "S2", "bbox": BBox(20, 0, 25, 5)},
    ]
    pairs = hungarian_pair(uavs, regions)
    assert len(pairs) == 1


def test_hungarian_empty_input():
    pairs = hungarian_pair([], [])
    assert pairs == []


def test_missing_scipy_is_blocked_instead_of_greedy_fallback(monkeypatch):
    original_import = builtins.__import__

    def block_scipy(name, *args, **kwargs):
        if name.startswith("scipy"):
            raise ImportError("scipy intentionally unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_scipy)

    with pytest.raises(AssignmentBackendUnavailable):
        hungarian_pair(
            [{"id": "U1", "position": GridCoord(1, 1)}],
            [{"id": "S1", "bbox": BBox(0, 0, 2, 2)}],
        )

import math

import numpy as np
import pytest

from src.schedule.datatypes import BBox, GridCoord
from src.env.dubins import DubinsPath
from src.utils.coverage_planner import CoveragePlanner


def test_square_bbox_has_three_gapless_swaths():
    path = CoveragePlanner(sample_step=0.2).plan(
        BBox(0, 0, 6, 6), (-2, -2, 0), swath_width=2, R_min=1
    )
    assert len(path.swaths) == 3
    expected = {GridCoord(c, r) for c in range(6) for r in range(6)}
    assert path.covered_cells == expected
    assert [swath.look_direction for swath in path.swaths] == ["right", "left", "right"]


def test_long_bbox_scans_along_long_axis():
    path = CoveragePlanner().plan(BBox(0, 0, 10, 3), (-2, -2, 0), 2, 1)
    assert len(path.swaths) == 2
    assert all(abs(swath.end[0] - swath.start[0]) > abs(swath.end[1] - swath.start[1]) for swath in path.swaths)


def test_initial_scan_direction_uses_shortest_dubins_entry():
    planner = CoveragePlanner(sample_step=0.2)
    bbox = BBox(5, 5, 13, 9)
    pose = (3.0, 8.5, math.pi)
    path = planner.plan(bbox, pose, 2, 1)
    first = path.swaths[0]
    entry = (first.start[0], first.start[1], first.heading)
    opposite = (first.end[0], first.end[1], (first.heading + math.pi) % (2 * math.pi) - math.pi)

    selected_length = DubinsPath.compute(pose, entry, 1, 0.2).total_length
    opposite_length = DubinsPath.compute(pose, opposite, 1, 0.2).total_length

    assert selected_length <= opposite_length + 1e-9


def test_scan_line_samples_are_collinear_and_equidistant():
    path = CoveragePlanner(sample_step=0.25).plan(BBox(0, 0, 6, 6), (-2, -2, 0), 2, 1)
    for start, end in path.scan_ranges:
        line = path.waypoints[start:end + 1]
        headings = [pose[2] for pose in line]
        assert max(headings) - min(headings) < 1e-9
        distances = [math.dist(a[:2], b[:2]) for a, b in zip(line, line[1:])]
        assert max(distances) - min(distances) < 1e-9


def test_dubins_turns_are_outside_bbox_without_extending_scan_lines():
    bbox = BBox(2, 2, 8, 8)
    path = CoveragePlanner().plan(bbox, (0, 0, 0), 2, 1)
    for swath in path.swaths:
        assert {swath.start[0], swath.end[0]} == {
            float(bbox.col_start),
            float(bbox.col_end),
        }

    for (_, scan_end), (next_scan_start, _) in zip(
        path.scan_ranges, path.scan_ranges[1:]
    ):
        connector = path.waypoints[scan_end + 1:next_scan_start]
        assert connector
        assert all(
            pose[0] < bbox.col_start or pose[0] > bbox.col_end
            for pose in connector
        )


def test_region_feasibility_checks_dubins_turns_outside_bbox():
    planner = CoveragePlanner(sample_step=0.2)
    bbox = BBox(5, 5, 11, 11)
    mask = np.zeros((30, 30), dtype=bool)
    mask[4, 7] = True
    assert not planner.is_region_feasible(bbox, 2, 1, mask)

import math

import numpy as np

from src.schedule.datatypes import BBox, GridCoord
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


def test_region_feasibility_checks_extended_sensor_scan_geometry():
    planner = CoveragePlanner(sample_step=0.2, near_range=0.25)
    bbox = BBox(10, 10, 16, 14)
    mask = np.zeros((30, 30), dtype=bool)
    mask[6, 10] = True

    assert not planner.is_region_feasible(
        bbox, 1.5, 1.0, mask, along_track_cells=0.8
    )


def test_sensor_geometry_clamps_endpoint_extension_to_world_bounds():
    planner = CoveragePlanner(sample_step=0.2, near_range=0.25)
    path = planner.plan(
        BBox(5, 1, 11, 9),
        (1.0, 1.0, 0.0),
        swath_width=1.5,
        R_min=1.0,
        along_track_cells=0.8,
        bounds=(30, 30),
    )

    assert all(
        0.0 <= coordinate < 30.0
        for swath in path.swaths
        for pose in (swath.start, swath.end)
        for coordinate in pose
    )


def test_sensor_geometry_leaves_turn_guard_at_world_boundary():
    path = CoveragePlanner().plan(
        BBox(5, 1, 11, 9),
        (2.0, 10.0, 0.0),
        swath_width=1.5,
        R_min=1.0,
        along_track_cells=0.8,
        bounds=(30, 30),
    )

    assert min(
        pose[0]
        for swath in path.swaths
        for pose in (swath.start, swath.end)
    ) >= 1.8 - 1e-9


def test_bounded_sensor_routes_alternate_direction_within_safe_bounds():
    path = CoveragePlanner().plan(
        BBox(11, 1, 17, 9),
        (2.0, 10.0, 0.0),
        swath_width=1.5,
        R_min=1.0,
        along_track_cells=0.8,
        bounds=(30, 30),
    )

    assert {swath.heading for swath in path.swaths} == {
        0.0,
        math.pi,
    }
    assert {swath.look_direction for swath in path.swaths} == {"left", "right"}
    assert all(
        0.0 <= coordinate < 30.0
        for swath in path.swaths
        for pose in (swath.start, swath.end)
        for coordinate in pose[:2]
    )

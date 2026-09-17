import math

import pytest

from src.control.common.contracts import ActionSpec
from src.control.heuristic.coverage_guidance import CoverageRouteFollower
from src.env.sar_sensor import SARSensor
from src.schedule.datatypes import BBox
from src.utils.coverage_planner import CoveragePlanner
from tests.mission.coverage_helpers import make_coverage_rig


@pytest.mark.parametrize("bbox", [(10, 10, 16, 14), (12, 8, 16, 14), (24, 16, 28, 21)])
@pytest.mark.parametrize("dt_min", [1.0, 0.25])
def test_real_motion_scans_every_required_cell(bbox, dt_min):
    rig = make_coverage_rig(bbox=bbox, start_pose=(6.0, 12.0, 0.0), dt_min=dt_min)
    records = rig.run(max_minutes=240)

    actual = {tuple(cell) for record in records for cell in record["footprint"]}
    required = {
        (col, row)
        for col in range(bbox[0], bbox[2])
        for row in range(bbox[1], bbox[3])
    }

    assert required <= actual
    assert any(record["sar_imaging"] for record in records)
    assert all(
        record["distance_cells"]
        <= record["max_speed_cells_min"] * dt_min + 1e-8
        for record in records
    )
    assert all(not record["obstacle_intersection"] for record in records)
    assert any(record["phase"] == "scanning" for record in records)
    assert all(
        not record["footprint"]
        or (
            record["applied_command"].sensor_mode.value == "sar"
            and record["sar_imaging"]
        )
        for record in records
    )
    assert rig.controller.follower.is_complete


@pytest.mark.parametrize("dt_min", [1.0, 0.25])
def test_reverse_entry_and_vertical_scan_are_guided_by_the_real_route(dt_min):
    rig = make_coverage_rig(
        bbox=(12, 8, 16, 14), start_pose=(6.0, 12.0, math.pi), dt_min=dt_min
    )
    records = rig.run(max_minutes=260)
    actual = {tuple(cell) for record in records for cell in record["footprint"]}
    required = {(col, row) for col in range(12, 16) for row in range(8, 14)}

    assert required <= actual
    assert rig.controller.follower.is_complete
    assert all(record["after_pose"] != record["before_pose"] for record in records)


def test_motion_trace_uses_the_speed_limit_for_that_tick():
    rig = make_coverage_rig(
        bbox=(10, 10, 16, 14), start_pose=(6.0, 12.0, 0.0), dt_min=1.0
    )
    rig.tick()
    low_speed_spec = ActionSpec(-2.0, 2.0, 0.1, 0.1)
    rig.controller._action_spec = low_speed_spec
    rig.safety._action_spec = low_speed_spec

    record = rig.tick()

    assert record["max_speed_cells_min"] == pytest.approx(
        record["applied_command"].speed_cells_min
    )
    assert record["distance_cells"] <= record["max_speed_cells_min"] + 1e-8


def test_transit_guidance_keeps_turning_toward_curved_scan_entry():
    rig = make_coverage_rig(
        bbox=(10, 10, 16, 14), start_pose=(6.0, 12.0, 0.0), dt_min=1.0
    )

    records = [rig.tick() for _ in range(8)]

    assert any(
        abs(record["applied_command"].turn_rate_rad_min) > 1e-9
        for record in records
    )
    assert rig.entity.float_position[1] < 12.0


def test_follower_does_not_jump_to_a_nearby_parallel_scan_line():
    follower = CoverageRouteFollower(
        (
            (0.0, 0.0, 0.0),
            (2.0, 0.0, 0.0),
            (2.0, 0.1, math.pi / 2.0),
            (0.0, 0.1, math.pi),
        ),
        scan_ranges=((0, 1), (2, 3)),
        r_min=1.0,
    )
    spec = ActionSpec(-2.0, 2.0, 0.5, 1.0)

    first = follower.update(
        position=(1.5, 0.0),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )
    near_next_line = follower.update(
        position=(1.8, 0.09),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )

    assert near_next_line.progress_cells < 2.1
    assert near_next_line.scan_segment_index == 0
    assert near_next_line.progress_cells >= first.progress_cells


def test_follower_skips_zero_length_segments_and_honors_asymmetric_turn_limits():
    follower = CoverageRouteFollower(
        ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (4.0, 0.0, 0.0)),
        scan_ranges=((0, 2),),
        r_min=1.0,
    )

    guidance = follower.update(
        position=(0.0, 0.0),
        heading_rad=math.pi / 2.0,
        speed_cells_min=0.5,
        dt_min=1.0,
        action_spec=ActionSpec(-0.3, 0.2, 0.1, 1.0),
    )

    assert guidance.progress_cells == pytest.approx(0.0)
    assert guidance.next_index == 2
    assert guidance.turn_rate_rad_min == pytest.approx(-0.2)


def test_real_sar_imaging_has_entry_and_exit_stability_margins():
    rig = make_coverage_rig(
        bbox=(10, 10, 16, 14), start_pose=(6.0, 12.0, 0.0), dt_min=1.0
    )
    records = rig.run(max_minutes=240)
    route = rig.controller.route
    cumulative = [0.0]
    for start, end in zip(route, route[1:]):
        cumulative.append(cumulative[-1] + math.dist(start[:2], end[:2]))

    for scan_start, scan_end in rig.controller.scan_ranges:
        start_s, end_s = cumulative[scan_start], cumulative[scan_end]
        imaging = [
            record
            for record in records
            if record["sar_imaging"]
            and start_s < record["progress_cells"] < end_s
        ]
        assert imaging
        margin = max(1.0 * rig.dt_min * 160.0 / 10.0 / 60.0, 0.8 / 2.0)
        assert imaging[0]["progress_cells"] - start_s >= margin - 1e-8
        assert end_s - imaging[-1]["progress_cells"] >= margin - 1e-8


def test_safety_intervention_recovers_to_real_sar_footprints():
    rig = make_coverage_rig(
        bbox=(10, 10, 16, 14), start_pose=(6.0, 12.0, 0.0), dt_min=1.0
    )
    for _ in range(40):
        rig.tick()

    interrupted = None
    for _ in range(240):
        candidate = rig.tick(inject_safety_obstacle=True)
        if candidate["safety_obstacle_mask_cells"]:
            interrupted = candidate
            break
    resumed = rig.run(max_minutes=240)

    assert interrupted is not None
    assert interrupted["safety_intervened"] is True
    assert interrupted["safety_interventions"]
    assert interrupted["safety_obstacle_mask_cells"]
    assert any(record["sar_imaging"] and record["footprint"] for record in resumed)


def test_follower_projects_only_in_the_current_local_route_window():
    route = ((0.0, 0.0, 0.0), (5.0, 0.0, 0.0), (5.0, 5.0, math.pi / 2.0))
    follower = CoverageRouteFollower(route, scan_ranges=((0, 1),), r_min=1.0)
    spec = ActionSpec(-2.0, 1.0, 0.5, 2.0)

    first = follower.update(
        position=(0.0, 0.0),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )
    second = follower.update(
        position=(2.0, 0.1),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )

    assert first.progress_cells == pytest.approx(0.0)
    assert second.progress_cells > first.progress_cells
    assert second.next_index == 1
    assert second.turn_rate_rad_min <= 1.0
    assert follower.index == 0


def test_follower_does_not_complete_from_a_lookahead_or_repeated_position():
    follower = CoverageRouteFollower(
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0)),
        scan_ranges=((0, 2),),
        r_min=1.0,
    )
    spec = ActionSpec(-2.0, 2.0, 0.5, 1.0)
    for _ in range(5):
        guidance = follower.update(
            position=(0.0, 0.0),
            heading_rad=0.0,
            speed_cells_min=1.0,
            dt_min=1.0,
            action_spec=spec,
        )
    assert guidance.progress_cells == pytest.approx(0.0)
    assert not guidance.is_complete
    assert not follower.is_complete


def test_nearby_parallel_scan_lines_do_not_allow_a_forward_jump():
    follower = CoverageRouteFollower(
        (
            (0.0, 0.0, 0.0),
            (5.0, 0.0, 0.0),
            (5.0, 0.5, math.pi / 2.0),
            (0.0, 0.5, math.pi),
        ),
        scan_ranges=((0, 1), (2, 3)),
        r_min=1.0,
    )
    spec = ActionSpec(-2.0, 2.0, 0.5, 1.0)

    first = follower.update(
        position=(1.0, 0.0),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )
    near_second_line = follower.update(
        position=(1.0, 0.5),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )

    assert near_second_line.progress_cells == pytest.approx(first.progress_cells)
    assert near_second_line.scan_segment_index == 0
    assert follower.index == 0


def test_follower_recovers_after_a_safe_lateral_avoidance():
    follower = CoverageRouteFollower(
        ((0.0, 0.0, 0.0), (6.0, 0.0, 0.0)), scan_ranges=((0, 1),), r_min=1.0
    )
    spec = ActionSpec(-2.0, 2.0, 0.5, 1.0)
    deviated = follower.update(
        position=(2.0, 0.5),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )
    recovered = follower.update(
        position=(3.0, 0.0),
        heading_rad=0.0,
        speed_cells_min=1.0,
        dt_min=1.0,
        action_spec=spec,
    )

    assert deviated.cross_track_error_cells == pytest.approx(0.5)
    assert recovered.cross_track_error_cells == pytest.approx(0.0)
    assert recovered.progress_cells >= deviated.progress_cells


def test_planner_extends_scan_endpoints_for_actual_sar_along_track():
    bbox = BBox(10, 10, 16, 14)
    planner = CoveragePlanner(sample_step=0.25, near_range=0.25)
    path = planner.plan(
        bbox,
        (6.0, 12.0, 0.0),
        swath_width=1.5,
        R_min=1.0,
        along_track_cells=0.8,
    )
    sensor = SARSensor(swath_width_cells=1.5, near_range_cells=0.25)
    required = {(col, row) for col in range(10, 16) for row in range(10, 14)}
    measured = set()
    for swath in path.swaths:
        for pose in path.waypoints[path.scan_ranges[path.swaths.index(swath)][0] : path.scan_ranges[path.swaths.index(swath)][1] + 1]:
            measured.update(
                (cell.col, cell.row)
                for cell in sensor.compute_swath_footprint(
                    pose[:2], pose[2], swath.look_direction, 0.8
                )
            )
    assert required <= measured
    assert path.swaths[0].start[0] < bbox.col_start
    assert path.swaths[0].end[0] > bbox.col_end

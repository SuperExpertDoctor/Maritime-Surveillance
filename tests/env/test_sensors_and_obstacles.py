import math

from src.env.eo_sensor import EOSensor
from src.env.obstacle import Island, Thunderstorm, obstacle_grid_mask
from src.env.sar_sensor import SARSensor
from src.schedule.datatypes import GridCoord


def test_sar_footprint_is_strictly_side_looking():
    sensor = SARSensor(swath_width_cells=2, near_range_cells=0.25, grid_shape=(12, 12))
    cells = sensor.compute_swath_footprint((5.0, 5.0), 0.0, "right", along_track_cells=2.0)
    assert cells
    assert all(cell.row + 0.5 > 5.0 for cell in cells)
    assert GridCoord(5, 4) not in cells


def test_sar_beam_is_a_side_looking_fan_with_the_uav_as_its_apex():
    sensor = SARSensor(swath_width_cells=2, near_range_cells=0.25)
    beam = sensor.compute_swath_beam((5.0, 5.0), 0.0, "right", along_track_cells=4.0)

    assert len(beam.polygon) == 5
    assert beam.polygon[0] == (5.0, 5.0)
    assert beam.near_range == 0.25
    assert beam.far_range == 2.25
    assert all(point[1] > 5.0 for point in beam.polygon[1:])
    assert math.dist(beam.polygon[1], beam.polygon[-1]) == 4.0
    assert math.dist(beam.polygon[2], beam.polygon[3]) == 4.0


def test_sar_snr_decreases_with_altitude_and_speed():
    baseline = SARSensor.compute_snr(5_000, 160)
    assert SARSensor.compute_snr(10_000, 160) < baseline
    assert SARSensor.compute_snr(5_000, 240) < baseline


def test_eo_fov_points_at_target_and_honours_range():
    sensor = EOSensor(fov_deg=10, max_range_cells=3)
    cone = sensor.compute_fov((2, 2), 0.0, (4, 3))
    assert cone.target == (4.0, 3.0)
    assert len(cone.polygon) == 3
    assert sensor.is_target_visible((2, 2), (4, 3))
    assert not sensor.is_target_visible((2, 2), (10, 10))


def test_square_obstacles_keep_islands_flyable_and_storms_no_fly():
    storm = Thunderstorm((8, 8), size=2, intensity=0.8)
    island = Island((15.5, 4.5), size=3)
    mask = obstacle_grid_mask([storm, island], resolution=(20, 20))
    assert mask[8, 8]
    assert mask[9, 8]  # one-cell square safety margin
    assert not mask[15, 4]  # Island is SAR-flyable for UAVs.
    assert len(island.vertices) == 4
    assert island.contains((15.5, 4.5))
    assert not mask[0, 0]


def test_thunderstorm_moves_and_bounces_inside_map():
    storm = Thunderstorm((18, 10), size=2, move_vector=(2, 0))
    storm.step(1, bounds=(20, 20))
    assert storm.center[0] <= 19
    assert storm.move_vector[0] < 0


def test_expired_thunderstorm_reports_dissipation():
    storm = Thunderstorm((8, 8), size=1, lifetime=0.5)

    assert not storm.step(1.0, bounds=(20, 20))
    assert storm.lifetime == 0.0

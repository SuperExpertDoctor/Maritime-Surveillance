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


def test_obstacle_mask_includes_required_safety_margins():
    storm = Thunderstorm((8, 8), radius=2)
    island = Island([(14, 3), (17, 3), (17, 6), (14, 6)])
    mask = obstacle_grid_mask([storm, island], resolution=(20, 20))
    assert mask[8, 8]
    assert mask[11, 8]  # storm radius + edge safety
    assert mask[15, 4]
    assert not mask[0, 0]


def test_thunderstorm_moves_and_bounces_inside_map():
    storm = Thunderstorm((18, 10), radius=1, move_vector=(2, 0))
    storm.step(1, bounds=(20, 20))
    assert storm.center[0] <= 19
    assert storm.move_vector[0] < 0

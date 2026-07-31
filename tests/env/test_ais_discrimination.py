import math

import pytest

from src.env.ais_signal import AISSignal, generate_ais_signal
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord, Region
from src.utils.ais_discriminator import AISDiscriminator, EOMeasurement


@pytest.mark.parametrize("index", range(20))
def test_position_discriminator_recognizes_civilian_signals(index):
    discriminator = AISDiscriminator(2.0)
    pose = (2.0 + index * 0.1, 3.0 + index * 0.05, math.radians(15 + index))
    target = (4.0 + index * 0.1, 4.5 + index * 0.05)
    bearing = math.atan2(target[1] - pose[1], target[0] - pose[0]) - pose[2]
    measurement = EOMeasurement(bearing, math.dist(pose[:2], target))
    estimated = discriminator.estimate_target_position(pose, measurement)
    signal = AISSignal("123456789", target, 16, 45, "MV Civil", "Cargo", 2.0)

    result = discriminator.discriminate(signal, estimated)

    assert not result.is_military
    assert result.discrepancy_cells == pytest.approx(0.0, abs=1e-8)


@pytest.mark.parametrize("index", range(20))
def test_position_discriminator_recognizes_deceptive_military_signals(index):
    discriminator = AISDiscriminator(2.0)
    pose = (5.0, 5.0, 0.0)
    target = (7.0 + index * 0.02, 5.5)
    measurement = EOMeasurement(math.atan2(target[1] - pose[1], target[0] - pose[0]), math.dist(pose[:2], target))
    estimated = discriminator.estimate_target_position(pose, measurement)
    signal = AISSignal("123456789", (target[0] + 3.0, target[1]), 20, 0, "Unknown", "Cargo", 2.0)

    result = discriminator.discriminate(signal, estimated)

    assert result.is_military
    assert result.discrepancy_cells > 2.0


def test_silent_ais_is_military():
    result = AISDiscriminator(2.0).discriminate(None, (5.0, 5.0))

    assert result.is_military
    assert result.reason == "AIS silent"


def test_engine_waits_for_delay_then_releases_civilian_tracking():
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    group_id = engine.ships[0].group_id
    for ship in engine.ships:
        ship.actual_military = False
        ship.ais_mode = "civilian"
        ship.is_military = None
        ship.discrimination = None
    engine._refresh_ais_signals(1.0)

    center = engine._group_center(group_id)
    uav = engine.uavs[0]
    uav.position = GridCoord(int(round(center[0] - 1)), int(round(center[1])))
    uav._col, uav._row = center[0] - 1.8, center[1]
    uav.heading_rad = 0.0
    uav.status = "tracking"
    uav.target_group_id = group_id
    track = engine.allocator.sm.create_track_region(group_id, GridCoord(int(center[0]), int(center[1])))
    track.assigned_uav_id = uav.id
    engine.allocator.sm.update_uav_status(
        uav.id, "tracking", uav.position,
        assigned_region_id=track.id, target_group_id=group_id,
    )

    engine._update_sensors_and_detections(0.0)
    assert engine.allocator.sm.get_track_region_for_group(group_id) is not None
    engine._update_sensors_and_detections(1.0)
    assert engine.allocator.sm.get_track_region_for_group(group_id) is not None
    engine._update_sensors_and_detections(2.0)

    assert engine.allocator.sm.get_track_region_for_group(group_id) is None
    assert all(ship.is_military is False for ship in engine.ships)
    assert engine.civilian_releases == 1
    assert not engine.allocator.sm.get_active_markers()


def test_departed_target_releases_track_without_marker():
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    group_id = engine.ships[0].group_id
    uav = engine.uavs[0]
    center = engine._group_center(group_id)
    uav.status = "tracking"
    uav.target_group_id = group_id
    track = engine.allocator.sm.create_track_region(group_id, GridCoord(int(center[0]), int(center[1])))
    track.assigned_uav_id = uav.id

    engine._release_departed_group(group_id, 3.0)

    assert engine.allocator.sm.get_track_region_for_group(group_id) is None
    assert not engine.allocator.sm.get_active_markers()
    assert uav.target_group_id is None

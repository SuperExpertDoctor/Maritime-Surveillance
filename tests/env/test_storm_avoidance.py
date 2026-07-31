import math

from src.env.obstacle import Thunderstorm
from src.env.simulation import SimulationEngine
from src.env.uav_entity import UAVEntity
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord
from src.utils.storm_avoider import StormAvoider, ThreatLevel
from src.utils.track_orbit import LGVFTracker


def test_threat_levels_cover_orbit_detour_and_target_cloud_cover():
    avoider = StormAvoider()
    l1 = avoider.detect_threat((5, 5, 0), (7, 5), [Thunderstorm((5, 8), 1)], 0.27)
    l2 = avoider.detect_threat((6.8, 5, 0), (5, 5), [Thunderstorm((8.4, 5), 1)], 0.27)
    l3 = avoider.detect_threat((5, 5, 0), (7, 5), [Thunderstorm((7, 5), 1)], 0.27)

    assert l1.level is ThreatLevel.LEVEL_1
    assert l2.level is ThreatLevel.LEVEL_2
    assert l3.level is ThreatLevel.LEVEL_3


def test_dubins_avoidance_path_stays_in_eo_range_and_outside_storm_margin():
    avoider = StormAvoider()
    target = (10.0, 10.0)
    pose = (9.80897903973024, 7.838694605644181, 1.9360784629304766)
    storm = Thunderstorm((6.995231673956134, 7.640579900475682), 1)

    path = avoider.plan_avoidance(pose, target, [storm], R_min=1.0)

    assert path
    assert all(not storm.contains(point[:2], safety_margin=1.0) for point in path)
    assert all(math.dist(point[:2], target) <= 2.5 + 1e-6 for point in path)


def test_tracking_never_enters_thunderstorm_safety_zone():
    uav = UAVEntity("UAV-test", GridCoord(0, 0), 8, 160)
    uav._col, uav._row = 6.0, 5.0
    uav.heading_rad = 0.0
    uav.status = "tracking"
    storm = Thunderstorm((8.4, 5.0), 1)

    for _ in range(8):
        uav.step(1.0, (5.0, 5.0), storm_zones=[storm])
        assert not storm.contains(uav.float_position, safety_margin=1.0)


def test_lgvf_storm_guidance_preserves_fixed_wing_turn_limit():
    tracker = LGVFTracker(R_min=1.0)
    speed = 0.28
    rate, _ = tracker.compute_guidance(
        (6.0, 5.0, 0.0),
        (5.0, 5.0),
        1.8,
        speed,
        storm_zones=[Thunderstorm((8.4, 5.0), 1)],
    )

    assert abs(rate) <= speed + 1e-9


def test_persistent_level_three_creates_marker_and_releases_track():
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    group_id = engine.ships[0].group_id
    center = engine._group_center(group_id)
    uav = engine.uavs[0]
    uav.status = "tracking"
    uav.target_group_id = group_id
    track = engine.allocator.sm.create_track_region(group_id, GridCoord(int(center[0]), int(center[1])))
    track.assigned_uav_id = uav.id
    uav.avoidance_level = 3

    engine._record_storm_avoidance(uav, 0.0)
    engine._record_storm_avoidance(uav, 3.0)

    assert engine.allocator.sm.get_track_region_for_group(group_id) is None
    assert engine.allocator.sm.get_active_markers()
    assert uav.target_group_id is None

import math

from src.utils.phase_coordinator import PhaseCoordinator
from src.utils.track_orbit import LGVFTracker


def test_lgvf_converges_to_stationary_target_orbit():
    tracker = LGVFTracker(R_min=1)
    path = tracker.compute_waypoints((8, 0, math.pi / 2), (0, 0), 3, dt=0.25, n_steps=160, v_nominal=0.4)
    errors = [abs(math.hypot(x, y) - 3) for x, y, _ in path[-20:]]
    assert sum(errors) / len(errors) < 0.5


def test_guidance_rate_respects_minimum_turn_radius():
    tracker = LGVFTracker(R_min=1.5)
    rate, speed = tracker.compute_guidance((8, 0, math.pi), (0, 0), 3, 0.4)
    assert abs(rate) <= speed / 1.5


def test_orbit_entry_is_a_dubins_path_to_tangent_pose():
    tracker = LGVFTracker(R_min=1)
    entry = tracker.plan_entry((8, 8, 0), (15, 15), 3)
    radius = math.dist(entry.waypoints[-1][:2], (15, 15))
    assert abs(radius - 3) < 1e-6


def test_phase_coordinator_separates_two_uavs():
    coordinator = PhaseCoordinator()
    errors = coordinator.compute_phase_offsets([0.0, 0.2])
    assert errors[0] == 0.0
    assert errors[1] > 0
    speeds = coordinator.adjust_airspeeds(errors, 1.0)
    assert 0.8 <= speeds[0] <= 1.2
    assert 0.8 <= speeds[1] <= 1.2

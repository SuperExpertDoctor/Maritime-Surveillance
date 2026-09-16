"""Vessel-only navigation: reference motion and the path actually executed."""
import importlib.util
import math

import numpy as np
import pytest

from src.env.ship import Ship
from src.mission.contracts import RedMotionParameters
from src.schedule.datatypes import GridCoord


def navigation():
    assert importlib.util.find_spec("src.env.ship_navigation") is not None, "T05 navigator missing"
    from src.env import ship_navigation
    return ship_navigation


def vessel(**kwargs):
    return Ship("V1", GridCoord(5, 15), 18, cell_size_km=1,
                normal_route=((5., 15., 0.), (55., 15., 0.)), **kwargs)


def parameters(offset=0., amplitude=0., phase=0., speed=18., period=12.):
    return RedMotionParameters("V1", offset, speed, amplitude, period, phase)


def test_clear_water_keeps_oscillation_without_astar(monkeypatch):
    nav = navigation()
    ship = vessel()
    planner = nav.ShipNavigator(ship, horizon_min=30)
    def forbidden(*args, **kwargs):
        pytest.fail("clear water must retain its reference, without A*")
    monkeypatch.setattr(planner.astar, "plan_grid", forbidden)
    route = planner.plan(ship.pose, parameters(amplitude=30), 0., 0., np.zeros((70, 40), bool))
    assert route.status == "ready"
    assert route.reference_deviation_cells == 0
    assert route.poses[0] == ship.pose
    assert min(p[2] for p in route.poses) < -.05
    assert max(p[2] for p in route.poses) > .05
    before = ship.pose
    actual = ship.advance(route, 30)
    assert actual == route.poses
    assert ship.pose == actual[-1] != before


def test_reference_phase_uses_install_time_and_fixed_normal_offset():
    nav = navigation()
    ship = vessel()
    planner = nav.ShipNavigator(ship)
    params = parameters(offset=20, amplitude=30, phase=90, period=8)
    planner.install(params, now_min=10.)
    for now in (10., 12., 14., 18.):
        expected = .4 + math.radians(20) + math.radians(30) * math.sin(
            math.pi / 2 + 2 * math.pi * (now - 10) / 8)
        assert planner.reference_heading(params, .4, now) == pytest.approx(expected)
    # Repeated planning of the same installed command must not reset the clock.
    planner.plan(ship.pose, params, .4, 12., np.zeros((70, 40), bool))
    planner.plan(ship.pose, params, .4, 14., np.zeros((70, 40), bool))
    assert planner.reference_heading(params, .4, 14.) == pytest.approx(.4 + math.radians(-10))
    planner.install(params, now_min=14.)  # a new installation explicitly resets phase
    assert planner.reference_heading(params, .4, 14.) == pytest.approx(.4 + math.radians(50))


def test_rolling_replans_equal_one_rollout_with_nonzero_yaw_and_acceleration():
    nav = navigation()
    mask = np.zeros((70, 40), bool)
    params = parameters(offset=25, amplitude=15, speed=24)
    whole, split = vessel(), vessel()
    full = nav.ShipNavigator(whole, horizon_min=4)
    rolling = nav.ShipNavigator(split, horizon_min=4)
    route = full.plan(whole.pose, params, 0., 0., mask)
    whole.advance(route, 4)
    for minute in range(4):
        part = rolling.plan(split.pose, params, 0., float(minute), mask)
        split.advance(part, 1)
    assert split.pose == pytest.approx(whole.pose, abs=1e-10)
    assert split.speed_kn == pytest.approx(whole.speed_kn)
    assert split.turn_rate_deg_min == pytest.approx(whole.turn_rate_deg_min)


def test_advance_obeys_acceleration_yaw_and_returns_every_real_substep():
    nav = navigation()
    ship = vessel()
    planner = nav.ShipNavigator(ship, horizon_min=2, integration_dt_min=.1)
    mask = np.zeros((70, 40), bool)
    elapsed = 0.
    old_speed = ship.speed_kn
    for _ in range(20):
        route = planner.plan(ship.pose, parameters(offset=90, speed=24), 0., elapsed, mask)
        start = ship.pose
        actual = ship.advance(route, .1)
        assert len(actual) == 2 and actual[0] == start
        assert ship.speed_kn - old_speed <= .2 + 1e-10
        delta = abs((actual[-1][2] - start[2] + math.pi) % (2 * math.pi) - math.pi)
        assert delta <= ship.max_turn_rate_rad_per_min * .1 + 1e-10
        assert math.dist(start[:2], actual[-1][:2]) <= 24 * 1.852 / 60 * .1
        old_speed = ship.speed_kn
        elapsed += .1
    assert old_speed == pytest.approx(22)


def test_normal_tangent_follows_route_progress_without_snapping():
    navigation()
    ship = vessel()
    assert hasattr(ship, "normal_tangent_rad"), "normal tangent interface missing"
    assert ship.normal_tangent_rad() == pytest.approx(0.)
    start = ship.pose
    ship.step(.01)
    assert math.dist(start[:2], ship.pose[:2]) <= 18 * 1.852 / 60 * .01 + 1e-10
    assert not ship.departed


def test_normal_route_eventually_crosses_a_boundary_exit_gate_with_inertia():
    nav = navigation()
    mask = np.zeros((30, 30), dtype=bool)
    ship = Ship(
        "V1",
        GridCoord(28, 15),
        18,
        cell_size_km=10,
        normal_route=((28.0, 15.0, 0.0), (29.0, 15.0, 0.0)),
        land_mask=mask,
        navigator=nav.AStarNavigator(),
    )

    for _ in range(300):
        ship.step(1.0)
        if ship.departed:
            break

    assert ship.departed


def assert_water_path(poses, mask, clearance=0.):
    # Independent exact rectangle/segment check, including grid-corner grazing.
    from src.env.obstacle import Island
    for col, row in np.argwhere(mask):
        box = Island((col + .5, row + .5), 1)
        for a, b in zip(poses, poses[1:]):
            assert not box.intersects_segment(a, b), (a, b, col, row)
    for pose in poses:
        assert 0 <= pose[0] < mask.shape[0] and 0 <= pose[1] < mask.shape[1]


@pytest.mark.parametrize("side", [-1, 1])
def test_repairs_island_on_either_side_and_preserves_clear_prefix(side, monkeypatch):
    nav = navigation()
    ship = vessel(max_turn_rate_deg_min=45, yaw_time_constant_min=.3,
                  heading_control_gain_per_min=2, turn_speed_loss_fraction=0)
    mask = np.zeros((40, 32), bool)
    mask[13:16, 13:18] = True
    # A connected wall forces the chosen side of the island.
    if side == 1:
        mask[13:16, :13] = True
    else:
        mask[13:16, 18:] = True
    planner = nav.ShipNavigator(ship, horizon_min=36)
    calls = []
    original = planner.astar.plan_grid
    def record(start, goals, obstacle_mask, r_min, **kwargs):
        calls.append((start, goals, r_min))
        return original(start, goals, obstacle_mask, r_min, **kwargs)
    monkeypatch.setattr(planner.astar, "plan_grid", record)
    reference = planner.plan(ship.pose, parameters(), 0., 0., np.zeros_like(mask))
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    assert calls, "colliding references must use the existing Hybrid A*"
    assert route.status == "ready", route.blocked_reason
    assert route.poses[:20] == reference.poses[:20]
    assert route.reference_deviation_cells > 1.
    assert route.poses[-1][0] > 16
    if side == 1:
        assert max(p[1] for p in route.poses) > 19
    else:
        assert min(p[1] for p in route.poses) < 12
    assert_water_path(route.poses, mask)
    actual = ship.advance(route, sum(c.duration_min for c in route._commands))
    assert actual == route.poses
    assert_water_path(actual, mask)


def test_concave_coast_repair_rolls_out_to_downstream_reference():
    nav = navigation()
    ship = vessel(max_turn_rate_deg_min=60, yaw_time_constant_min=.2,
                  heading_control_gain_per_min=3, turn_speed_loss_fraction=0)
    mask = np.zeros((40, 32), bool)
    mask[13:15, 10:21] = True
    mask[9:15, 10:12] = True
    planner = nav.ShipNavigator(ship, horizon_min=36)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    assert route.status == "ready", route.blocked_reason
    assert route.poses[-1][0] > 16
    assert_water_path(route.poses, mask)
    assert ship.advance(route, sum(c.duration_min for c in route._commands)) == route.poses


def test_no_path_returns_blocked_and_brakes_without_teleporting():
    nav = navigation()
    ship = vessel()
    mask = np.zeros((40, 32), bool)
    mask[13:16, :] = True
    planner = nav.ShipNavigator(ship, horizon_min=30)
    route = planner.plan(ship.pose, parameters(), 0., 7., mask)
    assert route.status == "blocked"
    assert route.blocked_reason
    assert route.generated_at_min == 7.
    actual = ship.advance(route, 30)
    assert ship.navigation_status == "blocked"
    assert ship.blocked_reason == route.blocked_reason
    assert ship.speed_kn == 0
    assert_water_path(actual, mask)
    # 18 knots / 2 knots/min: a 9-minute braking distance, not a jump to the goal.
    assert ship.pose[0] == pytest.approx(5 + .5 * 18 * 9 * 1.852 / 60)


def test_continuous_segment_collision_and_inflated_land_clearance():
    nav = navigation()
    ship = vessel()
    planner = nav.ShipNavigator(ship, clearance_cells=.2)
    mask = np.zeros((40, 32), bool)
    mask[10, 15] = True
    assert not planner.segment_is_safe((9., 15.5), (12., 15.5), mask)
    assert not planner.segment_is_safe((9., 14.9), (12., 14.9), mask)
    assert planner.segment_is_safe((9., 14.7), (12., 14.7), mask)
    # Neither endpoint is land; even touching one exact corner is forbidden.
    assert not planner.segment_is_safe((9., 16.), (11., 14.), mask)


def test_geometric_success_that_real_yaw_cannot_follow_is_blocked(monkeypatch):
    nav = navigation()
    ship = vessel()
    mask = np.zeros((40, 32), bool)
    mask[9:11, 14:17] = True
    planner = nav.ShipNavigator(ship, horizon_min=24)
    # A plausible water-only polyline with turns an inertial vessel cannot execute.
    def impossible(start, goals, *args, **kwargs):
        goal = sorted(goals)[-1]
        return [start, (start[0], 13.8, -math.pi / 2),
                (goal[0], 13.8, 0.), (*goal, math.pi / 2)]
    monkeypatch.setattr(planner.astar, "plan_grid", impossible)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    assert route.status == "blocked"
    assert_water_path(ship.advance(route, 20), mask)


def test_stale_or_unvalidated_route_cannot_move_ship_through_land():
    nav = navigation()
    ship = vessel()
    planner = nav.ShipNavigator(ship)
    route = planner.plan(ship.pose, parameters(), 0., 0., np.zeros((70, 40), bool))
    ship.advance(route, 1)
    start = ship.pose
    ship.advance(route, 1)  # route starts at a now-stale physical state
    assert ship.navigation_status == "blocked"
    assert ship.pose[0] > start[0]  # bounded braking, no reset to route start
    assert ship.speed_kn < 18
    forged = nav.ShipRoute(((0., 0., 0.), (30., 30., 0.)), 0., 0, "ready", None, 0.)
    ship.advance(forged, 1)
    assert ship.navigation_status == "blocked"
    assert ship.pose != forged.poses[-1]


@pytest.mark.parametrize("identity", ["civilian", "target"])
@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_normal_departure_requires_crossing_own_exit_and_ignores_tracking(identity, edge):
    navigation()
    mask = np.zeros((12, 12), bool)
    endpoints = {"left": ((1., 6.), (0., 6.), math.pi),
                 "right": ((10., 6.), (11., 6.), 0.),
                 "top": ((6., 1.), (6., 0.), -math.pi / 2),
                 "bottom": ((6., 10.), (6., 11.), math.pi / 2)}
    start, end, heading = endpoints[edge]
    route = ((*start, heading), (*end, heading))
    ships = [Ship(f"V{i}", GridCoord(1, 1), 18, cell_size_km=1,
                  normal_route=route, truth_identity=identity) for i in range(2)]
    assert hasattr(ships[0], "land_mask"), "ship must retain its real navigation chart"
    for ship in ships:
        ship.land_mask = mask
    ships[1].set_tracked(True)
    for _ in range(50):
        for ship in ships:
            ship.step(.1)
        assert ships[0].pose == ships[1].pose
        inside = 0 <= ships[0].pose[0] < 12 and 0 <= ships[0].pose[1] < 12
        assert ships[0].departed == (not inside)
        if ships[0].departed:
            break
    assert ships[0].departed
    assert abs(ships[0].pose[1 if edge in ("left", "right") else 0] - 6) < .5
    archived = ships[0].pose
    assert ships[0].step(1.) == (archived,)


def test_wrong_boundary_is_blocked_and_cannot_archive_other_vessels():
    nav = navigation()
    ship = Ship("V1", GridCoord(10, 2), 18, cell_size_km=1,
                normal_route=((10., 2., -math.pi / 2), (11., 6., 0.)))
    sibling = vessel()
    planner = nav.ShipNavigator(ship, horizon_min=12)
    route = planner.plan(ship.pose, parameters(), -math.pi / 2, 0., np.zeros((12, 12), bool))
    assert route.status == "blocked"
    ship.advance(route, 12)
    assert not ship.departed and not sibling.departed
    assert ship.pose[1] >= 0


def test_step_honors_exact_island_footprint_and_ignores_clouds():
    navigation()
    from src.env.obstacle import Island, Thunderstorm
    ship = vessel()
    # A tiny main step used to snap across close waypoints without checking islands.
    island = Island((6., 15.), 1)
    actual = ship.step(10., [island, Thunderstorm((5., 15.), 2)])
    assert all(not island.intersects_segment(a, b) for a, b in zip(actual, actual[1:]))
    assert not ship.departed
    clean, cloudy = vessel(), vessel()
    assert clean.step(1.) == cloudy.step(1., [Thunderstorm((5., 15.), 2)])


def test_population_binds_actual_map_navigator_and_dynamics_config():
    navigation()
    from dataclasses import replace
    from src.env.ship import create_ship_population
    from src.control.heuristic.navigation import AStarNavigator
    from src.schedule.config_loader import ConfigLoader
    config = ConfigLoader.load()
    population = replace(
        config.ship.population,
        total_count=1,
        type_i_ratio=1.0,
        type_ii_ratio=0.0,
    )
    config = replace(config, ship=replace(
        config.ship,
        population=population,
        max_acceleration_kn_per_min=.75,
    ))
    mask = np.zeros((30, 30), bool)
    mask[:5, :] = True
    astar = AStarNavigator()
    ship = create_ship_population(config, 417, mask, astar)[0]
    assert hasattr(ship, "land_mask"), "actual population chart was discarded"
    assert np.array_equal(ship.land_mask, mask)
    assert ship.navigator.astar is astar
    assert ship.motion_dynamics.max_acceleration == .75
    assert ship.navigator.horizon_min == config.ship.navigation_horizon_min
    assert ship.navigator.integration_dt_min == config.ship.integration_dt_min
    assert ship.navigator.clearance_cells == config.ship.navigation_clearance_cells


def test_normal_motion_turns_with_route_progress_instead_of_running_past_bend():
    navigation()
    route = ((5., 5., 0.), (7., 5., 0.), (9., 7., math.pi / 4),
             (9., 11., math.pi / 2))
    ship = Ship("V1", GridCoord(5, 5), 18, cell_size_km=1, normal_route=route,
                max_turn_rate_deg_min=45, yaw_time_constant_min=.3,
                heading_control_gain_per_min=2)
    for _ in range(80):
        ship.step(.1)
    assert ship.pose[1] > 5.5
    assert ship.normal_tangent_rad() == pytest.approx(math.pi / 4)


def test_detour_suffix_phase_uses_actual_detour_duration():
    nav = navigation()
    ship = vessel(max_turn_rate_deg_min=60, yaw_time_constant_min=.2,
                  heading_control_gain_per_min=3, turn_speed_loss_fraction=0)
    planner = nav.ShipNavigator(ship, horizon_min=36)
    mask = np.zeros((45, 35), bool)
    mask[13:16, 13:18] = True
    params = parameters(amplitude=8, phase=23)
    planner.install(params, 7.)
    route = planner.plan(ship.pose, params, 0., 9., mask)
    assert route.status == "ready", route.blocked_reason
    assert route.reference_deviation_cells > 1
    elapsed = sum(c.duration_min for c in route._commands[:-1])
    expected = math.radians(8) * math.sin(math.radians(23) + 2 * math.pi * (9 + elapsed - 7) / 12)
    assert route._commands[-1].heading_rad == pytest.approx(expected)


def test_short_horizon_keeps_a_collision_free_stopping_reserve():
    nav = navigation()
    ship = vessel()
    mask = np.zeros((40, 32), bool)
    mask[8, :] = True
    planner = nav.ShipNavigator(ship, horizon_min=1.)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    assert route.status == "blocked", "one clear minute is insufficient if braking afterward hits land"
    ship.advance(route, 10.)
    assert ship.speed_kn == 0
    assert 5 < ship.pose[0] < 7.9


def test_execution_rejects_changed_map_version_and_rechecks_new_island():
    nav = navigation()
    from src.env.obstacle import Island
    ship = vessel()
    planner = nav.ShipNavigator(ship)
    route = planner.plan(ship.pose, parameters(), 0., 0., np.zeros((40, 32), bool))
    planner.map_version += 1
    ship.advance(route, 1.)
    assert ship.navigation_status == "blocked"
    assert ship.speed_kn < 18
    # New chart geometry is checked before any actual displacement.
    route = planner.plan(ship.pose, parameters(), 0., 1., np.zeros((40, 32), bool))
    planner.set_islands([Island((6.5, 15.), 1)])
    actual = ship.advance(route, 1.)
    assert ship.navigation_status == "blocked"
    assert all(not Island((6.5, 15.), 1).intersects_segment(a, b)
               for a, b in zip(actual, actual[1:]))


def test_horizon_exhaustion_brakes_and_returns_the_full_requested_interval():
    nav = navigation()
    ship = vessel()
    planner = nav.ShipNavigator(ship, horizon_min=.2)
    route = planner.plan(ship.pose, parameters(), 0., 20., np.zeros((40, 32), bool))
    actual = ship.advance(route, 10.)
    assert len(actual) == 101
    assert actual[:3] == route.poses
    assert ship._motion_time_min == pytest.approx(30.)
    assert ship.navigation_status == "blocked"
    assert ship.speed_kn == 0
    assert ship.pose[0] == pytest.approx(5 + 18 * .2 * 1.852 / 60 + .5 * 18 * 9 * 1.852 / 60)


def test_execution_rechecks_chart_contents_even_without_version_bump():
    nav = navigation()
    ship = vessel()
    planner = nav.ShipNavigator(ship)
    mask = np.zeros((40, 32), bool)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    mask[9, :] = True
    actual = ship.advance(route, 10.)
    assert ship.navigation_status == "blocked"
    assert ship.speed_kn == 0
    assert_water_path(actual, mask)


def test_exit_requires_segment_crossing_inside_gate_not_only_endpoint():
    nav = navigation()
    ship = Ship("V1", GridCoord(10, 6), 18,
                normal_route=((10., 6., 0.), (11., 6., 0.)))
    planner = nav.ShipNavigator(ship)
    mask = np.zeros((12, 12), bool)
    assert not planner.segment_is_safe((11.99, 6.6), (12.02, 6.4), mask)
    assert planner.segment_is_safe((11.99, 6.4), (12.02, 6.2), mask)


@pytest.mark.parametrize("change", ["replace", "mutate", "island"])
def test_execution_uses_vessels_current_chart_and_obstacles(change):
    from src.env.obstacle import Island
    ship = vessel()
    planner = navigation().ShipNavigator(ship)
    mask = np.zeros((40, 32), bool)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    assert route.status == "ready"
    if change == "mutate":
        ship.land_mask = mask
        ship.land_mask[9, :] = True
    elif change == "replace":
        mask[9, :] = True
        ship.land_mask = mask
        # The planning chart is independent of the executing vessel's chart.
        planner.land_mask = np.zeros_like(mask)
    else:
        ship.land_mask = mask
        ship.navigator.set_islands([Island((9.5, 15.5), 1)])
    actual = ship.advance(route, 10.)
    assert ship.navigation_status == "blocked"
    assert ship.speed_kn == 0
    assert ship._motion_time_min == pytest.approx(10.)
    assert all(ship.navigator.segment_is_safe(a, b, ship.land_mask)
               for a, b in zip(actual, actual[1:]))
    assert ship.pose[0] < 8.9


def test_default_dynamics_repair_reaches_downstream_point_within_horizon(monkeypatch):
    ship = vessel()
    planner = navigation().ShipNavigator(ship, horizon_min=30.)
    mask = np.zeros((70, 40), bool)
    mask[13, 15] = True
    reference = planner.plan(ship.pose, parameters(), 0., 0., np.zeros_like(mask))
    calls = []
    original = planner.astar.plan_grid

    def record(start, goals, *args, **kwargs):
        calls.append((start, goals))
        return original(start, goals, *args, **kwargs)

    monkeypatch.setattr(planner.astar, "plan_grid", record)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    assert calls, "default yaw dynamics must not filter out every downstream A* goal"
    assert all(goal in {p[:2] for p in reference.poses}
               for _, goals in calls for goal in goals)
    assert route.status == "ready", route.blocked_reason
    assert route.reference_deviation_cells > .1
    assert route.poses[-1][0] > 14.
    assert all(planner.segment_is_safe(a, b, mask)
               for a, b in zip(route.poses, route.poses[1:]))
    actual = ship.advance(route, sum(c.duration_min for c in route._commands))
    assert actual == route.poses
    assert planner.can_stop(ship._motion_state(), mask)
    assert_water_path(actual, mask)


def test_old_timestamp_at_current_pose_brakes_without_rewinding_time():
    ship = vessel()
    planner = ship.navigator
    mask = np.zeros((70, 40), bool)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    ship.advance(route, 1.)
    old = planner.plan(ship.pose, parameters(), 0., 0., mask)
    ship.advance(old, 1.)
    assert ship.navigation_status == "blocked"
    assert ship._motion_time_min == pytest.approx(2.)
    assert ship.speed_kn == pytest.approx(16.)


def test_stationary_route_cannot_be_replayed_after_time_advances():
    ship = vessel()
    ship.speed_kn = 0.
    route = ship.navigator.plan(ship.pose, parameters(speed=0), 0., 0., ship.land_mask)
    ship.advance(route, 1.)
    assert ship.pose == route.poses[0]
    ship.advance(route, 1.)
    assert ship.navigation_status == "blocked"
    assert ship._motion_time_min == pytest.approx(2.)


@pytest.mark.parametrize("supersede", ["opposite", "equal", "normal", "replan", "other_navigator"])
def test_superseded_route_brakes_and_fresh_replan_remains_executable(supersede):
    ship = vessel()
    planner = ship.navigator
    mask = np.zeros((70, 40), bool)
    params = parameters(offset=60)
    old = planner.plan(ship.pose, params, 0., 0., mask)
    replacement = parameters(offset=-60)
    if supersede == "equal":
        replacement = params
    elif supersede == "normal":
        replacement = None
    if supersede == "other_navigator":
        navigation().ShipNavigator(ship).install(replacement, 0.)
    elif supersede == "replan":
        planner.plan(ship.pose, params, 0., 0., mask)
        replacement = params
    else:
        planner.install(replacement, 0.)
    ship.advance(old, 1.)
    assert ship.navigation_status == "blocked"
    assert ship.heading_rad == pytest.approx(0.)
    assert ship.speed_kn == pytest.approx(16.)
    assert ship._motion_time_min == pytest.approx(1.)
    fresh = planner.plan(ship.pose, replacement, 0., 1., mask)
    actual = ship.advance(fresh, .1)
    assert ship.navigation_status == "ready"
    assert actual == fresh.poses[:2]


@pytest.mark.parametrize("changed_params", [False, True])
def test_command_installation_epoch_is_shared_across_navigators(changed_params):
    ship = vessel()
    first = ship.navigator
    second = navigation().ShipNavigator(ship)
    params = parameters(amplitude=30, phase=20, period=8)
    first.install(parameters(amplitude=10) if changed_params else params, 1.)
    second.install(params, 4.)
    for planner, now in ((first, 5.), (navigation().ShipNavigator(ship), 6.), (second, 7.)):
        generation = ship._navigation_generation
        route = planner.plan(ship.pose, params, 0., now, ship.land_mask)
        expected = math.radians(30) * math.sin(math.radians(20) + 2 * math.pi * (now - 4) / 8)
        assert route._commands[0].heading_rad == pytest.approx(expected)
        assert ship._navigation_generation == generation + 1, "equal rolling plans do not reinstall"
        assert ship.advance(route, .1) == route.poses[:2]
        assert ship.navigation_status == "ready"
    # Returning to normal mode still installs once, then rolls normally.
    ship.step(.1)
    generation = ship._navigation_generation
    ship.step(.1)
    assert ship.navigation_status == "ready"
    assert ship._navigation_generation == generation + 1


@pytest.mark.parametrize("created_before_update", [False, True])
@pytest.mark.parametrize("owned_navigator_exists", [False, True])
@pytest.mark.parametrize("pass_old_chart", [False, True])
def test_external_planner_uses_current_vessel_chart_and_version(
        created_before_update, owned_navigator_exists, pass_old_chart):
    ship = vessel()
    if owned_navigator_exists:
        ship.navigator
    old_chart = ship.land_mask
    planner = navigation().ShipNavigator(ship) if created_before_update else None
    updated = np.zeros_like(old_chart)
    updated[20, 20] = True
    ship.land_mask = updated
    if planner is None:
        planner = navigation().ShipNavigator(ship)
    route = planner.plan(ship.pose, parameters(), 0., 0.,
                         old_chart if pass_old_chart else ship.land_mask)
    assert route.status == "ready"
    assert route.map_version > 0
    assert route.map_version == ship.navigator.map_version
    assert np.array_equal(route._land_mask, ship.land_mask)
    assert ship.advance(route, .1) == route.poses[:2]
    assert ship.navigation_status == "ready"
    assert ship.speed_kn == pytest.approx(18.)
    # Equal chart replacement still invalidates a recipe by version.
    fresh = planner.plan(ship.pose, parameters(), 0., .1, ship.land_mask)
    ship.land_mask = ship.land_mask
    ship.advance(fresh, .1)
    assert ship.navigation_status == "blocked"
    assert ship.speed_kn < 18.
    fresh = planner.plan(ship.pose, parameters(), 0., .2, ship.land_mask)
    ship.land_mask[20, 21] = True
    assert fresh.map_version == planner.map_version
    ship.advance(fresh, .1)
    assert ship.navigation_status == "blocked"


@pytest.mark.parametrize("normal_mode", [False, True])
@pytest.mark.parametrize("rolling_execution", [False, True])
def test_actual_defaults_rolling_repair_looks_beyond_horizon(
        normal_mode, rolling_execution, monkeypatch):
    ship = Ship("V1", GridCoord(9, 15), 18,
                normal_route=((9., 15., 0.), (29., 15., 0.)))
    planner = ship.navigator
    mask = np.zeros((30, 30), bool)
    mask[13, 15] = True
    ship.land_mask = mask
    params = None if normal_mode else parameters()
    calls = []
    original = planner.astar.plan_grid

    def record(start, goals, inflated, *args, **kwargs):
        assert planner.segment_is_safe(start, start, inflated)
        assert all(planner.segment_is_safe(goal, goal, inflated) for goal in goals)
        calls.append((start, goals))
        return original(start, goals, inflated, *args, **kwargs)

    monkeypatch.setattr(planner.astar, "plan_grid", record)
    assert ship.cell_size_km == 10.
    assert planner.horizon_min == 8.
    assert planner.clearance_cells == .1
    for _ in range(100):
        route = planner.plan(ship.pose, params, 0., ship._motion_time_min, mask)
        if calls or route.status == "blocked":
            break
        assert sum(c.duration_min for c in route._commands) == pytest.approx(8.)
        assert ship.advance(route, 1.) == route.poses[:11]
    assert calls, "rolling replans must reach A* before losing the turn approach"
    assert route.status == "ready", route.blocked_reason
    assert route.poses[-1][0] > 15.
    assert route.reference_deviation_cells > .1
    # The approach is retained exactly; only the necessary turn leaves it.
    assert calls[0][0][0] > route.poses[0][0] + .1
    np.testing.assert_allclose(route.poses[:20], [
        (route.poses[0][0] + i * 18 * 1.852 / 60 / 10 * .1, 15., 0.)
        for i in range(20)], atol=1e-12, rtol=0)
    for _, goals in calls:
        for goal in goals:
            assert goal[1] == 15.
            reference_steps = (goal[0] - route.poses[0][0]) / (18 * 1.852 / 60 / 10 * .1)
            assert reference_steps == pytest.approx(round(reference_steps))
            assert 8. < reference_steps * .1 <= 128.
    from src.env.obstacle import Island
    expanded_obstacle = Island((13.5, 15.5), 1.2)
    state = ship._motion_state()
    for command, expected_pose in zip(route._commands, route.poses[1:]):
        rolled = ship.motion_dynamics.roll(state, command.heading_rad, command.speed_kn,
                                           command.duration_min)
        assert rolled.pose == expected_pose
        assert abs(rolled.speed_kn - state.speed_kn) <= 2 * command.duration_min + 1e-10
        assert abs(rolled.yaw_rate) <= ship.max_turn_rate_rad_per_min
        assert math.dist(state.pose[:2], rolled.pose[:2]) <= 18 * 1.852 / 60 / 10 * command.duration_min + 1e-12
        assert not expanded_obstacle.intersects_segment(state.pose, rolled.pose)
        state = rolled
    assert planner.can_stop(state, mask)
    if rolling_execution:
        for _ in range(150):
            actual = ship.advance(route, 1.)
            assert actual == route.poses[:11]
            assert ship.navigation_status == "ready"
            assert all(not expanded_obstacle.intersects_segment(a, b)
                       for a, b in zip(actual, actual[1:]))
            if ship.pose[0] > 15.5:
                break
            route = planner.plan(ship.pose, params, 0., ship._motion_time_min, mask)
            assert route.status == "ready", route.blocked_reason
        assert ship.pose[0] > 15.5, "rolling execution must get past the obstacle"
    else:
        assert ship.advance(route, sum(c.duration_min for c in route._commands)) == route.poses
    assert ship.navigation_status == "ready"


def bending_vessel(horizon=20.):
    return Ship(
        "V1", GridCoord(5, 5), 18, cell_size_km=1,
        normal_route=tuple((x, y, 0.) for x, y in
                           ((5, 5), (6, 5), (6, 6), (5, 6), (5, 9), (20, 9))),
        max_turn_rate_deg_min=90, yaw_time_constant_min=.1,
        heading_control_gain_per_min=5, turn_speed_loss_fraction=0,
        navigation_horizon_min=horizon, navigation_clearance_cells=0)


@pytest.mark.parametrize("obstacle", [False, True])
def test_bending_normal_route_execution_retains_progress_on_replan(obstacle):
    ship = bending_vessel()
    mask = np.zeros((30, 30), bool)
    if obstacle:
        mask[4, 7] = True
    ship.land_mask = mask
    ship.step(20.)
    assert ship.navigation_status == "ready"
    assert ship.pose[0] > 8. and ship.pose[1] > 8.
    assert ship._route_index == 5, "execution must retain progress onto the final eastbound leg"
    for _ in range(3):
        tangent = ship.normal_tangent_rad()
        assert tangent == pytest.approx(0.)
        route = ship.navigator.plan(ship.pose, None, tangent, ship._motion_time_min, mask)
        assert route.status == "ready", route.blocked_reason
        assert abs(route._commands[0].heading_rad) < .2, "must not target passed waypoint (5, 6)"
        x = ship.pose[0]
        ship.advance(route, 1.)
        assert ship.pose[0] > x
        assert ship._route_index == 5


@pytest.mark.parametrize("duration", [.05, 1., 20.])
def test_normal_progress_only_commits_executed_route_prefix(duration):
    ship = bending_vessel()
    route = ship.navigator.plan(ship.pose, None, ship.normal_tangent_rad(), 0., ship.land_mask)
    assert ship._route_index == 1, "prediction must not commit future progress"
    ship.advance(route, duration)
    assert ship._route_index == (5 if duration == 20. else 1)


@pytest.mark.parametrize("finish_rejoin", [False, True])
def test_detour_progress_waits_for_executed_rejoin(finish_rejoin):
    ship = bending_vessel(8.)
    mask = np.zeros((30, 30), bool)
    mask[4, 7] = True
    ship.land_mask = mask
    route = ship.navigator.plan(ship.pose, None, ship.normal_tangent_rad(), 0., mask)
    assert route.status == "ready", route.blocked_reason
    duration = sum(c.duration_min for c in route._commands)
    ship.advance(route, duration if finish_rejoin else duration - .15)
    # Just before the rejoin, the fractional substep is still east of the
    # westbound waypoint's crossing plane; it cannot claim downstream progress.
    assert ship._route_index == (4 if finish_rejoin else 3)
    assert (ship.pose[0] < 5.) == finish_rejoin


def test_rejected_normal_route_does_not_commit_its_future_progress():
    ship = bending_vessel()
    route = ship.navigator.plan(ship.pose, None, ship.normal_tangent_rad(), 0., ship.land_mask)
    ship.navigator.install(parameters(), 0.)
    ship.advance(route, 1.)
    assert ship.navigation_status == "blocked"
    assert ship._route_index == 1


def test_parameter_route_keeps_geometric_normal_cursor_semantics():
    ship = bending_vessel()
    route = ship.navigator.plan(ship.pose, parameters(), 0., 0., ship.land_mask)
    assert not route._normal_indices
    ship.advance(route, 20.)
    # Eastbound red motion passes (6, 5), but never follows the normal bends.
    assert ship.pose[0] > 16. and ship.pose[1] == pytest.approx(5.)
    assert ship._route_index == 2
    assert ship.normal_tangent_rad() == pytest.approx(math.pi / 2)


@pytest.mark.parametrize("horizon", [8., 20.])
def test_bending_normal_route_preserves_progress_through_obstacle_repair(horizon, monkeypatch):
    ship = bending_vessel(horizon)
    planner = ship.navigator
    mask = np.zeros((30, 30), bool)
    mask[4, 7] = True
    ship.land_mask = mask
    goals_seen = []
    original = planner.astar.plan_grid

    def record(start, goals, *args, **kwargs):
        goals_seen.extend(goals)
        return original(start, goals, *args, **kwargs)

    monkeypatch.setattr(planner.astar, "plan_grid", record)
    route = planner.plan(ship.pose, None, ship.normal_tangent_rad(), 0., mask)
    assert ship._route_index == 1, "prediction must not advance the physical vessel's cursor"
    assert goals_seen, "the obstacle must exercise reference repair"
    # The 8-minute horizon ends inside the obstacle, so goals require extension.
    # All rejoins must be north of it, never back toward the earlier (6, 5) leg.
    assert all(y > 8. for x, y in goals_seen), goals_seen
    assert route.status == "ready", route.blocked_reason
    assert route.poses[-1][1] > 7.8
    if horizon == 20.:
        # This horizon also regenerates a suffix after the detour. It must
        # continue east toward (20, 9), rather than turn back toward (5, 6).
        assert route.poses[-1][0] > 8.
        assert abs(route._commands[-1].heading_rad) < .2
    state = ship._motion_state()
    for command, expected in zip(route._commands, route.poses[1:]):
        rolled = ship.motion_dynamics.roll(
            state, command.heading_rad, command.speed_kn, command.duration_min)
        assert rolled.pose == expected
        assert math.dist(state.pose[:2], rolled.pose[:2]) <= (
            18 * 1.852 / 60 * command.duration_min + 1e-12)
        assert abs(rolled.yaw_rate) <= ship.max_turn_rate_rad_per_min
        state = rolled
    assert planner.can_stop(state, mask)
    actual = ship.advance(route, sum(c.duration_min for c in route._commands))
    assert actual == route.poses
    assert ship.navigation_status == "ready"
    assert_water_path(actual, mask)
    assert ship._route_index >= 4, "the executed rejoin must persist its downstream cursor"
    next_route = planner.plan(ship.pose, None, ship.normal_tangent_rad(), ship._motion_time_min, mask)
    assert next_route.status == "ready", next_route.blocked_reason
    assert abs(next_route._commands[0].heading_rad) < math.pi / 2


def test_default_repair_blocks_when_bounded_reference_cannot_rejoin(monkeypatch):
    ship = Ship("V1", GridCoord(11, 15), 18,
                normal_route=((11., 15., 0.), (39., 15., 0.)))
    planner = ship.navigator
    mask = np.zeros((40, 32), bool)
    mask[13:30, 15] = True  # Water exists downstream, beyond the repair budget.
    sampled_times = []
    original = planner.reference_heading

    def record(params, tangent, now):
        sampled_times.append(now)
        return original(params, tangent, now)

    monkeypatch.setattr(planner, "reference_heading", record)
    route = planner.plan(ship.pose, parameters(), 0., 0., mask)
    assert route.status == "blocked"
    assert 8. < max(sampled_times) <= 128.
    assert len(sampled_times) <= 1281
    actual = ship.advance(route, 10.)
    assert ship.speed_kn == 0.
    assert 11. < ship.pose[0] < 12.9
    assert_water_path(actual, mask)


@pytest.mark.parametrize("normal_mode", [False, True])
def test_actual_defaults_clear_plan_keeps_configured_horizon(normal_mode, monkeypatch):
    ship = Ship("V1", GridCoord(9, 15), 18,
                normal_route=((9., 15., 0.), (29., 15., 0.)))
    planner = ship.navigator
    mask = np.zeros((30, 30), bool)
    mask[13, 20] = True  # A nearby obstacle off the reference must not lengthen it.

    def forbidden(*args, **kwargs):
        pytest.fail("a clear reference does not need A*")

    monkeypatch.setattr(planner.astar, "plan_grid", forbidden)
    route = planner.plan(ship.pose, None if normal_mode else parameters(amplitude=20),
                         0., 0., mask)
    assert route.status == "ready"
    assert sum(c.duration_min for c in route._commands) == pytest.approx(8.)
    assert route.reference_deviation_cells == 0.

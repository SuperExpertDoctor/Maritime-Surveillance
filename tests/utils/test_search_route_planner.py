from concurrent.futures import ProcessPoolExecutor

import numpy as np

from src.utils.obstacle_avoider import ObstacleAvoider
from src.utils.search_route_planner import SearchRouteRequest, plan_search_route


def _request(uav_id, start_pose):
    return SearchRouteRequest(
        uav_id=uav_id,
        start_pose=start_pose,
        bbox=(4, 4, 12, 12),
        swath_width=2.0,
        r_min=1.0,
        obstacle_mask=np.zeros((30, 30), dtype=bool),
        unscanned_mask=np.ones((30, 30), dtype=bool),
        allow_revisit=False,
        seed=31,
    )


def test_search_route_plan_is_obstacle_safe_and_contains_scan_ranges():
    request = _request("UAV-1", (1.0, 1.0, 0.0))

    plan = plan_search_route(request)

    assert plan.uav_id == request.uav_id
    assert plan.scanned_swath_count == len(plan.scan_ranges)
    assert plan.transit_end_index > 0
    assert ObstacleAvoider().is_path_safe(plan.path, request.obstacle_mask)


def test_search_route_plans_are_picklable_for_parallel_workers():
    requests = [
        _request("UAV-1", (1.0, 1.0, 0.0)),
        _request("UAV-2", (2.0, 1.0, 0.0)),
    ]

    with ProcessPoolExecutor(max_workers=2) as executor:
        plans = list(executor.map(plan_search_route, requests))

    assert [plan.uav_id for plan in plans] == ["UAV-1", "UAV-2"]
    assert all(plan.path for plan in plans)


def test_search_route_skips_blocked_scan_legs_instead_of_failing():
    request = _request("UAV-1", (1.0, 1.0, 0.0))
    obstacle_mask = np.zeros((30, 30), dtype=bool)
    # Block the interior columns of the bbox so some scan legs are unflyable.
    obstacle_mask[6:9, 4:12] = True
    request = SearchRouteRequest(
        uav_id=request.uav_id,
        start_pose=request.start_pose,
        bbox=request.bbox,
        swath_width=request.swath_width,
        r_min=request.r_min,
        obstacle_mask=obstacle_mask,
        unscanned_mask=request.unscanned_mask,
        allow_revisit=request.allow_revisit,
        seed=request.seed,
    )

    plan = plan_search_route(request)
    clean_plan = plan_search_route(_request("UAV-1", (1.0, 1.0, 0.0)))

    assert 0 < plan.scanned_swath_count < clean_plan.scanned_swath_count
    assert plan.scanned_swath_count == len(plan.scan_ranges)
    assert ObstacleAvoider().is_path_safe(plan.path, obstacle_mask)


def test_search_route_skips_unreachable_swaths_instead_of_failing():
    """A swath whose entry RRT* cannot reach must not veto the whole
    region — the reachable swaths are still served."""
    obstacle_mask = np.zeros((30, 30), dtype=bool)
    # Storm-margin wall immediately NW of the region: some swath entries
    # are unreachable by Dubins connectors.
    obstacle_mask[15:19, 18:23] = True
    request = SearchRouteRequest(
        uav_id="UAV-1",
        start_pose=(1.5, 11.5, 0.0),
        bbox=(19, 22, 24, 27),
        swath_width=1.2,
        r_min=0.5,
        obstacle_mask=obstacle_mask,
        unscanned_mask=np.ones((30, 30), dtype=bool),
        allow_revisit=False,
        seed=42,
    )

    plan = plan_search_route(request)

    assert plan.scanned_swath_count > 0
    assert ObstacleAvoider().is_path_safe(plan.path, obstacle_mask)


def test_search_route_all_blocked_legs_returns_empty_plan():
    obstacle_mask = np.zeros((30, 30), dtype=bool)
    obstacle_mask[3:14, 3:14] = True
    request = SearchRouteRequest(
        uav_id="UAV-1",
        start_pose=(20.0, 20.0, 0.0),
        bbox=(4, 4, 12, 12),
        swath_width=2.0,
        r_min=1.0,
        obstacle_mask=obstacle_mask,
        unscanned_mask=np.ones((30, 30), dtype=bool),
        allow_revisit=False,
        seed=31,
    )

    plan = plan_search_route(request)

    assert plan.scanned_swath_count == 0
    assert plan.scan_ranges == ()


def test_search_route_grounded_start_heading_does_not_veto_departure():
    """A grounded airframe can pivot before takeoff: parked heading 196°
    at a coastal base vetoes every departure arc, but with
    start_heading_free the planner reorients the start and still plans."""
    mask = np.zeros((30, 30), dtype=bool)
    locked = SearchRouteRequest(
        uav_id="UAV-9",
        start_pose=(1.0, 11.0, np.radians(196.4)),
        bbox=(13, 6, 21, 17),
        swath_width=3.0,
        r_min=1.7,
        obstacle_mask=mask,
        unscanned_mask=np.zeros((30, 30), dtype=bool),
        allow_revisit=True,
        seed=42,
    )
    assert plan_search_route(locked).scanned_swath_count == 0

    free = SearchRouteRequest(
        uav_id="UAV-9",
        start_pose=(1.0, 11.0, np.radians(196.4)),
        bbox=(13, 6, 21, 17),
        swath_width=3.0,
        r_min=1.7,
        obstacle_mask=mask,
        unscanned_mask=np.zeros((30, 30), dtype=bool),
        allow_revisit=True,
        seed=42,
        start_heading_free=True,
    )
    plan = plan_search_route(free)
    assert plan.scanned_swath_count > 0
    assert plan.path[0][:2] == (1.0, 11.0)
    assert plan.path[0][2] != locked.start_pose[2]
    assert ObstacleAvoider().is_path_safe(plan.path, mask)

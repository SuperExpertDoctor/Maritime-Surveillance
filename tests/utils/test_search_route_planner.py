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

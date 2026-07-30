import math
from typing import Optional
from schedule.datatypes import GridCoord, BBox


def _bbox_center(bbox: BBox) -> tuple[float, float]:
    return (
        (bbox.col_start + bbox.col_end) / 2.0,
        (bbox.row_start + bbox.row_end) / 2.0,
    )


def _hungarian_greedy(n_uavs: int, n_regions: int,
                      cost: list[list[float]],
                      uavs: list[dict], regions: list[dict]) -> list[tuple[str, str]]:
    """Greedy minimum-cost pairing (fallback when scipy unavailable)."""
    assignments: list[Optional[int]] = [None] * n_uavs
    region_taken = [False] * n_regions

    pairs = []
    for i in range(n_uavs):
        for j in range(n_regions):
            pairs.append((cost[i][j], i, j))
    pairs.sort(key=lambda x: x[0])

    for _, i, j in pairs:
        if assignments[i] is None and not region_taken[j]:
            assignments[i] = j
            region_taken[j] = True

    result = []
    for i, j in enumerate(assignments):
        if j is not None:
            result.append((uavs[i]["id"], regions[j]["id"]))
    return result


def hungarian_pair(uavs: list[dict], regions: list[dict]) -> list[tuple[str, str]]:
    """Hungarian 算法最小总距离配对（scipy 最优；回退到贪心）。

    输入:
      uavs: [{"id": str, "position": GridCoord}, ...]
      regions: [{"id": str, "bbox": BBox}, ...]
    输出:
      [(uav_id, region_id), ...]
    """
    n_uavs = len(uavs)
    n_regions = len(regions)

    if n_uavs == 0 or n_regions == 0:
        return []

    # 构建代价矩阵
    n = max(n_uavs, n_regions)
    cost = [[0.0] * n for _ in range(n)]

    for i, uav in enumerate(uavs):
        for j, region in enumerate(regions):
            cx, cy = _bbox_center(region["bbox"])
            dx = uav["position"].col - cx
            dy = uav["position"].row - cy
            cost[i][j] = math.sqrt(dx * dx + dy * dy)

    # 尝试 scipy 最优 Hungarian
    try:
        from scipy.optimize import linear_sum_assignment
        import numpy as np
        cost_mat = np.array([row[:n_regions] for row in cost[:n_uavs]], dtype=np.float64)
        row_ind, col_ind = linear_sum_assignment(cost_mat)
        result = []
        for i, j in zip(row_ind, col_ind):
            if i < n_uavs and j < n_regions:
                result.append((uavs[i]["id"], regions[j]["id"]))
        return result
    except ImportError:
        pass

    # 回退到贪心配对
    return _hungarian_greedy(n_uavs, n_regions, cost, uavs, regions)

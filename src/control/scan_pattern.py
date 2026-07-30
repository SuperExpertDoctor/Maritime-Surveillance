"""覆盖扫描模式：弓形扫描（Boustrophedon pattern）。"""
from src.schedule.datatypes import GridCoord, BBox


def generate_scan_waypoints(bbox: BBox, swath_cells: int = 1) -> list[GridCoord]:
    """生成弓形扫描航路点序列。

    扫描方向：沿 row 方向来回，col 方向步进。
    swath_cells: SAR 条带宽度对应的 cell 数 (15km / 10km ≈ 1-2)。
    """
    waypoints = []
    c_start, r_start, c_end, r_end = bbox
    left_to_right = True

    for c in range(c_start, c_end, swath_cells):
        if left_to_right:
            waypoints.append(GridCoord(c, r_start))
            waypoints.append(GridCoord(c, r_end - 1))
        else:
            waypoints.append(GridCoord(c, r_end - 1))
            waypoints.append(GridCoord(c, r_start))
        left_to_right = not left_to_right

    return waypoints


def estimate_coverage_time(bbox: BBox, cruise_speed_kmh: float,
                           sar_swath_km: int, cell_size_km: int = 10,
                           efficiency: float = 0.75) -> float:
    """估算覆盖 bbox 所需时间（单位：分钟）。"""
    w_cells = bbox.col_end - bbox.col_start
    h_cells = bbox.row_end - bbox.row_start
    area_km2 = w_cells * h_cells * cell_size_km * cell_size_km
    coverage_rate_km2h = cruise_speed_kmh * sar_swath_km * efficiency
    hours = area_km2 / coverage_rate_km2h
    return hours * 60.0

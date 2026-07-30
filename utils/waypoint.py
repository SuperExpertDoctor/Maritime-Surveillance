"""航路点计算：当前位置 → 目标区域的简单直线路径。"""
import math
from schedule.datatypes import GridCoord, BBox


def navigate_to_region(current: GridCoord, target_bbox: BBox,
                       cell_size_km: float = 10.0) -> list[GridCoord]:
    """生成从当前位置到目标区域中心的直线航路点。"""
    cx = (target_bbox.col_start + target_bbox.col_end) / 2.0
    cy = (target_bbox.row_start + target_bbox.row_end) / 2.0
    # 简单直线路径：起点 → 终点
    waypoints = [current]
    # 在途中添加中间点（如果需要避开障碍等，此处简化）
    waypoints.append(GridCoord(int(cx), int(cy)))
    return waypoints


def grid_distance(a: GridCoord, b: GridCoord) -> float:
    """网格坐标间的欧氏距离（单位：格）。"""
    return math.sqrt((a.col - b.col) ** 2 + (a.row - b.row) ** 2)


def travel_time(a: GridCoord, b: GridCoord, cruise_speed_kmh: float,
                cell_size_km: float = 10.0) -> float:
    """计算两点间的飞行时间（单位：分钟）。"""
    dist_km = grid_distance(a, b) * cell_size_km
    return (dist_km / cruise_speed_kmh) * 60.0

"""传感器朝向控制：SAR/光电/雷帧的简单朝向规则。"""
import math
from src.schedule.datatypes import GridCoord


def sensor_heading_for_search(uav_position: GridCoord,
                               next_waypoint: GridCoord) -> float:
    """覆盖搜索时传感器指向前进方向。

    Args:
        uav_position: UAV 当前网格坐标
        next_waypoint: 下一个航路点坐标

    Returns:
        朝向角度 (度)，0° = 正东，90° = 正北
    """
    dx = next_waypoint.col - uav_position.col
    dy = next_waypoint.row - uav_position.row
    return math.degrees(math.atan2(dy, dx))


def sensor_heading_for_track(uav_position: GridCoord,
                              target_position: GridCoord) -> float:
    """跟踪时传感器始终指向目标。

    Args:
        uav_position: UAV 当前网格坐标
        target_position: 目标当前网格坐标

    Returns:
        朝向角度 (度)，0° = 正东，90° = 正北
    """
    dx = target_position.col - uav_position.col
    dy = target_position.row - uav_position.row
    return math.degrees(math.atan2(dy, dx))

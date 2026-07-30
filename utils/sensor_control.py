"""传感器朝向控制：SAR/光电/雷帧的简单朝向规则。"""
from schedule.datatypes import GridCoord


def sensor_heading_for_search(uav_position: GridCoord,
                               next_waypoint: GridCoord) -> float:
    """覆盖搜索时传感器指向前进方向。"""
    import math
    dx = next_waypoint.col - uav_position.col
    dy = next_waypoint.row - uav_position.row
    return math.degrees(math.atan2(dy, dx))


def sensor_heading_for_track(uav_position: GridCoord,
                              target_position: GridCoord) -> float:
    """跟踪时传感器始终指向目标。"""
    import math
    dx = target_position.col - uav_position.col
    dy = target_position.row - uav_position.row
    return math.degrees(math.atan2(dy, dx))

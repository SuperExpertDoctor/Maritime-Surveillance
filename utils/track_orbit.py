"""目标跟踪盘旋：围绕目标保持一定距离的圆形/椭圆形轨迹。"""
import math
from schedule.datatypes import GridCoord


def generate_orbit_waypoints(target_position: GridCoord,
                             standoff_cells: float = 3.0,
                             num_points: int = 8) -> list[GridCoord]:
    """生成围绕目标的盘旋航路点。

    standoff_cells: 与目标保持的距离（格）。
    """
    waypoints = []
    for i in range(num_points):
        angle = 2 * math.pi * i / num_points
        col = target_position.col + standoff_cells * math.cos(angle)
        row = target_position.row + standoff_cells * math.sin(angle)
        waypoints.append(GridCoord(int(col), int(row)))
    return waypoints


def update_orbit_center(old_waypoints: list[GridCoord],
                        target_displacement: tuple[float, float]) -> list[GridCoord]:
    """根据目标位移更新盘旋中心。"""
    dc, dr = target_displacement
    return [GridCoord(wp.col + int(dc), wp.row + int(dr)) for wp in old_waypoints]

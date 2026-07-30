"""返航路径规划：当前位置 → 基地的最短直线路径。"""
from schedule.datatypes import GridCoord


def return_to_base(current: GridCoord, base_position: GridCoord) -> list[GridCoord]:
    """生成返航航路点。"""
    return [current, base_position]

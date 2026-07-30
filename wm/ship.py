"""目标船舶实体：宙斯盾驱逐舰级别，支持 zigzag 逃逸。"""
import math
import random
from schedule.datatypes import GridCoord


class Ship:
    def __init__(self, ship_id: str, initial_position: GridCoord,
                 speed_kn: float, zigzag_amplitude_km: float,
                 zigzag_period_min: float, cell_size_km: float = 10.0):
        self.id = ship_id
        self.position = initial_position
        self.speed_kn = speed_kn  # 节
        self.speed_km_per_min = speed_kn * 1.852 / 60.0  # km/min
        self.zigzag_amplitude_km = zigzag_amplitude_km
        self.zigzag_period_min = zigzag_period_min
        self.cell_size_km = cell_size_km
        self._detected: bool = False
        self._zigzag_phase: float = random.uniform(0, 2 * math.pi)
        self._base_heading: float = random.uniform(0, 2 * math.pi)
        self.group_id: str | None = None

    @property
    def detected(self) -> bool:
        return self._detected

    def mark_detected(self) -> None:
        self._detected = True

    def step(self, dt_min: float) -> None:
        """推进 dt 分钟。"""
        if not self._detected:
            # 未被发现前可能漂移
            return

        # Zigzag 逃逸
        t = self._zigzag_phase
        self._zigzag_phase += dt_min / self.zigzag_period_min * 2 * math.pi

        # 横向偏移（zigzag）
        lateral_offset = self.zigzag_amplitude_km * math.sin(self._zigzag_phase)

        # 沿基本方向前进
        forward_dist = self.speed_km_per_min * dt_min
        dx_km = forward_dist * math.cos(self._base_heading) - lateral_offset * math.sin(self._base_heading)
        dy_km = forward_dist * math.sin(self._base_heading) + lateral_offset * math.cos(self._base_heading)

        # 更新位置
        new_col = self.position.col + dx_km / self.cell_size_km
        new_row = self.position.row + dy_km / self.cell_size_km

        # clamp 到 [0, 29] 网格范围
        new_col = max(0, min(29, new_col))
        new_row = max(0, min(29, new_row))

        # 如果碰到边界，改变方向
        if new_col <= 0 or new_col >= 29 or new_row <= 0 or new_row >= 29:
            self._base_heading = random.uniform(0, 2 * math.pi)

        self.position = GridCoord(int(new_col), int(new_row))

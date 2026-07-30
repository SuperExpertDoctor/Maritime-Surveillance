"""UAV 实体：包含位置、油量、状态更新逻辑。"""
from schedule.datatypes import GridCoord, BBox


class UAVEntity:
    def __init__(self, uav_id: str, base_position: GridCoord,
                 endurance_h: float, cruise_speed_kmh: float,
                 cell_size_km: float = 10.0):
        self.id = uav_id
        self._col: float = float(base_position.col)
        self._row: float = float(base_position.row)
        self._base_col: float = float(base_position.col)
        self._base_row: float = float(base_position.row)
        self.endurance_h = endurance_h
        self.cruise_speed_kmh = cruise_speed_kmh
        self.cell_size_km = cell_size_km
        self.fuel_remaining_pct: float = 1.0
        self.status: str = "idle"  # idle|transit|searching|tracking|returning|refueling
        self.assigned_region: BBox | None = None
        self.waypoints: list[GridCoord] = []
        self._wp_index: int = 0
        self._fuel_consumption_rate: float = 1.0 / (endurance_h * 60.0)  # % per minute

    @property
    def position(self) -> GridCoord:
        return GridCoord(int(self._col), int(self._row))

    @position.setter
    def position(self, value: GridCoord) -> None:
        self._col = float(value.col)
        self._row = float(value.row)

    @property
    def base_position(self) -> GridCoord:
        return GridCoord(int(self._base_col), int(self._base_row))

    @base_position.setter
    def base_position(self, value: GridCoord) -> None:
        self._base_col = float(value.col)
        self._base_row = float(value.row)

    def assign_mission(self, region_bbox: BBox, waypoints: list[GridCoord]) -> None:
        self.assigned_region = region_bbox
        self.waypoints = waypoints
        self._wp_index = 0
        self.status = "transit"

    def step(self, dt_min: float) -> bool:
        """推进 dt 分钟。返回 True 表示油量耗尽需返航。"""
        # 燃油消耗
        if self.status not in ("idle", "refueling"):
            self.fuel_remaining_pct -= self._fuel_consumption_rate * dt_min
            self.fuel_remaining_pct = max(0.0, self.fuel_remaining_pct)

        # 按航路点移动（使用浮点内部坐标避免截断误差）
        if self.waypoints and self._wp_index < len(self.waypoints):
            target = self.waypoints[self._wp_index]
            dist_cells = ((target.col - self._col) ** 2 +
                         (target.row - self._row) ** 2) ** 0.5
            dist_km = dist_cells * self.cell_size_km
            speed_km_per_min = self.cruise_speed_kmh / 60.0
            travel_dist = speed_km_per_min * dt_min

            if travel_dist >= dist_km:
                self._col = float(target.col)
                self._row = float(target.row)
                self._wp_index += 1
                # 到达第一个航路点（区域入口）后切换为 search 模式
                if self._wp_index == 1 and self.status == "transit":
                    self.status = "searching"
                if self._wp_index >= len(self.waypoints):
                    if self.status == "transit":
                        self.status = "searching"
                    elif self.status == "returning":
                        self.status = "refueling"
            else:
                ratio = travel_dist / max(dist_km, 0.001)
                self._col += (target.col - self._col) * ratio
                self._row += (target.row - self._row) * ratio

        # 油量检查
        if self.fuel_remaining_pct <= 0.05 and self.status not in ("returning", "idle", "refueling"):
            self.status = "returning"
            self.waypoints = [self.position, self.base_position]
            self._wp_index = 0
            return True

        return False

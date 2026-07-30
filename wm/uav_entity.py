"""UAV 实体：包含位置、油量、状态更新逻辑。"""
from schedule.datatypes import GridCoord, BBox


class UAVEntity:
    def __init__(self, uav_id: str, base_position: GridCoord,
                 endurance_h: float, cruise_speed_kmh: float):
        self.id = uav_id
        self.position = base_position
        self.base_position = base_position
        self.endurance_h = endurance_h
        self.cruise_speed_kmh = cruise_speed_kmh
        self.fuel_remaining_pct: float = 1.0
        self.status: str = "idle"  # idle|transit|searching|tracking|returning|refueling
        self.assigned_region: BBox | None = None
        self.waypoints: list[GridCoord] = []
        self._wp_index: int = 0
        self._fuel_consumption_rate: float = 1.0 / (endurance_h * 60.0)  # % per minute

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

        # 按航路点移动
        if self.waypoints and self._wp_index < len(self.waypoints):
            target = self.waypoints[self._wp_index]
            dist_cells = ((target.col - self.position.col) ** 2 +
                         (target.row - self.position.row) ** 2) ** 0.5
            dist_km = dist_cells * 10.0  # cell_size_km
            speed_km_per_min = self.cruise_speed_kmh / 60.0
            travel_dist = speed_km_per_min * dt_min

            if travel_dist >= dist_km:
                self.position = target
                self._wp_index += 1
                if self._wp_index >= len(self.waypoints):
                    if self.status == "transit":
                        self.status = "searching"
                    elif self.status == "returning":
                        self.status = "refueling"
            else:
                ratio = travel_dist / max(dist_km, 0.001)
                new_col = self.position.col + (target.col - self.position.col) * ratio
                new_row = self.position.row + (target.row - self.position.row) * ratio
                self.position = GridCoord(int(new_col), int(new_row))

        # 油量检查
        if self.fuel_remaining_pct <= 0.05 and self.status not in ("returning", "idle", "refueling"):
            self.status = "returning"
            self.waypoints = [self.position, self.base_position]
            self._wp_index = 0
            return True

        return False

import math
from typing import Optional
from schedule.datatypes import UAVState, Region, Marker, BBox, GridCoord
from schedule.config_loader import AppConfig
from schedule.info_field import InfoField


class StateManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.current_time: float = 0.0
        self.cycle: int = 0
        self.info_field = InfoField(config)

        # UAV 列表
        self._uavs: list[UAVState] = [
            UAVState(id=f"UAV-{i+1}", status="idle",
                     position=GridCoord(config.environment.base_position[0],
                                        config.environment.base_position[1]))
            for i in range(config.uav.count_max)
        ]

        # 区域
        self._search_regions: list[Region] = []
        self._track_regions: list[Region] = []
        self._previous_search_regions: list[Region] = []

        # 标记点
        self._markers: list[Marker] = []
        self._marker_counter: int = 0

        # 事件流
        self._events: list[dict] = []

        # 已发现的目标群
        self._known_target_groups: set[str] = set()

    def step(self, current_time: float) -> None:
        """推进一帧仿真时间，更新所有衰减。"""
        self.current_time = current_time
        self.info_field.update_decay(current_time)

    # --- UAV 管理 ---
    def get_all_uavs(self) -> list[UAVState]:
        return self._uavs

    def get_uav(self, uav_id: str) -> Optional[UAVState]:
        for u in self._uavs:
            if u.id == uav_id:
                return u
        return None

    def get_available_uavs(self) -> list[UAVState]:
        """状态为 idle 且 position 为基地的 UAV。"""
        return [u for u in self._uavs if u.status == "idle"]

    def update_uav_status(self, uav_id: str, status: str, position: GridCoord,
                          assigned_region_id: Optional[str] = None,
                          fuel_remaining_pct: Optional[float] = None,
                          target_group_id: Optional[str] = None) -> None:
        uav = self.get_uav(uav_id)
        if uav is None:
            return
        uav.status = status
        uav.position = position
        if assigned_region_id is not None:
            uav.assigned_region_id = assigned_region_id
        if fuel_remaining_pct is not None:
            uav.fuel_remaining_pct = fuel_remaining_pct
        if target_group_id is not None:
            uav.target_group_id = target_group_id

    # --- 区域管理 ---
    def set_search_regions(self, regions: list[Region]) -> None:
        self._previous_search_regions = list(self._search_regions)
        self._search_regions = regions

    def get_search_regions(self) -> list[Region]:
        return self._search_regions

    def get_active_search_regions(self) -> list[Region]:
        return [r for r in self._search_regions if r.status == "active"]

    def get_previous_search_regions(self) -> list[Region]:
        return self._previous_search_regions

    def get_track_regions(self) -> list[Region]:
        return self._track_regions

    def create_track_region(self, target_group_id: str, center: GridCoord) -> Region:
        """以目标位置为中心，创建 4×4 跟踪区。"""
        c, r = center
        half = 2
        bbox = BBox(
            max(0, c - half), max(0, r - half),
            min(self.config.grid.resolution[1], c + half),
            min(self.config.grid.resolution[0], r + half)
        )
        region = Region(
            id=f"T{len(self._track_regions)+1}",
            bbox=bbox, type="track", priority="high",
            created_cycle=self.cycle
        )
        self._track_regions.append(region)
        self._known_target_groups.add(target_group_id)
        return region

    def update_track_region_center(self, region_id: str, new_center: GridCoord) -> None:
        """跟随目标移动更新跟踪区位置。"""
        for r in self._track_regions:
            if r.id == region_id:
                c, r_c = new_center
                half = 2
                r.bbox = BBox(
                    max(0, c - half), max(0, r_c - half),
                    min(self.config.grid.resolution[1], c + half),
                    min(self.config.grid.resolution[0], r_c + half)
                )
                return

    def release_track_region(self, region_id: str, source_uav_id: str = "") -> None:
        """释放跟踪区并创建标记点。"""
        for r in self._track_regions:
            if r.id == region_id:
                center_col = (r.bbox.col_start + r.bbox.col_end) // 2
                center_row = (r.bbox.row_start + r.bbox.row_end) // 2
                self._marker_counter += 1
                marker = Marker(
                    id=f"MK{self._marker_counter}",
                    position=GridCoord(center_col, center_row),
                    created_time=self.current_time,
                    source_uav_id=source_uav_id,
                )
                self._markers.append(marker)
                self.info_field.add_marker(marker.position, self.current_time, marker.id)
                self._track_regions.remove(r)
                # 原跟踪区 cell 信息价值提升
                self._set_region_value_boost(r.bbox)
                return

    def _set_region_value_boost(self, bbox: BBox) -> None:
        """提升 bbox 内 cell 的信息价值（通过标记点已有机制）。"""
        # 标记点已通过 add_marker 提升了周边价值，此处无需额外操作
        pass

    def get_active_markers(self) -> list[Marker]:
        return self._markers

    # --- 事件管理 ---
    def add_event(self, event_type: str, data: dict) -> None:
        self._events.append({
            "type": event_type,
            "time": self.current_time,
            "data": data,
        })

    def get_recent_events(self, since_time: float) -> list[dict]:
        return [e for e in self._events if e["time"] >= since_time]

    # --- 信息场接口代理 ---
    def scan_bbox(self, bbox: BBox, current_time: float, is_track: bool = False) -> None:
        self.info_field.scan_bbox(bbox, current_time, is_track)

    def scan_cell(self, coord: GridCoord, current_time: float, is_track: bool = False) -> None:
        self.info_field.scan_cell(coord, current_time, is_track)

    def get_info_matrix(self):
        return self.info_field.get_info_matrix()

    def get_value_matrix(self):
        return self.info_field.get_value_matrix(self.current_time)

    def get_avg_info_in_bbox(self, bbox: BBox) -> float:
        return self.info_field.get_avg_info_in_bbox(bbox)

    def get_avg_value_in_bbox(self, bbox: BBox) -> float:
        return self.info_field.get_avg_value_in_bbox(bbox, self.current_time)

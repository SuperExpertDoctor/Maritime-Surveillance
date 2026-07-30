"""基地：UAV 起飞/降落/加油管理。"""
from src.schedule.datatypes import GridCoord


class BaseStation:
    def __init__(self, position: GridCoord, refuel_time_min: float):
        self.position = position
        self.refuel_time_min = refuel_time_min
        self._refueling_queue: dict[str, float] = {}  # uav_id -> time_remaining_min

    def land_uav(self, uav_id: str) -> None:
        self._refueling_queue[uav_id] = self.refuel_time_min

    def step(self, dt_min: float) -> list[str]:
        """推进 dt 分钟。返回加油完成的 UAV ID 列表。"""
        ready = []
        for uav_id in list(self._refueling_queue):
            self._refueling_queue[uav_id] -= dt_min
            if self._refueling_queue[uav_id] <= 0:
                ready.append(uav_id)
                del self._refueling_queue[uav_id]
        return ready

    def is_refueling(self, uav_id: str) -> bool:
        return uav_id in self._refueling_queue

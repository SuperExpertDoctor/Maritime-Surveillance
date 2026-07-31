"""基地：UAV 起飞/降落/加油管理。"""
from src.schedule.datatypes import GridCoord


class BaseStation:
    """One numbered coastal base with a hard concurrent-service limit."""

    def __init__(
        self,
        position: GridCoord,
        refuel_time_min: float,
        capacity: int = 3,
        base_id: str = "Base-1",
    ):
        if capacity <= 0:
            raise ValueError("base capacity must be positive")
        self.id = base_id
        self.position = position
        self.refuel_time_min = float(refuel_time_min)
        self.capacity = int(capacity)
        self._refueling_queue: dict[str, float] = {}
        self._hangar: list[str] = []
        self.refuel_count = 0

    @property
    def occupancy(self) -> int:
        return len(self._refueling_queue)

    @property
    def is_busy(self) -> bool:
        return self.occupancy >= self.capacity

    @property
    def hangar(self) -> tuple[str, ...]:
        return tuple(self._hangar)

    def can_accept(self) -> bool:
        return self.occupancy < self.capacity

    def land_uav(self, uav_id: str) -> bool:
        """Start refuelling iff a service position is free."""
        if uav_id in self._refueling_queue:
            return True
        if not self.can_accept():
            return False
        self._refueling_queue[uav_id] = self.refuel_time_min
        if uav_id not in self._hangar:
            self._hangar.append(uav_id)
        return True

    def step(self, dt_min: float) -> list[str]:
        """推进 dt 分钟。返回加油完成的 UAV ID 列表。"""
        ready = []
        for uav_id in list(self._refueling_queue):
            self._refueling_queue[uav_id] -= dt_min
            if self._refueling_queue[uav_id] <= 0:
                ready.append(uav_id)
                del self._refueling_queue[uav_id]
                if uav_id in self._hangar:
                    self._hangar.remove(uav_id)
                self.refuel_count += 1
        return ready

    def is_refueling(self, uav_id: str) -> bool:
        return uav_id in self._refueling_queue

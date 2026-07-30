"""仿真时钟。"""


class SimClock:
    def __init__(self, start_time: float = 0.0):
        self.time: float = start_time
        self.dt_min: float = 1.0  # 默认步长 1 分钟

    def tick(self) -> float:
        self.time += self.dt_min
        return self.time

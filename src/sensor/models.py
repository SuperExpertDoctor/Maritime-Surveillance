"""传感器模型：SAR、EO/IR、雷帧 —— 探测概率 + 距离限制 + 传感器融合。"""
import math
import random
from dataclasses import dataclass
from src.schedule.datatypes import GridCoord


# ---------------------------------------------------------------------------
# 传感器配置 dataclass
# ---------------------------------------------------------------------------

@dataclass
class SarConfig:
    """SAR 合成孔径雷达配置。"""
    swath_km: float
    detection_range_km: float
    detection_probability: float
    false_alarm_rate: float
    resolution_m: float
    mode: str


@dataclass
class EoIrConfig:
    """EO/IR 光电/红外配置。"""
    fov_deg: float
    detection_range_km: float
    detection_probability: float
    false_alarm_rate: float
    resolution_m: float


@dataclass
class RadarConfig:
    """Radar/ESM 雷帧配置。"""
    detection_range_km: float
    azimuth_coverage_deg: float
    detection_probability: float
    false_alarm_rate: float
    update_rate_hz: float


@dataclass
class GeneralSensorConfig:
    """通用传感器参数。"""
    search_efficiency: float


@dataclass
class SensorConfig:
    """传感器总配置。"""
    sar: SarConfig
    eoir: EoIrConfig
    radar: RadarConfig
    general: GeneralSensorConfig


# ---------------------------------------------------------------------------
# 传感器模型
# ---------------------------------------------------------------------------

class SensorBase:
    """传感器基类。

    Attributes:
        name: 传感器名称
        detection_range_km: 最大探测距离 (km)
        detection_probability: 单次检测概率 Pd
        false_alarm_rate: 虚警率
    """

    def __init__(self, name: str, detection_range_km: float,
                 detection_probability: float, false_alarm_rate: float):
        self.name = name
        self.detection_range_km = detection_range_km
        self.detection_probability = detection_probability
        self.false_alarm_rate = false_alarm_rate

    def get_effective_range_cells(self, cell_size_km: float) -> float:
        """返回有效探测距离（网格单位）。"""
        return self.detection_range_km / cell_size_km

    def can_detect(self, uav_pos: GridCoord, target_pos: GridCoord,
                   cell_size_km: float) -> bool:
        """判断传感器是否能探测到目标。

        两步判定：
        1. 距离检查：目标必须在有效探测距离内
        2. 概率判定：在距离内时，以 Pd 概率成功检测

        Args:
            uav_pos: UAV 位置
            target_pos: 目标位置
            cell_size_km: 每格实际距离 (km)

        Returns:
            True 表示本次扫描成功发现目标
        """
        # 距离判断
        dist_cells = math.sqrt(
            (uav_pos.col - target_pos.col) ** 2 +
            (uav_pos.row - target_pos.row) ** 2
        )
        effective_range = self.get_effective_range_cells(cell_size_km)
        if dist_cells > effective_range:
            return False

        # 概率判定
        return random.random() < self.detection_probability


class SarSensor(SensorBase):
    """SAR 合成孔径雷达。

    特点：大范围条带扫描，分辨率适中，不受光照影响。
    """

    def __init__(self, config: SarConfig):
        super().__init__(
            name="SAR",
            detection_range_km=config.detection_range_km,
            detection_probability=config.detection_probability,
            false_alarm_rate=config.false_alarm_rate,
        )
        self.swath_km = config.swath_km
        self.resolution_m = config.resolution_m
        self.mode = config.mode

    def get_swath_cells(self, cell_size_km: float) -> int:
        """返回条带宽度对应的网格单元数。"""
        return max(1, int(self.swath_km / cell_size_km))


class EoIrSensor(SensorBase):
    """EO/IR 光电/红外传感器。

    特点：高分辨率，短距离，受天气和光照影响。
    """

    def __init__(self, config: EoIrConfig):
        super().__init__(
            name="EO/IR",
            detection_range_km=config.detection_range_km,
            detection_probability=config.detection_probability,
            false_alarm_rate=config.false_alarm_rate,
        )
        self.fov_deg = config.fov_deg
        self.resolution_m = config.resolution_m


class RadarSensor(SensorBase):
    """Radar/ESM 雷帧传感器。

    特点：最远探测距离，360° 方位覆盖，高检测概率。
    """

    def __init__(self, config: RadarConfig):
        super().__init__(
            name="Radar/ESM",
            detection_range_km=config.detection_range_km,
            detection_probability=config.detection_probability,
            false_alarm_rate=config.false_alarm_rate,
        )
        self.azimuth_coverage_deg = config.azimuth_coverage_deg
        self.update_rate_hz = config.update_rate_hz


# ---------------------------------------------------------------------------
# 传感器套件（融合）
# ---------------------------------------------------------------------------

class SensorSuite:
    """UAV 传感器套件：组合 SAR + EO/IR + Radar/ESM 三种传感器。

    融合策略：任一传感器探测到目标即判定为发现。
    """

    def __init__(self, sar: SarSensor, eoir: EoIrSensor, radar: RadarSensor):
        self.sar = sar
        self.eoir = eoir
        self.radar = radar
        self._sensors = [sar, eoir, radar]

    @classmethod
    def from_config(cls, config: SensorConfig) -> "SensorSuite":
        """从 SensorConfig 创建传感器套件。"""
        return cls(
            sar=SarSensor(config.sar),
            eoir=EoIrSensor(config.eoir),
            radar=RadarSensor(config.radar),
        )

    def detect(self, uav_pos: GridCoord,
               targets: list, cell_size_km: float) -> list:
        """使用传感器套件探测目标列表。

        每个目标依次由三个传感器独立判定，
        任一传感器命中即视为被发现。

        Args:
            uav_pos: UAV 当前位置
            targets: 目标对象列表（须有 position 属性和 detected 属性）
            cell_size_km: 每格实际距离 (km)

        Returns:
            本次新发现的目标列表
        """
        detected = []
        for target in targets:
            if target.detected:
                continue
            for sensor in self._sensors:
                if sensor.can_detect(uav_pos, target.position, cell_size_km):
                    detected.append(target)
                    break  # 任一传感器命中即可，不重复计数
        return detected

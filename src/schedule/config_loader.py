import os
import yaml
from dataclasses import dataclass

from src.sensor.models import (
    SarConfig, EoIrConfig, RadarConfig, GeneralSensorConfig, SensorConfig,
)


@dataclass
class EnvironmentConfig:
    sea_area_km: tuple
    base_position: tuple


@dataclass
class GridConfig:
    resolution: tuple
    cell_size_km: int
    decay_half_life_min: float
    track_decay_half_life_min: float
    white_threshold: float
    gray_threshold: float
    value_alpha: float
    value_beta: float
    value_gamma: float
    marker_sigma_cells: float
    marker_max_age_min: float
    marker_decay_half_life_min: float
    candidate_value_threshold: float
    fragment_threshold_cells: int
    track_min_cells: int
    track_max_cells: int
    search_min_cells: int
    search_max_cells: int
    aspect_ratio_max: float
    stability_iou_threshold: float


@dataclass
class UAVConfig:
    count_max: int
    cruise_speed_kmh: float
    endurance_h: float
    refuel_time_min: float
    sortie_endurance_h: float = 1.8


@dataclass
class ShipConfig:
    count_min: int
    max_groups: int
    speed_kn: float
    zigzag_amplitude_km: float
    zigzag_period_min: float
    zigzag_phase_random: bool


@dataclass
class LLMConfig:
    heavy_cycle_min: float
    reviewer_cycle_min: float
    max_retries: int


@dataclass
class AppConfig:
    environment: EnvironmentConfig
    grid: GridConfig
    uav: UAVConfig
    ship: ShipConfig
    llm: LLMConfig
    sensor: SensorConfig


class ConfigLoader:
    @staticmethod
    def _dict_to_dataclass(d: dict, cls):
        field_names = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in field_names}
        return cls(**filtered)

    @staticmethod
    def load(base_path: str = "configs") -> "AppConfig":
        def _read(name):
            with open(os.path.join(base_path, name), "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

        env_data = _read("environment.yaml")
        env_data["sea_area_km"] = tuple(env_data["sea_area_km"])
        env_data["base_position"] = tuple(env_data["base_position"])

        grid_data = _read("grid.yaml")
        grid_data["resolution"] = tuple(grid_data["resolution"])

        return AppConfig(
            environment=ConfigLoader._dict_to_dataclass(env_data, EnvironmentConfig),
            grid=ConfigLoader._dict_to_dataclass(grid_data, GridConfig),
            uav=ConfigLoader._dict_to_dataclass(_read("uav.yaml"), UAVConfig),
            ship=ConfigLoader._dict_to_dataclass(_read("ship.yaml"), ShipConfig),
            llm=ConfigLoader._dict_to_dataclass(_read("llm.yaml"), LLMConfig),
            sensor=ConfigLoader._load_sensor_config(base_path),
        )

    @staticmethod
    def _load_sensor_config(base_path: str) -> SensorConfig:
        def _read(name):
            with open(os.path.join(base_path, name), "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

        data = _read("sensor.yaml")
        return SensorConfig(
            sar=ConfigLoader._dict_to_dataclass(data["sar"], SarConfig),
            eoir=ConfigLoader._dict_to_dataclass(data["eoir"], EoIrConfig),
            radar=ConfigLoader._dict_to_dataclass(data["radar"], RadarConfig),
            general=ConfigLoader._dict_to_dataclass(data["general"], GeneralSensorConfig),
        )

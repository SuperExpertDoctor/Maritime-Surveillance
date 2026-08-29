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
    base_count: int = 1
    base_capacity: int = 3
    base_min_distance_cells: float = 5.0
    base_land_margin: int = 0
    mainland_width_cells: int = 5
    base_task_min_distance_cells: float = 3.0
    base_obstacle_clearance_cells: float = 4.0
    island_count_min: int = 0
    island_count_max: int = 2
    thunderstorm_count_min: int = 2
    thunderstorm_count_max: int = 3
    storm_safety_margin_cells: float = 1.0


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
    lifecycle_rotation_start_min: float = 120.0
    lifecycle_coverage_threshold_pct: float = 50.0
    lifecycle_search_dwell_min: float = 5.0
    lifecycle_candidate_max_distance_cells: float = 12.0
    lifecycle_required_cycles: int = 3
    freshness_patrol_start_min: float = 120.0
    freshness_patrol_count: int = 5
    freshness_patrol_coverage_threshold_pct: float = 80.0


@dataclass
class ShipConfig:
    count_min: int
    max_groups: int
    speed_kn: float
    zigzag_amplitude_km: float
    zigzag_period_min: float
    zigzag_phase_random: bool
    target_min: int = 3
    target_max: int = 5
    group_max: int = 3
    carrier_max: int = 1
    carrier_speed_kn: float = 14.0
    destroyer_speed_kn: float = 20.0
    zigzag_heading_deg: float = 18.0
    max_turn_rate_deg_min: float = 12.0
    yaw_time_constant_min: float = 2.5
    heading_control_gain_per_min: float = 0.35
    turn_speed_loss_fraction: float = 0.12
    ais_discrepancy_threshold_cells: float = 2.0
    ais_update_interval_min: float = 1.0
    ais_discrimination_delay_min: float = 2.0


@dataclass
class LLMConfig:
    heavy_cycle_min: float
    reviewer_cycle_min: float
    max_retries: int


@dataclass
class CommonConfig:
    clear_outputs_before_run: bool = True


@dataclass(frozen=True)
class ObservationControlConfig:
    schema_version: str = "control-observation/v1"
    local_window_cells: int = 11


@dataclass(frozen=True)
class SafetyControlConfig:
    min_speed_fraction: float = 0.6
    max_speed_fraction: float = 1.2
    reserve_range_cells: float = 4.0
    max_invalid_commands: int = 3


@dataclass(frozen=True)
class HeuristicControlConfig:
    astar_dynamic_replan_limit: int = 3
    astar_xy_resolution_cells: float = 0.5
    astar_heading_bins: int = 72
    astar_candidate_limit: int = 32
    astar_primitive_length_cells: float = 1.0
    path_sample_step_cells: float = 0.2


@dataclass(frozen=True)
class ControlConfig:
    default_mode: str
    per_uav: dict[str, str]
    observation: ObservationControlConfig
    safety: SafetyControlConfig
    heuristic: HeuristicControlConfig


@dataclass
class AppConfig:
    environment: EnvironmentConfig
    grid: GridConfig
    uav: UAVConfig
    ship: ShipConfig
    llm: LLMConfig
    sensor: SensorConfig
    common: CommonConfig
    control: ControlConfig


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
        grid_data = env_data.pop("grid")
        env_data["sea_area_km"] = tuple(env_data["sea_area_km"])
        env_data["base_position"] = tuple(env_data["base_position"])
        grid_data["resolution"] = tuple(grid_data["resolution"])
        llm_params_data = _read("llm_params.yaml")
        control_data = _read("control.yaml")
        configured_modes = {
            control_data["default_mode"],
            *control_data.get("per_uav", {}).values(),
        }
        unknown_modes = configured_modes - {"heuristic", "bc", "rl"}
        if unknown_modes:
            raise ValueError(f"unsupported control modes: {sorted(unknown_modes)}")
        window = int(control_data["observation"]["local_window_cells"])
        if window <= 0 or window % 2 == 0:
            raise ValueError(
                "control observation local_window_cells must be a positive odd integer"
            )
        control = ControlConfig(
            default_mode=control_data["default_mode"],
            per_uav=dict(control_data.get("per_uav", {})),
            observation=ConfigLoader._dict_to_dataclass(
                control_data["observation"], ObservationControlConfig
            ),
            safety=ConfigLoader._dict_to_dataclass(
                control_data["safety"], SafetyControlConfig
            ),
            heuristic=ConfigLoader._dict_to_dataclass(
                control_data["heuristic"], HeuristicControlConfig
            ),
        )

        return AppConfig(
            environment=ConfigLoader._dict_to_dataclass(env_data, EnvironmentConfig),
            grid=ConfigLoader._dict_to_dataclass(grid_data, GridConfig),
            uav=ConfigLoader._dict_to_dataclass(_read("uav.yaml"), UAVConfig),
            ship=ConfigLoader._dict_to_dataclass(_read("ship.yaml"), ShipConfig),
            llm=ConfigLoader._dict_to_dataclass(llm_params_data["cycles"], LLMConfig),
            sensor=ConfigLoader._load_sensor_config(base_path),
            common=ConfigLoader._dict_to_dataclass(_read("common.yaml") or {}, CommonConfig),
            control=control,
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

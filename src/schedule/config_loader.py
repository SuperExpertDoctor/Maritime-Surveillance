import os
from dataclasses import dataclass, replace
import math

from src.mission.config import (
    ActivityConfig,
    ContactConfig,
    CoverageConfig,
    EmitterConfig,
    EvasionConfig,
    EvolutionConfig,
    InformationUpdateConfig,
    IntentConfig,
    MissionConfig,
    PassiveConfig,
    PopulationConfig,
    SchedulingConfig,
    load_strict_yaml,
    strict_dataclass,
    validate_mission_config,
)

from src.sensor.models import (
    SarConfig, EoIrConfig, RadarConfig, GeneralSensorConfig, SensorConfig,
)


def allocate_population(total: int, ratios: dict[str, float]) -> dict[str, int]:
    """Allocate a population with largest-remainder rounding and stable ties."""
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("population total must be a non-negative integer")
    order = ("type_i", "type_ii")
    if set(ratios) != set(order):
        raise ValueError("population ratios must contain type_i and type_ii")
    values = tuple(ratios[key] for key in order)
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0.0 for value in values):
        raise ValueError("population ratios must be finite non-negative numbers")
    total_ratio = math.fsum(values)
    if total_ratio <= 0.0 or abs(total_ratio - 1.0) > 1e-9:
        raise ValueError("population ratios must sum to 1")
    quotas = {key: total * value / total_ratio for key, value in zip(order, values)}
    result = {key: math.floor(quotas[key]) for key in order}
    remaining = total - sum(result.values())
    if not 0 <= remaining < len(order):
        raise ValueError("population allocation is numerically inconsistent")
    ranked = sorted(
        order,
        key=lambda key: (-(quotas[key] - result[key]), order.index(key)),
    )
    for key in ranked[:remaining]:
        result[key] += 1
    return result


def validate_uav_count(value: object, *, formal_acceptance: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("uav.count: expected integer")
    minimum = 10 if formal_acceptance else 1
    if value < minimum:
        raise ValueError(f"uav.count: expected integer >= {minimum}")
    return value


def _seed_sequence(value: object, name: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name}: expected sequence")
    return tuple(value)


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

    @property
    def count(self) -> int:
        """New semantic name; the legacy field remains for replay compatibility."""
        return self.count_max


@dataclass(frozen=True)
class ShipConfig:
    population: PopulationConfig
    type_ii_ais_on_probability: float
    speed_kn: float
    ais_update_interval_min: float
    ais_position_noise_cells: float
    max_turn_rate_deg_min: float
    yaw_time_constant_min: float
    heading_control_gain_per_min: float
    turn_speed_loss_fraction: float
    max_acceleration_kn_per_min: float
    detect_uav_radius_cells: float
    clear_uav_radius_cells: float
    clear_hold_min: float
    red_decision_cycle_min: float
    red_plan_valid_min: float
    speed_min_kn: float
    speed_max_kn: float
    heading_offset_max_deg: float
    zigzag_heading_max_deg: float
    zigzag_period_min_min: float
    zigzag_period_max_min: float
    min_evasion_heading_deg: float
    min_evasion_speed_delta_kn: float
    navigation_horizon_min: float
    integration_dt_min: float
    navigation_clearance_cells: float

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
    schema_version: str = "control-observation/v2"
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
    mission: MissionConfig


class ConfigLoader:
    @staticmethod
    def _dict_to_dataclass(d: dict, cls):
        return strict_dataclass(d, cls, cls.__name__)

    @staticmethod
    def load(
        base_path: str = "configs",
        *,
        ship_path: str | os.PathLike[str] | None = None,
    ) -> "AppConfig":
        def _read(name):
            return load_strict_yaml(os.path.join(base_path, name))

        env_data = _read("environment.yaml")
        grid_data = env_data.pop("grid")
        env_data["sea_area_km"] = tuple(env_data["sea_area_km"])
        env_data["base_position"] = tuple(env_data["base_position"])
        grid_data["resolution"] = tuple(grid_data["resolution"])
        llm_params_data = _read("llm_params.yaml")
        mission_data = _read("mission.yaml")
        mission_fields = {
            "contact", "intent", "scheduling", "evolution", "activity",
            "evasion", "information_update", "coverage",
        }
        unknown_mission_fields = set(mission_data) - mission_fields
        if unknown_mission_fields:
            raise ValueError(
                f"mission: unknown fields: {sorted(unknown_mission_fields)}"
            )
        evolution_data = mission_data.get("evolution")
        if not isinstance(evolution_data, dict):
            raise ValueError("mission.evolution: expected mapping")
        evolution_data = dict(evolution_data)
        for seed_field in ("validation_seeds", "holdout_seeds"):
            if seed_field in evolution_data:
                evolution_data[seed_field] = _seed_sequence(
                    evolution_data[seed_field],
                    f"mission.evolution.{seed_field}",
                )
        alignment_data = {
            "activity": mission_data.get("activity", {}),
            "evasion": mission_data.get("evasion", {}),
            "information_update": mission_data.get("information_update", {}),
        }
        alignment_configs = {
            "activity": ConfigLoader._alignment_dataclass(
                alignment_data["activity"], ActivityConfig, "mission.activity",
            ),
            "evasion": ConfigLoader._alignment_dataclass(
                alignment_data["evasion"], EvasionConfig, "mission.evasion",
            ),
            "information_update": ConfigLoader._alignment_dataclass(
                alignment_data["information_update"],
                InformationUpdateConfig,
                "mission.information_update",
            ),
        }
        mission = MissionConfig(
            contact=replace(
                strict_dataclass(mission_data.get("contact"), ContactConfig, "mission.contact"),
                cell_size_km=grid_data["cell_size_km"],
            ),
            intent=strict_dataclass(
                mission_data.get("intent"), IntentConfig, "mission.intent"
            ),
            scheduling=strict_dataclass(
                mission_data.get("scheduling"),
                SchedulingConfig,
                "mission.scheduling",
            ),
            evolution=strict_dataclass(
                evolution_data,
                EvolutionConfig,
                "mission.evolution",
            ),
            coverage=ConfigLoader._coverage_dataclass(
                mission_data.get("coverage", {}), "mission.coverage"
            ),
        )
        for name, value in alignment_configs.items():
            object.__setattr__(mission, name, value)
        control_data = _read("control.yaml")
        configured_modes = {
            control_data["default_mode"],
            *control_data.get("per_uav", {}).values(),
        }
        unknown_modes = configured_modes - {"heuristic", "bc", "rl"}
        if unknown_modes:
            raise ValueError(f"unsupported control modes: {sorted(unknown_modes)}")
        raw_window = control_data["observation"]["local_window_cells"]
        if (
            isinstance(raw_window, bool)
            or not isinstance(raw_window, int)
            or raw_window <= 0
            or raw_window % 2 == 0
        ):
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

        ship_data = load_strict_yaml(ship_path) if ship_path is not None else _read("ship.yaml")
        population_data = ship_data.pop("population", None)
        if population_data is None:
            raise ValueError(
                "ship.population is required; use total_count, type_i_ratio, "
                "and type_ii_ratio"
            )
        missing_population_fields = {
            "type_i_ratio", "type_ii_ratio",
        } - set(population_data)
        if missing_population_fields:
            raise ValueError(
                "ship.population requires type_i_ratio and type_ii_ratio"
            )
        population = ConfigLoader._alignment_dataclass(
            population_data, PopulationConfig, "ship.population",
        )
        legacy_ship_fields = {
            "count_min",
            "max_groups",
            "zigzag_amplitude_km",
            "zigzag_period_min",
            "zigzag_phase_random",
            "target_min",
            "target_max",
            "group_max",
            "carrier_max",
            "carrier_speed_kn",
            "destroyer_speed_kn",
            "zigzag_heading_deg",
            "ais_discrepancy_threshold_cells",
            "ais_discrimination_delay_min",
        } & set(ship_data)
        if legacy_ship_fields:
            raise ValueError(
                "legacy ship configuration fields require migration: "
                f"{sorted(legacy_ship_fields)}; use population and "
                "type_i/type_ii configuration"
            )

        uav_data = _read("uav.yaml")
        if "count" in uav_data:
            if "count_max" in uav_data:
                raise ValueError("uav: use count instead of count_max")
            uav_data["count_max"] = uav_data.pop("count")
        elif "count_max" in uav_data:
            raise ValueError("uav: use count instead of count_max")

        config = AppConfig(
            environment=ConfigLoader._dict_to_dataclass(env_data, EnvironmentConfig),
            grid=ConfigLoader._dict_to_dataclass(grid_data, GridConfig),
            uav=ConfigLoader._dict_to_dataclass(uav_data, UAVConfig),
            ship=strict_dataclass(
                {"population": population, **ship_data}, ShipConfig, "ship"
            ),
            llm=ConfigLoader._dict_to_dataclass(llm_params_data["cycles"], LLMConfig),
            sensor=ConfigLoader._load_sensor_config(base_path),
            common=ConfigLoader._dict_to_dataclass(_read("common.yaml") or {}, CommonConfig),
            control=control,
            mission=mission,
        )
        validate_mission_config(config)
        return config

    @staticmethod
    def load_legacy_ship_config(
        path: str | os.PathLike[str], base_path: str = "configs"
    ) -> "AppConfig":
        """Load an explicitly legacy ship file through the canonical loader."""
        from src.mission.vessel_compat import normalize_legacy_ship_config

        legacy = load_strict_yaml(path)
        normalized = normalize_legacy_ship_config(legacy)
        import tempfile
        from pathlib import Path
        import yaml

        with tempfile.TemporaryDirectory() as directory:
            normalized_path = Path(directory) / "ship.yaml"
            normalized_path.write_text(yaml.safe_dump(normalized), encoding="utf-8")
            return ConfigLoader.load(base_path, ship_path=normalized_path)

    @staticmethod
    def _alignment_dataclass(data: object, cls, name: str):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"{name}: expected mapping")
        normalized = dict(data)
        for field_name in (
            "burst_duration_min", "schedule_start_min", "schedule_duration_min",
        ):
            if field_name in normalized:
                normalized[field_name] = tuple(normalized[field_name])
        if cls is ActivityConfig and "regulated_bboxes" in normalized:
            normalized["regulated_bboxes"] = tuple(
                tuple(bbox) for bbox in normalized["regulated_bboxes"]
            )
        return strict_dataclass(normalized, cls, name)

    @staticmethod
    def _coverage_dataclass(data: object, name: str) -> CoverageConfig:
        if not isinstance(data, dict):
            raise ValueError(f"{name}: expected mapping")
        normalized = dict(data)
        if "windows_min" in normalized:
            windows = normalized["windows_min"]
            if isinstance(windows, (list, tuple)):
                normalized["windows_min"] = tuple(windows)
        return strict_dataclass(normalized, CoverageConfig, name)

    @staticmethod
    def _load_sensor_config(base_path: str) -> SensorConfig:
        def _read(name):
            return load_strict_yaml(os.path.join(base_path, name))

        data = _read("sensor.yaml")
        return SensorConfig(
            sar=ConfigLoader._dict_to_dataclass(data["sar"], SarConfig),
            eoir=ConfigLoader._dict_to_dataclass(data["eoir"], EoIrConfig),
            radar=ConfigLoader._dict_to_dataclass(data["radar"], RadarConfig),
            general=ConfigLoader._dict_to_dataclass(data["general"], GeneralSensorConfig),
            passive=ConfigLoader._alignment_dataclass(
                data.get("passive", {}), PassiveConfig, "sensor.passive",
            ),
            emitter=ConfigLoader._alignment_dataclass(
                data.get("emitter", {}), EmitterConfig, "sensor.emitter",
            ),
        )


def search_min_cells(config: AppConfig) -> int:
    """Return the configured minimum search-region area."""
    return config.grid.search_min_cells

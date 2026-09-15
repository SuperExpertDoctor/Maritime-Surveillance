"""Strict configuration contracts and validation for the mission domain."""

from dataclasses import dataclass, field, fields, is_dataclass
import math
from typing import TYPE_CHECKING, TypeVar

import yaml

if TYPE_CHECKING:
    from src.schedule.config_loader import AppConfig


@dataclass(frozen=True)
class ContactConfig:
    history_window_min: float
    history_max_samples: int
    stale_after_min: float
    association_gate_cells: float
    association_margin_cells: float
    assessment_interval_min: float
    min_valid_samples_per_phase: int
    max_sample_gap_min: float
    baseline_duration_min: float
    near_duration_min: float
    baseline_standoff_cells: float
    near_standoff_cells: float
    probe_timeout_min: float
    approach_timeout_min: float
    probe_retry_cooldown_min: float
    assessment_confidence_min: float
    civilian_recheck_cooldown_min: float
    prompt_contact_limit: int
    prompt_keypoints_per_contact: int
    # Filled from grid configuration by ConfigLoader. Standalone callers may
    # omit the scale; physical speed features must then remain null.
    cell_size_km: float | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class PopulationConfig:
    """Actual vessel population and its deterministic class proportions."""

    total_count: int
    civilian_ratio: float
    research_ratio: float

    def __post_init__(self) -> None:
        if isinstance(self.total_count, bool) or not isinstance(self.total_count, int):
            raise ValueError("population.total_count: expected integer")
        if self.total_count < 0:
            raise ValueError("population.total_count: expected integer >= 0")
        ratios = (self.civilian_ratio, self.research_ratio)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in ratios):
            raise ValueError("population ratios: expected finite numbers")
        if any(value < 0.0 or value > 1.0 for value in ratios):
            raise ValueError("population ratios: expected values in [0, 1]")
        if abs(math.fsum(ratios) - 1.0) > 1e-9:
            raise ValueError("population ratios must sum to 1")


@dataclass(frozen=True)
class PassiveConfig:
    measurement_interval_min: float = 1.0
    reference_detection_probability: float = 0.90
    detection_range_cells: float = 10.0
    range_scale_cells: float = 10.0
    bearing_std_deg: float = 3.0
    received_power_std_db: float = 2.0
    reference_distance_cells: float = 1.0
    minimum_received_power_db: float = -90.0
    position_association_radius_cells: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "measurement_interval_min", "detection_range_cells", "range_scale_cells",
            "reference_distance_cells", "position_association_radius_cells",
        ):
            _positive(getattr(self, name), f"passive.{name}")
        _probability(
            self.reference_detection_probability,
            "passive.reference_detection_probability",
        )
        if not 0.0 < self.bearing_std_deg < 90.0:
            raise ValueError("passive.bearing_std_deg must be in (0, 90)")
        _non_negative(self.received_power_std_db, "passive.received_power_std_db")
        finite_number(self.minimum_received_power_db, "passive.minimum_received_power_db")


@dataclass(frozen=True)
class EmitterConfig:
    mean_silent_interval_min: float = 10.0
    burst_duration_min: tuple[float, float] = (0.5, 2.0)
    source_power_at_reference_db: float = -40.0

    def __post_init__(self) -> None:
        _positive(self.mean_silent_interval_min, "emitter.mean_silent_interval_min")
        if len(self.burst_duration_min) != 2:
            raise ValueError("emitter.burst_duration_min must contain two values")
        low, high = (finite_number(value, "emitter.burst_duration_min")
                     for value in self.burst_duration_min)
        if low <= 0.0 or high < low:
            raise ValueError("emitter.burst_duration_min must be positive and ordered")
        finite_number(
            self.source_power_at_reference_db,
            "emitter.source_power_at_reference_db",
        )
        object.__setattr__(self, "burst_duration_min", (low, high))


@dataclass(frozen=True)
class ActivityConfig:
    regulated_bboxes: tuple[tuple[int, int, int, int], ...] = ((8, 8, 22, 22),)
    schedule_start_min: tuple[float, float] = (30.0, 120.0)
    schedule_duration_min: tuple[float, float] = (60.0, 120.0)
    survey_command_speed_kn: float = 10.0
    survey_track_spacing_cells: float = 1.0
    trajectory_window_min: float = 20.0
    min_observed_duration_min: float = 12.0
    observed_speed_max_kn: float = 12.0
    reversal_angle_min_deg: float = 120.0
    min_reversal_count: int = 2
    radiation_window_min: float = 10.0
    min_distinct_bursts: int = 2
    research_equipment_pd: float = 0.90
    research_equipment_pfa: float = 0.05
    deployed_equipment_pd: float = 0.85
    deployed_equipment_pfa: float = 0.05

    def __post_init__(self) -> None:
        for bbox in self.regulated_bboxes:
            if len(bbox) != 4 or not all(isinstance(value, int) and not isinstance(value, bool)
                                         for value in bbox):
                raise ValueError("activity.regulated_bboxes must contain integer bboxes")
            x0, y0, x1, y1 = bbox
            if not x0 < x1 or not y0 < y1:
                raise ValueError("activity.regulated_bboxes must use non-empty half-open boxes")
        for name, value in (
            ("schedule_start_min", self.schedule_start_min),
            ("schedule_duration_min", self.schedule_duration_min),
        ):
            if len(value) != 2:
                raise ValueError(f"activity.{name} must contain two values")
            low, high = (finite_number(item, f"activity.{name}") for item in value)
            if low <= 0.0 or high < low:
                raise ValueError(f"activity.{name} must be positive and ordered")
        for name in (
            "survey_command_speed_kn", "survey_track_spacing_cells",
            "trajectory_window_min", "min_observed_duration_min",
            "observed_speed_max_kn", "radiation_window_min",
        ):
            _positive(getattr(self, name), f"activity.{name}")
        if not 0.0 < self.reversal_angle_min_deg <= 180.0:
            raise ValueError("activity.reversal_angle_min_deg must be in (0, 180]")
        for name in ("min_reversal_count", "min_distinct_bursts"):
            _integer(getattr(self, name), f"activity.{name}", minimum=1)
        for name in (
            "research_equipment_pd", "research_equipment_pfa",
            "deployed_equipment_pd", "deployed_equipment_pfa",
        ):
            _probability(getattr(self, name), f"activity.{name}")


@dataclass(frozen=True)
class EvasionConfig:
    observer_range_cells: float = 6.0
    history_window_min: float = 6.0
    response_window_min: float = 3.0
    minimum_samples_per_window: int = 3
    minimum_window_span_min: float = 1.5
    minimum_course_change_deg: float = 45.0
    minimum_speed_increase_kn: float = 3.0
    minimum_outward_speed_kn: float = 3.0
    minimum_range_increase_cells: float = 0.05
    confirmation_samples: int = 2
    forced_maneuver_exclusion_cells: float = 2.0
    rearm_clear_min: float = 5.0
    evidence_ttl_min: float = 20.0
    evidence_tau_min: float = 8.0
    enabled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "observer_range_cells", "history_window_min", "response_window_min",
            "minimum_window_span_min", "minimum_speed_increase_kn",
            "minimum_outward_speed_kn", "minimum_range_increase_cells",
            "forced_maneuver_exclusion_cells", "rearm_clear_min",
            "evidence_ttl_min", "evidence_tau_min",
        ):
            _positive(getattr(self, name), f"evasion.{name}")
        if self.response_window_min >= self.history_window_min:
            raise ValueError("evasion.response_window_min must be below history_window_min")
        _integer(self.minimum_samples_per_window, "evasion.minimum_samples_per_window", minimum=2)
        _integer(self.confirmation_samples, "evasion.confirmation_samples", minimum=2)
        if self.minimum_window_span_min > self.response_window_min:
            raise ValueError("evasion.minimum_window_span_min exceeds response window")
        if not 0.0 < self.minimum_course_change_deg <= 180.0:
            raise ValueError("evasion.minimum_course_change_deg must be in (0, 180]")
        _boolean(self.enabled, "evasion.enabled")


@dataclass(frozen=True)
class InformationUpdateConfig:
    value_alpha: float = 0.45
    value_beta: float = 0.35
    value_gamma: float = 0.20
    material_delta_threshold: float = 0.05
    normal_heavy_cooldown_min: float = 1.0
    kernel_epsilon: float = 0.01
    planning_deadline_seconds: float = 2.0
    postprocess_reserve_seconds: float = 0.2

    def __post_init__(self) -> None:
        weights = (self.value_alpha, self.value_beta, self.value_gamma)
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value < 0.0 for value in weights):
            raise ValueError("information_update weights must be finite and non-negative")
        if abs(math.fsum(weights) - 1.0) > 1e-9:
            raise ValueError("information_update weights must sum to 1")
        for name in ("material_delta_threshold", "kernel_epsilon"):
            value = finite_number(getattr(self, name), f"information_update.{name}")
            if not 0.0 < value <= 1.0:
                raise ValueError(f"information_update.{name} must be in (0, 1]")
        _positive(self.normal_heavy_cooldown_min, "information_update.normal_heavy_cooldown_min")
        _positive(self.planning_deadline_seconds, "information_update.planning_deadline_seconds")
        _positive(self.postprocess_reserve_seconds, "information_update.postprocess_reserve_seconds")
        if self.postprocess_reserve_seconds >= self.planning_deadline_seconds:
            raise ValueError("information_update.postprocess_reserve_seconds must be below deadline")


@dataclass(frozen=True)
class IntentConfig:
    max_active_intents: int
    default_valid_duration_min: float
    default_revisit_interval_min: float
    default_weight: float
    candidate_limit: int
    general_candidate_reserve: int
    freshness_threshold: float
    mutation_queue_limit: int


@dataclass(frozen=True)
class SchedulingConfig:
    allow_probe_preempt_search: bool
    allow_intent_preempt_search: bool
    reassignment_cooldown_min: float
    max_tasks_in_prompt: int


@dataclass(frozen=True)
class EvolutionConfig:
    enabled: bool
    max_active_memories: int
    max_memory_chars: int
    min_support_episodes: int
    candidate_generation_interval_episodes: int
    validation_seeds: tuple[int, ...]
    holdout_seeds: tuple[int, ...]
    repeats_per_seed: int
    minimum_score_gain: float
    maximum_component_regression: float


@dataclass(frozen=True)
class MissionConfig:
    contact: ContactConfig
    intent: IntentConfig
    scheduling: SchedulingConfig
    evolution: EvolutionConfig

    def __post_init__(self) -> None:
        # Keep the four-field legacy dataclass shape while exposing the new
        # alignment sections as immutable, non-serialized companions.
        object.__setattr__(self, "activity", ActivityConfig())
        object.__setattr__(self, "evasion", EvasionConfig())
        object.__setattr__(self, "information_update", InformationUpdateConfig())


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError(f"unhashable YAML mapping key: {key!r}") from exc
        if duplicate:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _reject_non_finite_tree(value: object, path: str = "configuration") -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError(f"{path}: expected finite number")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_non_finite_tree(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_non_finite_tree(child, f"{path}[{index}]")


def load_strict_yaml(path: str) -> dict:
    """Load a YAML mapping while rejecting duplicate keys and non-finite values."""
    with open(path, "r", encoding="utf-8") as stream:
        data = yaml.load(stream, Loader=_UniqueKeySafeLoader)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected YAML mapping")
    _reject_non_finite_tree(data)
    return data


DataclassT = TypeVar("DataclassT")


def strict_dataclass(data: object, cls: type[DataclassT], name: str) -> DataclassT:
    """Construct a dataclass without silently dropping unknown configuration keys."""
    if not isinstance(data, dict):
        raise ValueError(f"{name}: expected mapping")
    expected = {field.name for field in fields(cls)}
    unknown = set(data) - expected
    if unknown:
        raise ValueError(f"{name}: unknown fields: {sorted(unknown)}")
    try:
        result = cls(**data)
    except TypeError as exc:
        raise ValueError(f"{name}: invalid fields: {exc}") from exc
    if not is_dataclass(result):
        raise ValueError(f"{name}: expected dataclass type")
    return result


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: expected integer")
    if value < minimum:
        raise ValueError(f"{name}: expected integer >= {minimum}")
    return value


def finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: expected number")
    if not math.isfinite(value):
        raise ValueError(f"{name}: expected finite number")
    return float(value)


def _positive(value: object, name: str) -> float:
    number = finite_number(value, name)
    if number <= 0.0:
        raise ValueError(f"{name}: expected positive number")
    return number


def _non_negative(value: object, name: str) -> float:
    number = finite_number(value, name)
    if number < 0.0:
        raise ValueError(f"{name}: expected non-negative number")
    return number


def _probability(value: object, name: str) -> float:
    number = finite_number(value, name)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name}: expected probability in [0, 1]")
    return number


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name}: expected boolean")
    return value


def validate_mission_config(config: "AppConfig") -> None:
    """Validate ship/mission values and their sensor/grid relationships."""
    ship = config.ship
    contact = config.mission.contact
    intent = config.mission.intent
    scheduling = config.mission.scheduling
    evolution = config.mission.evolution

    initial_count = _integer(ship.initial_ship_count, "ship.initial_ship_count")
    target_count = _integer(ship.target_ship_count, "ship.target_ship_count")
    if target_count > initial_count:
        raise ValueError("ship.target_ship_count must not exceed initial_ship_count")
    population = ship.population
    cols, rows = config.grid.resolution
    if population.total_count > cols * rows:
        raise ValueError("population.total_count exceeds grid capacity")

    _probability(
        ship.target_ais_on_probability, "ship.target_ais_on_probability"
    )
    _probability(ship.turn_speed_loss_fraction, "ship.turn_speed_loss_fraction")
    for name in (
        "speed_kn",
        "ais_update_interval_min",
        "max_turn_rate_deg_min",
        "yaw_time_constant_min",
        "heading_control_gain_per_min",
        "max_acceleration_kn_per_min",
        "detect_uav_radius_cells",
        "clear_uav_radius_cells",
        "clear_hold_min",
        "red_decision_cycle_min",
        "red_plan_valid_min",
        "speed_min_kn",
        "speed_max_kn",
        "heading_offset_max_deg",
        "zigzag_heading_max_deg",
        "zigzag_period_min_min",
        "zigzag_period_max_min",
        "min_evasion_heading_deg",
        "min_evasion_speed_delta_kn",
        "navigation_horizon_min",
        "integration_dt_min",
    ):
        _positive(getattr(ship, name), f"ship.{name}")
    _non_negative(ship.ais_position_noise_cells, "ship.ais_position_noise_cells")
    _non_negative(
        ship.navigation_clearance_cells, "ship.navigation_clearance_cells"
    )
    if ship.clear_uav_radius_cells <= ship.detect_uav_radius_cells:
        raise ValueError(
            "ship.clear_uav_radius_cells must exceed detect_uav_radius_cells"
        )
    if ship.speed_min_kn > ship.speed_max_kn:
        raise ValueError("ship.speed_min_kn must not exceed speed_max_kn")
    if not ship.speed_min_kn <= ship.speed_kn <= ship.speed_max_kn:
        raise ValueError("ship.speed_kn must be within speed_min_kn and speed_max_kn")
    if ship.zigzag_period_min_min > ship.zigzag_period_max_min:
        raise ValueError(
            "ship.zigzag_period_min_min must not exceed zigzag_period_max_min"
        )
    if ship.min_evasion_heading_deg > max(
        ship.heading_offset_max_deg, ship.zigzag_heading_max_deg
    ):
        raise ValueError("ship.min_evasion_heading_deg exceeds available heading range")
    available_speed_delta = max(
        ship.speed_kn - ship.speed_min_kn,
        ship.speed_max_kn - ship.speed_kn,
    )
    if ship.min_evasion_speed_delta_kn > available_speed_delta:
        raise ValueError("ship.min_evasion_speed_delta_kn exceeds available speed range")

    for name in (
        "history_window_min",
        "stale_after_min",
        "association_gate_cells",
        "association_margin_cells",
        "assessment_interval_min",
        "max_sample_gap_min",
        "baseline_duration_min",
        "near_duration_min",
        "baseline_standoff_cells",
        "near_standoff_cells",
        "probe_timeout_min",
        "approach_timeout_min",
        "probe_retry_cooldown_min",
        "civilian_recheck_cooldown_min",
    ):
        _positive(getattr(contact, name), f"mission.contact.{name}")
    for name in (
        "history_max_samples",
        "min_valid_samples_per_phase",
        "prompt_contact_limit",
        "prompt_keypoints_per_contact",
    ):
        _integer(getattr(contact, name), f"mission.contact.{name}", minimum=1)
    _probability(
        contact.assessment_confidence_min,
        "mission.contact.assessment_confidence_min",
    )
    if contact.near_standoff_cells >= ship.detect_uav_radius_cells:
        raise ValueError(
            "mission.contact.near_standoff_cells must be below "
            "ship.detect_uav_radius_cells"
        )
    if ship.detect_uav_radius_cells >= contact.baseline_standoff_cells:
        raise ValueError(
            "ship.detect_uav_radius_cells must be below "
            "mission.contact.baseline_standoff_cells"
        )
    cell_size_km = _positive(config.grid.cell_size_km, "grid.cell_size_km")
    if contact.cell_size_km is not None:
        _positive(contact.cell_size_km, "mission.contact.cell_size_km")
    activity = config.mission.activity
    for bbox in activity.regulated_bboxes:
        if not (
            0 <= bbox[0] < bbox[2] <= cols
            and 0 <= bbox[1] < bbox[3] <= rows
        ):
            raise ValueError("mission.activity.regulated_bboxes must stay within grid")
    if not (
        ship.speed_min_kn
        <= activity.survey_command_speed_kn
        <= activity.observed_speed_max_kn
        <= ship.speed_max_kn
    ):
        raise ValueError(
            "mission.activity.survey_command_speed_kn must stay within ship speed bounds"
        )
    eo_range_cells = _positive(
        config.sensor.eoir.detection_range_km,
        "sensor.eoir.detection_range_km",
    ) / cell_size_km
    if contact.baseline_standoff_cells > eo_range_cells:
        raise ValueError(
            "mission.contact.baseline_standoff_cells exceeds EO clear-sky range"
        )
    minimum_probe_timeout = (
        contact.baseline_duration_min + contact.near_duration_min + 1.0
    )
    if contact.probe_timeout_min <= minimum_probe_timeout:
        raise ValueError(
            "mission.contact.probe_timeout_min must exceed both observation "
            "phases plus one main step"
        )

    for name in (
        "max_active_intents",
        "candidate_limit",
        "mutation_queue_limit",
    ):
        _integer(getattr(intent, name), f"mission.intent.{name}", minimum=1)
    _integer(
        intent.general_candidate_reserve,
        "mission.intent.general_candidate_reserve",
    )
    if intent.general_candidate_reserve > intent.candidate_limit:
        raise ValueError(
            "mission.intent.general_candidate_reserve must not exceed candidate_limit"
        )
    _positive(
        intent.default_valid_duration_min,
        "mission.intent.default_valid_duration_min",
    )
    _positive(
        intent.default_revisit_interval_min,
        "mission.intent.default_revisit_interval_min",
    )
    default_weight = finite_number(
        intent.default_weight, "mission.intent.default_weight"
    )
    if not 0.0 <= default_weight <= 2.0:
        raise ValueError("mission.intent.default_weight must be in [0, 2]")
    _probability(intent.freshness_threshold, "mission.intent.freshness_threshold")

    _boolean(
        scheduling.allow_probe_preempt_search,
        "mission.scheduling.allow_probe_preempt_search",
    )
    _boolean(
        scheduling.allow_intent_preempt_search,
        "mission.scheduling.allow_intent_preempt_search",
    )
    _non_negative(
        scheduling.reassignment_cooldown_min,
        "mission.scheduling.reassignment_cooldown_min",
    )
    _integer(
        scheduling.max_tasks_in_prompt,
        "mission.scheduling.max_tasks_in_prompt",
        minimum=1,
    )

    _boolean(evolution.enabled, "mission.evolution.enabled")
    for name in (
        "max_active_memories",
        "max_memory_chars",
        "min_support_episodes",
        "candidate_generation_interval_episodes",
        "repeats_per_seed",
    ):
        _integer(getattr(evolution, name), f"mission.evolution.{name}", minimum=1)
    for group_name in ("validation_seeds", "holdout_seeds"):
        seeds = getattr(evolution, group_name)
        if not isinstance(seeds, tuple) or not seeds:
            raise ValueError(f"mission.evolution.{group_name}: expected non-empty tuple")
        for index, seed in enumerate(seeds):
            _integer(seed, f"mission.evolution.{group_name}[{index}]")
    _non_negative(
        evolution.minimum_score_gain,
        "mission.evolution.minimum_score_gain",
    )
    _non_negative(
        evolution.maximum_component_regression,
        "mission.evolution.maximum_component_regression",
    )


__all__ = [
    "ActivityConfig",
    "ContactConfig",
    "EmitterConfig",
    "EvasionConfig",
    "EvolutionConfig",
    "InformationUpdateConfig",
    "IntentConfig",
    "MissionConfig",
    "PassiveConfig",
    "PopulationConfig",
    "SchedulingConfig",
    "finite_number",
    "load_strict_yaml",
    "strict_dataclass",
    "validate_mission_config",
]

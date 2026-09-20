from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
import shutil

import pytest
import yaml

from src.mission.config import load_strict_yaml, validate_mission_config
from src.schedule.config_loader import ConfigLoader


CONFIG_NAMES = (
    "common.yaml",
    "control.yaml",
    "environment.yaml",
    "llm_params.yaml",
    "mission.yaml",
    "sensor.yaml",
    "ship.yaml",
    "uav.yaml",
)


def _copy_configs(tmp_path: Path) -> Path:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for config_name in CONFIG_NAMES:
        shutil.copy(Path("configs") / config_name, config_dir / config_name)
    return config_dir


def test_ship_and_mission_config_fields_match_design():
    config = ConfigLoader.load()

    assert tuple(field.name for field in fields(config.ship)) == (
        "population",
        "type_ii_ais_on_probability",
        "speed_kn",
        "ais_update_interval_min",
        "ais_position_noise_cells",
        "max_turn_rate_deg_min",
        "yaw_time_constant_min",
        "heading_control_gain_per_min",
        "turn_speed_loss_fraction",
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
        "navigation_clearance_cells",
    )
    assert tuple(field.name for field in fields(config.mission)) == (
        "contact",
        "intent",
        "scheduling",
        "evolution",
        "coverage",
    )
    assert tuple(field.name for field in fields(config.mission.contact)) == (
        "history_window_min",
        "history_max_samples",
        "stale_after_min",
        "association_gate_cells",
        "association_margin_cells",
        "assessment_interval_min",
        "min_valid_samples_per_phase",
        "max_sample_gap_min",
        "baseline_duration_min",
        "near_duration_min",
        "baseline_standoff_cells",
        "near_standoff_cells",
        "probe_timeout_min",
        "approach_timeout_min",
        "probe_retry_cooldown_min",
        "assessment_confidence_min",
        "type_i_recheck_cooldown_min",
        "prompt_contact_limit",
        "prompt_keypoints_per_contact",
        "cell_size_km",
    )
    assert tuple(field.name for field in fields(config.mission.intent)) == (
        "max_active_intents",
        "default_valid_duration_min",
        "default_revisit_interval_min",
        "default_weight",
        "candidate_limit",
        "general_candidate_reserve",
        "freshness_threshold",
        "mutation_queue_limit",
    )
    assert tuple(field.name for field in fields(config.mission.scheduling)) == (
        "allow_probe_preempt_search",
        "allow_intent_preempt_search",
        "reassignment_cooldown_min",
        "max_tasks_in_prompt",
    )
    assert tuple(field.name for field in fields(config.mission.evolution)) == (
        "enabled",
        "max_active_memories",
        "max_memory_chars",
        "min_support_episodes",
        "candidate_generation_interval_episodes",
        "validation_seeds",
        "holdout_seeds",
        "repeats_per_seed",
        "minimum_score_gain",
        "maximum_component_regression",
    )
    assert tuple(field.name for field in fields(config.mission.coverage)) == (
        "windows_min",
        "primary_window_min",
        "min_search_uav_fraction",
        "ordinary_prompt_reserve",
        "geometry_candidate_budget",
        "no_progress_timeout_min",
        "align_timeout_min",
        "max_stall_replans",
        "max_consecutive_decision_failures",
    )
    assert config.mission.activity.regulated_bboxes == ((8, 8, 22, 22),)
    assert config.mission.evasion.enabled is True
    assert config.mission.information_update.planning_deadline_seconds == 30.0


def test_coverage_configuration_defaults_and_explicit_values(tmp_path: Path):
    config = ConfigLoader.load()
    assert config.mission.coverage.windows_min == (30, 60, 120)
    assert config.mission.coverage.primary_window_min == 60
    assert config.mission.coverage.min_search_uav_fraction == 0.4
    assert config.mission.coverage.ordinary_prompt_reserve == 8
    assert config.mission.coverage.geometry_candidate_budget == 120
    assert config.mission.coverage.no_progress_timeout_min == 10.0
    assert config.mission.coverage.align_timeout_min == 8.0
    assert config.mission.coverage.max_stall_replans == 2
    assert config.mission.coverage.max_consecutive_decision_failures == 3

    config_dir = _copy_configs(tmp_path)
    mission_path = config_dir / "mission.yaml"
    mission_data = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    mission_data.pop("coverage", None)
    mission_path.write_text(yaml.safe_dump(mission_data), encoding="utf-8")
    assert ConfigLoader.load(str(config_dir)).mission.coverage == config.mission.coverage


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("windows_min", [30, 30, 120], "windows_min"),
        ("primary_window_min", True, "primary_window_min"),
        ("primary_window_min", 90, "primary_window_min"),
        ("min_search_uav_fraction", True, "min_search_uav_fraction"),
        ("ordinary_prompt_reserve", True, "ordinary_prompt_reserve"),
        ("geometry_candidate_budget", 1, "geometry_candidate_budget"),
        ("no_progress_timeout_min", True, "no_progress_timeout_min"),
        ("no_progress_timeout_min", 0, "no_progress_timeout_min"),
        ("align_timeout_min", True, "align_timeout_min"),
        ("align_timeout_min", float("inf"), "finite"),
        ("max_stall_replans", True, "max_stall_replans"),
        ("max_stall_replans", -1, "max_stall_replans"),
        ("max_consecutive_decision_failures", True, "max_consecutive_decision_failures"),
        ("max_consecutive_decision_failures", 0, "max_consecutive_decision_failures"),
    ],
)
def test_coverage_configuration_rejects_invalid_values(
    tmp_path: Path, field_name: str, value: object, message: str
):
    config_dir = _copy_configs(tmp_path)
    mission_path = config_dir / "mission.yaml"
    mission_data = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    mission_data["coverage"][field_name] = value
    mission_path.write_text(yaml.safe_dump(mission_data), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        ConfigLoader.load(str(config_dir))


def test_coverage_configuration_rejects_unknown_fields(tmp_path: Path):
    config_dir = _copy_configs(tmp_path)
    mission_path = config_dir / "mission.yaml"
    mission_data = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    mission_data["coverage"]["unexpected"] = 1
    mission_path.write_text(yaml.safe_dump(mission_data), encoding="utf-8")

    with pytest.raises(ValueError, match="mission.coverage.*unknown.*unexpected"):
        ConfigLoader.load(str(config_dir))


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("ordinary_prompt_reserve", 41, "ordinary_prompt_reserve"),
        ("geometry_candidate_budget", 39, "geometry_candidate_budget"),
    ],
)
def test_coverage_configuration_respects_prompt_capacity(
    tmp_path: Path, field_name: str, value: int, message: str
):
    config_dir = _copy_configs(tmp_path)
    mission_path = config_dir / "mission.yaml"
    mission_data = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    mission_data["coverage"][field_name] = value
    mission_path.write_text(yaml.safe_dump(mission_data), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        ConfigLoader.load(str(config_dir))


def test_loaded_ship_config_is_immutable():
    config = ConfigLoader.load()

    with pytest.raises(FrozenInstanceError):
        config.ship.population = config.ship.population


def test_zero_population_is_valid_when_all_ratios_are_type_i():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(
            config.ship,
            population=replace(
                config.ship.population,
                total_count=0,
                type_i_ratio=1.0,
                type_ii_ratio=0.0,
            ),
        ),
    )

    validate_mission_config(config)


def test_population_total_cannot_exceed_grid_capacity():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(
            config.ship,
            population=replace(config.ship.population, total_count=1000),
        ),
    )

    with pytest.raises(ValueError, match="population.total_count"):
        validate_mission_config(config)


def test_activity_region_and_survey_speed_stay_inside_operational_bounds():
    config = ConfigLoader.load()
    object.__setattr__(
        config.mission,
        "activity",
        replace(
            config.mission.activity,
            regulated_bboxes=((-1, 0, 22, 22),),
            survey_command_speed_kn=10.0,
        ),
    )

    with pytest.raises(ValueError, match="regulated_bboxes"):
        validate_mission_config(config)

    object.__setattr__(
        config.mission,
        "activity",
        replace(
            config.mission.activity,
            regulated_bboxes=((8, 8, 22, 22),),
            survey_command_speed_kn=9.0,
        ),
    )
    with pytest.raises(ValueError, match="survey_command_speed_kn"):
        validate_mission_config(config)


def test_population_ratios_must_sum_to_one():
    config = ConfigLoader.load()
    with pytest.raises(ValueError, match="sum to 1"):
        replace(
            config,
            ship=replace(
                config.ship,
                population=replace(
                    config.ship.population,
                    type_i_ratio=0.5,
                    type_ii_ratio=0.6,
                ),
            ),
        )


def test_boolean_population_count_is_rejected():
    config = ConfigLoader.load()
    with pytest.raises(ValueError, match="population.total_count.*integer"):
        replace(
            config,
            ship=replace(
                config.ship,
                population=replace(config.ship.population, total_count=True),
            ),
        )


def test_negative_type_ii_ais_probability_is_rejected():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(config.ship, type_ii_ais_on_probability=-0.01),
    )

    with pytest.raises(ValueError, match="type_ii_ais_on_probability"):
        validate_mission_config(config)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_configuration_values_are_rejected(value):
    config = ConfigLoader.load()
    config = replace(config, ship=replace(config.ship, speed_kn=value))

    with pytest.raises(ValueError, match="speed_kn.*finite"):
        validate_mission_config(config)


def test_near_standoff_must_be_below_detection_radius():
    config = ConfigLoader.load()
    config = replace(
        config,
        mission=replace(
            config.mission,
            contact=replace(
                config.mission.contact,
                near_standoff_cells=config.ship.detect_uav_radius_cells,
            ),
        ),
    )

    with pytest.raises(ValueError, match="near_standoff.*detect_uav_radius"):
        validate_mission_config(config)


def test_baseline_standoff_cannot_exceed_eo_clear_sky_range():
    config = ConfigLoader.load()
    eo_range_cells = (
        config.sensor.eoir.detection_range_km / config.grid.cell_size_km
    )
    config = replace(
        config,
        mission=replace(
            config.mission,
            contact=replace(
                config.mission.contact,
                baseline_standoff_cells=eo_range_cells + 0.01,
            ),
        ),
    )

    with pytest.raises(ValueError, match="baseline_standoff.*EO"):
        validate_mission_config(config)


def test_detection_and_clearance_radii_are_ordered():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(
            config.ship,
            clear_uav_radius_cells=config.ship.detect_uav_radius_cells,
        ),
    )

    with pytest.raises(ValueError, match="clear_uav_radius.*detect_uav_radius"):
        validate_mission_config(config)


def test_normal_ship_speed_must_be_within_configured_bounds():
    config = ConfigLoader.load()
    config = replace(
        config,
        ship=replace(config.ship, speed_kn=config.ship.speed_max_kn + 0.1),
    )

    with pytest.raises(ValueError, match="speed_kn.*speed_min_kn.*speed_max_kn"):
        validate_mission_config(config)


def test_probe_timeout_must_cover_both_observation_phases_and_main_step():
    config = ConfigLoader.load()
    contact = config.mission.contact
    config = replace(
        config,
        mission=replace(
            config.mission,
            contact=replace(
                contact,
                probe_timeout_min=(
                    contact.baseline_duration_min + contact.near_duration_min + 1.0
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="probe_timeout_min"):
        validate_mission_config(config)


def test_duplicate_cycles_are_rejected(tmp_path: Path):
    path = tmp_path / "llm.yaml"
    path.write_text(
        "cycles: {max_retries: 2}\ncycles: {max_retries: 3}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_strict_yaml(str(path))


def test_unknown_nested_mission_field_is_rejected(tmp_path: Path):
    config_dir = _copy_configs(tmp_path)
    mission_path = config_dir / "mission.yaml"
    mission_data = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    mission_data["contact"]["stale_after_minutes"] = 5.0
    mission_path.write_text(yaml.safe_dump(mission_data), encoding="utf-8")

    with pytest.raises(ValueError, match="mission.contact.*unknown.*stale_after_minutes"):
        ConfigLoader.load(str(config_dir))


def test_malformed_evolution_section_has_contextual_value_error(tmp_path: Path):
    config_dir = _copy_configs(tmp_path)
    mission_path = config_dir / "mission.yaml"
    mission_data = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    mission_data["evolution"] = []
    mission_path.write_text(yaml.safe_dump(mission_data), encoding="utf-8")

    with pytest.raises(ValueError, match=r"mission\.evolution: expected mapping"):
        ConfigLoader.load(str(config_dir))


@pytest.mark.parametrize(
    ("field_name", "malformed_value"),
    [("validation_seeds", 101), ("holdout_seeds", {"seed": 201})],
)
def test_malformed_seed_collection_has_contextual_value_error(
    tmp_path: Path, field_name: str, malformed_value: object
):
    config_dir = _copy_configs(tmp_path)
    mission_path = config_dir / "mission.yaml"
    mission_data = yaml.safe_load(mission_path.read_text(encoding="utf-8"))
    mission_data["evolution"][field_name] = malformed_value
    mission_path.write_text(yaml.safe_dump(mission_data), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=rf"mission\.evolution\.{field_name}: expected sequence",
    ):
        ConfigLoader.load(str(config_dir))


def test_legacy_ship_fields_have_an_explicit_migration_error(tmp_path: Path):
    config_dir = _copy_configs(tmp_path)
    ship_path = config_dir / "ship.yaml"
    ship_data = yaml.safe_load(ship_path.read_text(encoding="utf-8"))
    ship_data["count_min"] = 3
    ship_path.write_text(yaml.safe_dump(ship_data), encoding="utf-8")

    with pytest.raises(ValueError, match="legacy ship configuration.*count_min"):
        ConfigLoader.load(str(config_dir))


def test_legacy_ship_population_fields_have_an_explicit_migration_error(tmp_path: Path):
    config_dir = _copy_configs(tmp_path)
    ship_path = config_dir / "ship.yaml"
    ship_data = yaml.safe_load(ship_path.read_text(encoding="utf-8"))
    ship_data.pop("population")
    ship_data["population"] = {
        "total_count": 8,
        "unsupported_ratio": 1.0,
    }
    ship_path.write_text(yaml.safe_dump(ship_data), encoding="utf-8")

    with pytest.raises(ValueError, match="requires type_i_ratio"):
        ConfigLoader.load(str(config_dir))


def test_legacy_uav_count_field_has_an_explicit_migration_error(tmp_path: Path):
    config_dir = _copy_configs(tmp_path)
    uav_path = config_dir / "uav.yaml"
    uav_data = yaml.safe_load(uav_path.read_text(encoding="utf-8"))
    uav_data["count_max"] = uav_data.pop("count")
    uav_path.write_text(yaml.safe_dump(uav_data), encoding="utf-8")

    with pytest.raises(ValueError, match="uav.*use count instead of count_max"):
        ConfigLoader.load(str(config_dir))

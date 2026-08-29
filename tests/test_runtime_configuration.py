from pathlib import Path
import shutil

import pytest
import yaml

from main import clear_output_cache
from src.schedule.config_loader import ConfigLoader


def test_config_loader_reads_merged_environment_and_llm_parameters():
    config = ConfigLoader.load()

    assert config.grid.resolution == (30, 30)
    assert config.grid.cell_size_km == 10
    assert config.llm.heavy_cycle_min == 30
    assert config.llm.reviewer_cycle_min == 15
    # Retaining prior runs is required for replay after a new live run starts.
    assert config.common.clear_outputs_before_run is False
    assert config.environment.base_position == (2, 14)


def test_control_configuration_defaults_to_heuristic():
    config = ConfigLoader.load()

    assert config.control.default_mode == "heuristic"
    assert config.control.per_uav == {}
    assert config.control.observation.schema_version == "control-observation/v1"
    assert config.control.observation.local_window_cells == 11
    assert config.control.safety.reserve_range_cells == 4.0
    assert config.control.safety.max_invalid_commands == 3
    assert config.control.heuristic.astar_dynamic_replan_limit == 3
    assert config.control.heuristic.astar_xy_resolution_cells == 0.5
    assert config.control.heuristic.astar_heading_bins == 72
    assert config.control.heuristic.astar_candidate_limit == 32
    assert config.control.heuristic.astar_primitive_length_cells == 1.0


@pytest.mark.parametrize("local_window_cells", [1.5, 3.0])
def test_control_configuration_rejects_non_integer_local_window_cells(
    tmp_path: Path, local_window_cells: float
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    for config_name in (
        "common.yaml",
        "control.yaml",
        "environment.yaml",
        "llm_params.yaml",
        "sensor.yaml",
        "ship.yaml",
        "uav.yaml",
    ):
        shutil.copy(Path("configs") / config_name, config_dir / config_name)
    control_path = config_dir / "control.yaml"
    control_data = yaml.safe_load(control_path.read_text(encoding="utf-8"))
    control_data["observation"]["local_window_cells"] = local_window_cells
    control_path.write_text(yaml.safe_dump(control_data), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="control observation local_window_cells must be a positive odd integer",
    ):
        ConfigLoader.load(str(config_dir))


def test_clear_output_cache_removes_only_output_directory_contents(tmp_path: Path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / "run.jsonl").write_text("frame\n", encoding="utf-8")
    nested = output_dir / "stale"
    nested.mkdir()
    (nested / "cache.txt").write_text("cache\n", encoding="utf-8")

    assert clear_output_cache(str(output_dir)) == 2
    assert list(output_dir.iterdir()) == []

    with pytest.raises(ValueError, match="outputs directory"):
        clear_output_cache(str(tmp_path / "not-outputs"))

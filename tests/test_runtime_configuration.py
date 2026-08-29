from pathlib import Path

import pytest

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

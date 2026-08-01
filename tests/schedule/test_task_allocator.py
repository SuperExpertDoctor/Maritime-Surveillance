import pytest
from src.schedule.config_loader import ConfigLoader
from src.schedule.task_allocator import TaskAllocator
from src.schedule.datatypes import BBox, GridCoord, Region


@pytest.fixture
def config():
    return ConfigLoader.load()


@pytest.fixture
def allocator(config):
    return TaskAllocator(config)


def test_allocator_initializes_all_components(allocator):
    assert allocator.sm is not None
    assert allocator.ivt is not None
    assert allocator.extractor is not None
    assert allocator.llm_client is not None
    assert allocator.trigger_manager is not None


def test_step_initial_no_trigger(allocator):
    result = allocator.step(0.0)
    assert result["trigger_type"] == "none"


def test_step_heavy_trigger_at_cycle(allocator):
    cycle = allocator.config.llm.heavy_cycle_min
    result = allocator.step(cycle)
    assert result["trigger_type"] == "heavy"
    assert "search_regions" in result


def test_uav_search_complete_light_trigger(allocator):
    allocator.sm.current_time = 10.0
    base = allocator.sm.config.environment.base_position
    allocator.sm.create_track_region("G1", GridCoord(*base))
    allocator.ivt.add_row("S1",
        BBox(10, 20, 16, 26), "search", "UAV-1")
    allocator.trigger_manager.notify_event(
        "search_complete", time=10.0, uav_id="UAV-1", region_id="S1")
    result = allocator.step(10.0)
    assert result["trigger_type"] in ("light", "none")


def test_light_trigger_never_reassigns_search_region_overlapping_track(allocator):
    conflict = Region(
        id="S-conflict",
        bbox=BBox(12, 12, 17, 17),
        type="search",
    )
    safe = Region(
        id="S-safe",
        bbox=BBox(20, 20, 25, 25),
        type="search",
    )
    allocator.sm.set_search_regions([conflict, safe])
    allocator.ivt.add_row(conflict.id, conflict.bbox, "search")
    allocator.ivt.add_row(safe.id, safe.bbox, "search")
    allocator.sm.create_track_region("G1", GridCoord(14, 14))
    allocator.trigger_manager.notify_event(
        "search_complete", time=10.0, uav_id="UAV-1", region_id="S-old",
    )

    result = allocator.step(10.0)

    assert result["action"] == "hungarian_pairing"
    assert [region.id for region in allocator.sm.get_search_regions()] == ["S-safe"]
    assert all(region_id != "S-conflict" for _, region_id in result["pairs"])
    assert allocator.ivt.get_row("S-conflict") is None

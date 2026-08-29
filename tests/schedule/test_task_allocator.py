import pytest
import src.schedule.task_allocator as task_allocator_module
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


def test_uav_search_complete_triggers_reallocation_when_work_exists(allocator, monkeypatch):
    allocator.sm.current_time = 10.0
    base = allocator.sm.config.environment.base_position
    allocator.sm.create_track_region("G1", GridCoord(*base))
    allocator.ivt.add_row("S1",
        BBox(10, 20, 16, 26), "search", "UAV-1")
    allocator.trigger_manager.notify_event(
        "search_complete", time=10.0, uav_id="UAV-1", region_id="S1")
    def decide(state, table, candidates, required_search_regions=0):
        allocator.llm_client.last_interaction = {"success": True}
        return {
            "search_regions": [
                {"id": f"S{index + 1}", "bbox": list(candidate["bbox"])}
                for index, candidate in enumerate(candidates.candidate_regions[:required_search_regions])
            ],
            "notes": "coverage refresh",
        }

    monkeypatch.setattr(allocator.llm_client, "decide", decide)
    result = allocator.step(10.0)
    assert result["trigger_type"] == "heavy"


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


def test_heavy_allocation_requires_parallel_regions_for_idle_uavs(allocator, monkeypatch):
    initial_available = len(allocator.sm.get_available_uavs())
    captured = {}

    def decide(state, table, candidates, required_search_regions=0):
        captured["required"] = required_search_regions
        selected = candidates.candidate_regions[:required_search_regions]
        allocator.llm_client.last_interaction = {"success": True}
        return {
            "search_regions": [
                {"id": f"S{index + 1}", "bbox": list(item["bbox"])}
                for index, item in enumerate(selected)
            ],
            "notes": "parallel coverage",
        }

    monkeypatch.setattr(allocator.llm_client, "decide", decide)
    result = allocator._handle_heavy_trigger(30.0, object())

    assert captured["required"] == min(initial_available, len(result["search_regions"]))
    assert captured["required"] > 0
    assert len(result["pairs"]) == captured["required"]
    assert len({region_id for _, region_id in result["pairs"]}) == captured["required"]


def test_light_completion_escalates_when_new_coverage_work_is_available(allocator, monkeypatch):
    calls = []

    def decide(state, table, candidates, required_search_regions=0):
        calls.append(required_search_regions)
        allocator.llm_client.last_interaction = {"success": True}
        return {
            "search_regions": [
                {"id": "S-new", "bbox": list(candidates.candidate_regions[0]["bbox"])}
            ] if required_search_regions else [],
            "notes": "replace completed search",
        }

    monkeypatch.setattr(allocator.llm_client, "decide", decide)
    allocator.trigger_manager.notify_event(
        "search_complete", time=10.0, uav_id="UAV-1", region_id="S-old",
    )

    result = allocator.step(10.0)

    assert result["trigger_type"] == "heavy"
    assert calls and calls[0] > 0


@pytest.mark.parametrize(
    ("pairing_path", "control_mode"),
    [("light", "bc"), ("heavy", "rl")],
)
def test_pairing_paths_never_include_idle_learning_airframes(
    allocator, monkeypatch, pairing_path, control_mode
):
    allocator.sm.update_uav_control(
        "UAV-1", control_mode, "learning", "idle", 1, False
    )
    region = Region(
        id="S-unassigned",
        bbox=BBox(10, 10, 15, 15),
        type="search",
    )
    allocator.sm.set_search_regions([region])
    captured_uav_ids = []

    def capture_hungarian(uavs, regions):
        del regions
        captured_uav_ids.extend(uav["id"] for uav in uavs)
        return []

    monkeypatch.setattr(task_allocator_module, "hungarian_pair", capture_hungarian)

    if pairing_path == "light":
        allocator._handle_light_trigger(10.0, object())
    else:
        allocator._pair_available_regions([region])

    assert "UAV-1" not in captured_uav_ids

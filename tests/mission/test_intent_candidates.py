import numpy as np

from src.mission.contracts import Intent
from src.schedule.candidate_extractor import CandidateExtractor
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager


def _intent(intent_id, bbox):
    return Intent(
        intent_id=intent_id,
        revision=1,
        label=intent_id,
        bbox=bbox,
        mode="search_priority",
        priority="high",
        weight=1.0,
        created_at_min=0.0,
        expires_at_min=100.0,
        revisit_interval_min=None,
        lifecycle="active",
    )


def test_intent_weighted_candidates_remain_legal_search_rectangles_and_merge_ids():
    sm = StateManager(ConfigLoader.load())
    sm.current_time = 10.0
    scheduling_value = np.zeros(sm.config.grid.resolution)
    scheduling_value[10:15, 10:15] = 5.0
    intents = (_intent("I0001", (9, 9, 15, 15)), _intent("I0002", (10, 10, 16, 16)))

    result = CandidateExtractor().extract(
        sm, scheduling_value=scheduling_value, intents=intents
    )

    matching = [item for item in result.candidate_regions if item.get("intent_ids")]
    assert matching
    assert any(item["intent_ids"] == ("I0001", "I0002") for item in matching)
    assert all(item["bbox"] not in {intent.bbox for intent in intents} for item in matching)
    assert all(
        sm.config.grid.search_min_cells <= item["cell_count"] <= sm.config.grid.search_max_cells
        for item in matching
    )


def test_intent_candidates_are_available_without_idle_uavs():
    sm = StateManager(ConfigLoader.load())
    sm.current_time = 10.0
    for uav in sm.get_all_uavs():
        uav.status = "transit"
    scheduling_value = np.zeros(sm.config.grid.resolution)
    scheduling_value[10:15, 10:15] = 5.0

    result = CandidateExtractor().extract(
        sm,
        scheduling_value=scheduling_value,
        intents=(_intent("I0001", (9, 9, 16, 16)),),
    )

    assert result.candidate_regions
    assert any(item.get("intent_ids") == ("I0001",) for item in result.candidate_regions)

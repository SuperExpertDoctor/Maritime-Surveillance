from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.info_value_table import InfoValueTable
from src.schedule.prompt_builder import PromptBuilder
from src.schedule.state_manager import StateManager
from src.schedule.datatypes import BBox, GridCoord


def test_empty_candidates_explicitly_require_empty_model_output():
    state = StateManager(ConfigLoader.load())
    table = InfoValueTable(state)

    _, user_prompt = PromptBuilder().build(
        state,
        table,
        CandidateResult(),
    )

    assert '"search_regions": []' in user_prompt
    assert "不得自行创造 bbox" in user_prompt


def test_lifecycle_prompt_reserves_slots_for_returning_uavs():
    state = StateManager(ConfigLoader.load())
    state.lifecycle_mode = True
    for uav in state.get_all_uavs():
        uav.status = "returning"

    _, user_prompt = PromptBuilder().build(
        state,
        InfoValueTable(state),
        CandidateResult(),
    )

    assert user_prompt.count("10") >= 2


def test_prompt_uses_only_recorded_target_observations_for_handoff_context():
    state = StateManager(ConfigLoader.load())
    state.current_time = 17.0
    state.record_target_observation("G-contact", GridCoord(20, 16), "UAV-3")
    candidates = CandidateResult(candidate_regions=[{
        "bbox": BBox(18, 14, 23, 19),
        "cell_count": 25,
        "avg_info": 0.1,
        "total_value": 1001.0,
        "target_group_id": "G-contact",
    }])

    _, user_prompt = PromptBuilder().build(state, InfoValueTable(state), candidates)

    assert "G-contact" in user_prompt
    assert "最后观测=(20,16)" in user_prompt
    assert "未观测船舶不可推断" in user_prompt
    assert "接力目标=G-contact" in user_prompt

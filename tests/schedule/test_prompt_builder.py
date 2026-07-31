from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.info_value_table import InfoValueTable
from src.schedule.prompt_builder import PromptBuilder
from src.schedule.state_manager import StateManager


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

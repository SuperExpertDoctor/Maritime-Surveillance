from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, Region
from src.schedule.info_value_table import InfoValueTable
from src.schedule.llm_client import LLMClient
from src.schedule.state_manager import StateManager


def test_full_retained_capacity_accepts_empty_real_model_plan(monkeypatch):
    config = ConfigLoader.load()
    state = StateManager(config)
    state._search_regions = [
        Region(
            id=f"S{index}",
            bbox=BBox(1, 1, 5, 6),
            type="search",
        )
        for index in range(10)
    ]
    client = LLMClient(config)
    monkeypatch.setattr(
        client,
        "_call_api",
        lambda *args, **kwargs: '{"search_regions": [], "notes": "capacity full"}',
    )
    candidates = CandidateResult(candidate_regions=[{
        "bbox": BBox(1, 1, 5, 6),
        "cell_count": 20,
        "avg_info": 0.0,
        "total_value": 1.0,
    }])

    result = client.decide(state, InfoValueTable(state), candidates)

    assert result["search_regions"] == []
    assert client.last_interaction["success"]
    assert len(client.last_interaction["attempts"]) == 1


def test_probe_uses_a_short_bounded_longcat_request(monkeypatch):
    client = LLMClient(ConfigLoader.load())
    captured = {}

    def fake_call(system_prompt, user_prompt, **kwargs):
        captured.update(kwargs)
        assert "connectivity probe" in system_prompt
        assert user_prompt == "Reply with OK."
        return "OK"

    monkeypatch.setattr(client, "_call_api", fake_call)

    assert client.probe(timeout_seconds=7.5) == "OK"
    assert captured == {
        "role": "decision_maker",
        "json_mode": False,
        "max_tokens": 8,
        "timeout_seconds": 7.5,
    }

import json

import pytest

from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox, Region
from src.schedule.info_value_table import InfoValueTable
from src.schedule.llm_client import LLMClient
from src.schedule.llm_reviewer import LLMReviewer
from src.schedule.state_manager import StateManager
from tests.mission.conftest import ScriptedTransport


def test_full_retained_capacity_accepts_empty_real_model_plan():
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
    transport = ScriptedTransport({
        "decision_maker": [
            '{"search_regions": [], "notes": "capacity full"}'
        ],
    })
    client = LLMClient(config, transport=transport)
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
    assert transport.calls[0]["role"] == "decision_maker"


def test_probe_uses_a_short_bounded_longcat_request(monkeypatch):
    monkeypatch.setenv("LONGCAT_API_KEY", "offline-probe-key")
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


def test_decision_retries_when_parallel_allocation_is_incomplete():
    config = ConfigLoader.load()
    state = StateManager(config)
    transport = ScriptedTransport({
        "decision_maker": [
            '{"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13]}]}',
            '{"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13]}, {"id": "S2", "bbox": [14, 8, 18, 13]}]}',
        ],
    })
    client = LLMClient(config, transport=transport)
    candidates = CandidateResult(candidate_regions=[
        {"bbox": BBox(8, 8, 12, 13), "cell_count": 20, "avg_info": 0.0, "total_value": 20.0},
        {"bbox": BBox(14, 8, 18, 13), "cell_count": 20, "avg_info": 0.0, "total_value": 20.0},
    ])
    result = client.decide(
        state,
        InfoValueTable(state),
        candidates,
        required_search_regions=2,
    )

    assert len(result["search_regions"]) == 2
    assert client.last_interaction["success"]
    assert len(client.last_interaction["attempts"]) == 2
    assert transport.calls[1]["messages"][-2]["role"] == "assistant"


def test_legacy_decide_corrects_extra_schema_fields():
    config = ConfigLoader.load()
    state = StateManager(config)
    state._search_regions = [
        Region(id=f"S{index}", bbox=BBox(1, 1, 5, 6), type="search")
        for index in range(10)
    ]
    transport = ScriptedTransport({
        "decision_maker": [
            '{"search_regions": [], "notes": "bad", "extra": true}',
            '{"search_regions": [], "notes": "corrected"}',
        ],
    })
    client = LLMClient(config, transport=transport)

    result = client.decide(
        state,
        InfoValueTable(state),
        CandidateResult(candidate_regions=[]),
    )

    assert result == {"search_regions": [], "notes": "corrected"}
    assert len(transport.calls) == 2
    assert client.last_interaction["attempts"][0]["errors"] == [
        "unexpected top-level fields: ['extra']"
    ]


def test_legacy_decide_keeps_recognizable_fail_closed_wrapper():
    config = ConfigLoader.load()
    state = StateManager(config)
    transport = ScriptedTransport({"decision_maker": ["{bad}"] * 3})
    client = LLMClient(config, transport=transport)

    result = client.decide(
        state,
        InfoValueTable(state),
        CandidateResult(candidate_regions=[]),
    )

    assert result == {
        "search_regions": [],
        "notes": "LLM failed after max retries",
    }
    assert client.last_interaction["success"] is False
    assert client.last_interaction["failure_category"] == "validation"
    assert len(client.last_interaction["attempts"]) == 3


class _ReviewerState:
    @staticmethod
    def get_coverage_stats():
        return {"coverage_pct": 42.0}

    @staticmethod
    def get_recent_events(since_time):
        return [{"type": "contact_created", "time": since_time}]

    @staticmethod
    def get_track_regions():
        return []

    @staticmethod
    def get_all_uavs():
        return []


def test_reviewer_wrapper_routes_text_through_isolated_gateway():
    config = ConfigLoader.load()
    transport = ScriptedTransport({"reviewer": ["reviewer memory"]})
    client = LLMClient(config, transport=transport)
    reviewer = LLMReviewer(config, client)

    result = reviewer.step(15.0, _ReviewerState())

    assert result == "reviewer memory"
    assert reviewer.memory == "reviewer memory"
    assert transport.calls[0]["role"] == "reviewer"
    assert transport.calls[0]["json_mode"] is False
    assert client.last_reviewer_interaction["success"] is True
    assert client.last_reviewer_interaction["snapshot_id"] == "review-15.0"


@pytest.mark.parametrize("raw", [
    '{}',
    '{"search_regions": null}',
    '{"search_regions": {}}',
    '{"search_regions": [], "notes": 12}',
    '{"search_regions": [null]}',
    '{"search_regions": [{"id": 1, "bbox": [8, 8, 12, 13]}]}',
    '{"search_regions": [{"id": "S1", "bbox": ["8", 8, 12, 13]}]}',
    '{"search_regions": [{"id": "S1", "bbox": [true, 8, 12, 13]}]}',
    '{"search_regions": [{"id": "S1", "bbox": [8.0, 8, 12, 13]}]}',
    '{"search_regions": [{"id": "S1", "bbox": null}]}',
    '{"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13], "extra": 1}]}',
    '{"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13], "priority": []}]}',
    '{"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13], "priority": "urgent"}]}',
    '{"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13], "reason": 42}]}',
    '{"search_regions": [], "search_regions": []}',
    'prefix ```json\n{"search_regions": []}\n```',
])
def test_legacy_decide_corrects_malformed_schema_before_business_validator(raw):
    config = ConfigLoader.load()
    state = StateManager(config)
    transport = ScriptedTransport({"decision_maker": [raw, '{"search_regions": []}']})
    client = LLMClient(config, transport=transport)
    result = client.decide(state, InfoValueTable(state), CandidateResult(candidate_regions=[]))
    assert result == {"search_regions": []}
    assert len(transport.calls) == 2
    assert transport.calls[1]["messages"][-2]["content"] == raw
    assert client.last_interaction["attempts"][0]["errors"]
    assert client.last_interaction["snapshot_id"] == "decision-0.0"


def test_reviewer_failure_preserves_memory_and_redacts_diagnostics(monkeypatch, caplog):
    secret = "offline-reviewer-secret"
    monkeypatch.setenv("LONGCAT_API_KEY", secret)
    config = ConfigLoader.load()
    error = RuntimeError(f"Authorization: Bearer {secret}")
    transport = ScriptedTransport({"reviewer": ["previous memory", error, error, error]})
    client = LLMClient(config, transport=transport)
    reviewer = LLMReviewer(config, client)
    assert reviewer.step(15.0, _ReviewerState()) == "previous memory"
    assert reviewer.step(30.0, _ReviewerState()) is None
    assert reviewer.memory == "previous memory"
    assert not client.last_reviewer_interaction["success"]
    assert client.last_reviewer_interaction["failure_category"] == "transport"
    assert secret not in json.dumps(client.last_reviewer_interaction)
    assert secret not in caplog.text


def test_legacy_interaction_logs_redact_prompt_and_raw(monkeypatch):
    secret = "offline-decision-secret"
    monkeypatch.setenv("LONGCAT_API_KEY", secret)
    config = ConfigLoader.load()
    state = StateManager(config)
    raw = json.dumps({"search_regions": [], "notes": secret})
    transport = ScriptedTransport({"decision_maker": [raw]})
    client = LLMClient(config, transport=transport)
    client.set_reviewer_memory(secret)
    result = client.decide(state, InfoValueTable(state), CandidateResult(candidate_regions=[]))
    assert result["notes"] == secret
    assert secret not in json.dumps(client.last_interaction)


def test_legacy_parse_helper_uses_strict_gateway_parser():
    assert LLMClient._parse_json('{"a": 1, "a": 2}') is None
    assert LLMClient._parse_json('[]') is None
    assert LLMClient._parse_json('prefix ```json\n{"a": 1}\n```') is None
    assert LLMClient._parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_decide_accepts_priority_and_reason_declared_in_existing_prompt():
    config = ConfigLoader.load()
    state = StateManager(config)
    payload = {"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13],
                                   "priority": "high", "reason": "observed candidate"}]}
    transport = ScriptedTransport({"decision_maker": [json.dumps(payload)] * 3})
    client = LLMClient(config, transport=transport)
    candidates = CandidateResult(candidate_regions=[
        {"bbox": BBox(8, 8, 12, 13), "cell_count": 20, "avg_info": 0.0, "total_value": 20.0},
    ])
    result = client.decide(state, InfoValueTable(state), candidates)
    assert result == payload
    assert len(transport.calls) == 1


@pytest.mark.parametrize("field,value", [("priority", []), ("priority", "urgent"), ("reason", 42)])
def test_legacy_optional_fields_are_validated_before_geometry(field, value):
    payload = {"search_regions": [{"id": "S1", "bbox": [8, 8, 12, 13], field: value}]}
    assert LLMClient._schema_errors(payload)

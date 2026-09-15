from dataclasses import replace

import pytest

from src.mission.contracts import (
    ContactSnapshot,
    FeasibleEdge,
    Intent,
    MissionSnapshot,
    TaskCandidate,
    UavResource,
)
from src.mission.mission_scheduler import MissionScheduler
from src.mission.outcome_evaluator import EpisodeOutcome
from src.mission.strategy_memory import StrategyMemory, StrategyMemoryStore


def _outcome(episode_id, *, valid=True, score=0.7):
    return EpisodeOutcome(
        episode_id,
        valid,
        (),
        0.7,
        0.8,
        0.6,
        0.9,
        0.0,
        1.0,
        10.0,
        2.0,
        1,
        score,
        1,
        1,
        0,
        1.0,
    )


def _summaries(*, fixture=False):
    return tuple({
        "episode_id": f"episode-{index}",
        "is_fixture": fixture,
        "context": {"has_active_intent": "yes"},
        "advice": "Prioritize the oldest eligible probe before low-value coverage.",
    } for index in range(3))


def test_strategy_candidate_requires_three_valid_live_episodes_and_stays_inactive(tmp_path):
    store = StrategyMemoryStore(tmp_path)
    candidate = store.propose(
        (_outcome("e1"), _outcome("e2")),
        _summaries()[:2],
    )
    assert candidate is None

    candidate = store.propose(
        (_outcome("e1"), _outcome("e2"), _outcome("e3")),
        _summaries(),
    )
    assert candidate is not None
    assert candidate.status == "candidate"
    store.save_candidate(candidate)

    assert store.select_for_context({"has_active_intent": "yes"}, "baseline") == ()
    assert store.select_for_context({"has_active_intent": "yes"}, str(candidate.version)) == ()
    assert store.load_manifest(str(candidate.version))[0].status == "candidate"


def test_fixture_rounds_and_invalid_outcomes_do_not_support_a_candidate(tmp_path):
    store = StrategyMemoryStore(tmp_path)
    outcomes = (_outcome("e1"), _outcome("e2"), _outcome("e3", valid=False))
    assert store.propose(outcomes, _summaries()) is None
    assert store.propose(
        (_outcome("e1"), _outcome("e2"), _outcome("e3")),
        _summaries(fixture=True),
    ) is None


@pytest.mark.parametrize("summary", [
    {"episode_id": "e1", "truth_identity": "target"},
    {"episode_id": "e1", "physical_ship_id": "V1"},
    {"episode_id": "e1", "contact_id": "C0001"},
    {"episode_id": "e1", "truth": {"identity": "target"}},
    {"episode_id": "e1", "selected_task_ids": ["Q1"]},
    {"episode_id": "e1", "preempt_uav_ids": ["UAV-1"]},
    {"episode_id": "e1", "advice": "```python\nprint('x')\n```"},
])
def test_truth_ids_and_code_payloads_are_rejected(tmp_path, summary):
    store = StrategyMemoryStore(tmp_path)
    summaries = (*_summaries()[:2], {**_summaries()[2], **summary})
    assert store.propose(
        (_outcome("e1"), _outcome("e2"), _outcome("e3")), summaries,
    ) is None


def test_invalid_condition_keys_and_long_advice_cannot_be_saved(tmp_path):
    store = StrategyMemoryStore(tmp_path)
    with pytest.raises(ValueError, match="applies_when"):
        StrategyMemory(
            "M0001", 1, "candidate", {"contact_id": "C1"}, "short advice",
            ("e1", "e2", "e3"), {"mean_score": 0.5}, None,
            "2026-01-01T00:00:00+00:00",
        )

    with pytest.raises(ValueError, match="advice"):
        StrategyMemory(
            "M0001", 1, "candidate", {"has_active_intent": "yes"}, "x" * 401,
            ("e1", "e2", "e3"), {"mean_score": 0.5}, None,
            "2026-01-01T00:00:00+00:00",
        )


def test_unknown_context_keys_do_not_match_and_scheduler_receives_only_active_memory(tmp_path):
    store = StrategyMemoryStore(tmp_path)
    memory = StrategyMemory(
        "M0001",
        1,
        "validated",
        {"has_active_intent": "yes"},
        "Prioritize the oldest eligible probe before low-value coverage.",
        ("e1", "e2", "e3"),
        {"mean_score": 0.7},
        "R0001",
        "2026-01-01T00:00:00+00:00",
    )
    store.save_candidate(memory)

    assert store.select_for_context(
        {"has_active_intent": "yes", "unexpected": "value"}, "1"
    ) == ()
    selected = store.select_for_context({"has_active_intent": "yes"}, "1")
    assert selected == (store.load_manifest("1")[0],)

    task = TaskCandidate("Q1", "search", (2, 2, 6, 6), None, (), (), 0.0, "medium", 1.0, 1.0, 0.5)
    resource = UavResource("U1", (1.0, 1.0), 0.0, 1.0, 100.0, "idle", None, 1, 0.0)
    edge = FeasibleEdge("Q1", "U1", 1.0, 5.0, 5.0, 1.0, "route")
    snapshot = MissionSnapshot(
        "E1", 1.0, (task,), ("U1",), (), (("U1", 1),), (resource,),
        (edge,), (), (), (
            Intent("I1", 1, "focus", (2, 2, 6, 6), "search_priority", "high", 1.0, 0.0, 10.0, None, "active"),
        ), (), "1", 1, "",
    )
    scheduler = MissionScheduler(
        gateway=None,
        strategy_memory_store=store,
    )
    payload = scheduler._prompt_payload(snapshot)
    assert payload["snapshot"]["strategy_memories"][0]["memory_id"] == "M0001"
    assert "contact_id" not in payload["snapshot"]["strategy_memories"][0]

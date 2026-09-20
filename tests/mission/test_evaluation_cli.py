import json
from pathlib import Path
import subprocess
import sys

import pytest


def _run(script, *args):
    return subprocess.run(
        [sys.executable, script, *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_evaluation_script_dry_run_never_requests_a_model(tmp_path):
    result = _run(
        "scripts/evaluate_mixed_maritime.py",
        "--scenario", "mixed-ais",
        "--seed", "42",
        "--steps", "10",
        "--output-dir", str(tmp_path),
    )
    payload = json.loads(result.stdout)

    assert payload["mode"] == "dry-run"
    assert payload["planned_episodes"] == 1
    assert payload["api_calls"] == 0


def test_strategy_validation_cli_reports_full_budget_without_live_calls(tmp_path):
    result = _run(
        "scripts/validate_strategy_memory.py",
        "--memory-id", "M0001",
        "--baseline-version", "baseline",
        "--phase", "validation",
        "--output-dir", str(tmp_path),
    )
    payload = json.loads(result.stdout)

    assert payload["mode"] == "dry-run"
    assert payload["planned_episodes"] == 60
    assert payload["api_calls"] == 0


def test_alignment_fixture_batch_writes_explicit_fixture_report(tmp_path):
    output = tmp_path / "alignment-fixture.json"
    result = _run(
        "scripts/evaluate_mixed_maritime.py",
        "--config", "configs",
        "--scenario", "mixed-ais",
        "--seeds", "101",
        "--repeat", "1",
        "--steps", "1",
        "--output", str(output),
        "--transport", "fixture",
    )
    payload = json.loads(result.stdout)

    assert payload["transport"] == "fixture"
    assert payload["is_fixture"] is True
    assert payload["episode_count"] == 1
    assert payload["finished_count"] == 1
    assert payload["audit"]["boundary_violations"] == 0
    assert payload["audit"]["candidate_uncovered_without_reason"] == 0
    assert json.loads(output.read_text(encoding="utf-8"))["transport"] == "fixture"


def test_fixture_gateway_supports_reviewer_interaction_logging():
    from scripts.evaluate_mixed_maritime import _FixtureGateway
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.llm_client import LLMClient

    gateway = _FixtureGateway()
    client = LLMClient(ConfigLoader.load(), gateway=gateway)

    assert client.review("system", "user", snapshot_id="review-fixture") == "fixture review"
    assert gateway.call_log[-1]["role"] == "reviewer"


def test_fixture_gateway_expands_selection_until_validator_accepts():
    from scripts.evaluate_mixed_maritime import _FixtureGateway

    gateway = _FixtureGateway()
    snapshot = {
        "snapshot_id": "fixture-snapshot",
        "information_version": 0,
        "candidates": [
            {"task_id": "S1", "kind": "search", "priority": "high"},
            {"task_id": "S2", "kind": "search", "priority": "medium"},
        ],
        "feasible_edges": [
            {"task_id": "S1", "uav_options": [{"uav_id": "U1"}]},
            {"task_id": "S2", "uav_options": [{"uav_id": "U2"}]},
        ],
    }
    payloads = []

    def validate(payload):
        payloads.append(payload)
        return (
            ("underutilized_feasible_work:S2",)
            if payload["selected_task_ids"] != ["S1", "S2"]
            else ()
        )

    result = gateway.request_json(
        role="decision_maker",
        snapshot_id="fixture-snapshot",
        user_payload={"snapshot": snapshot},
        validate=validate,
    )

    assert result.success
    assert result.payload["selected_task_ids"] == ["S1", "S2"]
    assert payloads
    assert payloads[-1]["selected_task_ids"] == ["S1", "S2"]


def test_alignment_report_aggregates_raw_planning_latency_samples(monkeypatch, tmp_path):
    import scripts.evaluate_mixed_maritime as evaluator

    def fake_episode(config, *, scenario, seed, repeat, steps, transport):
        return {
            "scenario": scenario,
            "seed": seed,
            "repeat": repeat,
            "status": "finished",
            "latency": {"count": 2, "p50": 1.25, "p95": 2.285, "max": 2.4},
            "latency_samples": (0.1, 2.4),
            "non_fault_latency_samples": (0.1,),
            "outcome": {"metric_denominators": {}},
            "audit": {
                "boundary_violations": 0,
                "candidate_uncovered_without_reason": 0,
                "trace_breaks": 0,
                "cross_version_decisions": 0,
            },
        }

    monkeypatch.setattr(evaluator, "_run_batch_episode", fake_episode)
    output = tmp_path / "latency.json"
    report = evaluator._batch_report(
        type("Args", (), {
            "config": Path("configs"),
            "scenario": "mixed-ais",
            "seed": 101,
            "seeds": (101,),
            "repeat": 1,
            "steps": 1,
            "transport": "fixture",
            "output": output,
        })()
    )

    latency = report["planning_latency_seconds"]
    assert latency["count"] == 2
    assert latency["p50"] == pytest.approx(1.25)
    assert latency["p95"] == pytest.approx(2.285)
    assert latency["max"] == pytest.approx(2.4)
    assert latency["non_fault_samples_over_2s"] == 0


def test_alignment_report_prefers_canonical_metric_denominators(monkeypatch, tmp_path):
    import scripts.evaluate_mixed_maritime as evaluator

    def fake_episode(config, *, scenario, seed, repeat, steps, transport):
        return {
            "scenario": scenario,
            "seed": seed,
            "repeat": repeat,
            "status": "finished",
            "latency": {"count": 0, "p50": None, "p95": None, "max": None},
            "latency_samples": (),
            "non_fault_latency_samples": (),
            "outcome": {"metric_denominators": {
                "discovery_tracking_numerator": 1,
                "discovery_tracking_denominator": 2,
                "handoff_success_numerator": 2,
                "handoff_success_denominator": 4,
                "handoff": 99,
                "continuous_observation_numerator_min": 3,
                "continuous_observation_denominator_min": 6,
                "continuous_observation_min": 99,
            }},
            "audit": {
                "boundary_violations": 0,
                "candidate_uncovered_without_reason": 0,
                "trace_breaks": 0,
                "cross_version_decisions": 0,
            },
        }

    monkeypatch.setattr(evaluator, "_run_batch_episode", fake_episode)
    report = evaluator._batch_report(type("Args", (), {
        "config": Path("configs"),
        "scenario": "mixed-ais",
        "seed": 101,
        "seeds": (101,),
        "repeat": 1,
        "steps": 1,
        "transport": "fixture",
        "output": tmp_path / "canonical.json",
    })())

    assert report["metrics"]["handoff_success_rate"]["denominator"] == 4
    assert report["metrics"]["continuous_observation_rate"]["denominator"] == 6

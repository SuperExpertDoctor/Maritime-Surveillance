import json

import pytest

from src.mission.episode_logger import EpisodeLogger, SensitiveLogError


def test_episode_logger_writes_separated_streams_and_finishes_atomically(tmp_path):
    logger = EpisodeLogger(tmp_path)
    episode_id = logger.start({"episode_id": "episode-fixture-01", "is_fixture": True})

    assert episode_id == "episode-fixture-01"
    logger.append("blue", "observations", {"sim_time_min": 1.0, "memory_version": "baseline"})
    logger.append("evaluation", "truth", {"sim_time_min": 1.0, "vessels": []})
    logger.append("intents", "intents", {"command_id": "cmd-1"})

    logger.finish("completed")

    manifest = json.loads((tmp_path / episode_id / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["record_counts"] == {
        "blue/observations": 1,
        "evaluation/truth": 1,
        "intents/intents": 1,
    }
    assert json.loads(
        (tmp_path / episode_id / "blue" / "observations.jsonl").read_text()
    )["memory_version"] == "baseline"

    with pytest.raises(RuntimeError):
        logger.append("blue", "decisions", {"later": True})


def test_episode_logger_rejects_unknown_streams_and_sensitive_payloads(tmp_path):
    logger = EpisodeLogger(tmp_path)
    logger.start({"episode_id": "episode-fixture-02"})

    with pytest.raises(ValueError, match="unsupported log stream"):
        logger.append("blue", "truth", {})
    with pytest.raises(SensitiveLogError):
        logger.append("blue", "observations", {"Authorization": "Bearer secret"})
    with pytest.raises(SensitiveLogError):
        logger.append("blue", "observations", {"environment_vessel_class": "type_ii"})

    assert list((tmp_path / "episode-fixture-02").rglob("*.jsonl")) == []


def test_episode_logger_never_reuses_an_existing_episode_directory(tmp_path):
    first = EpisodeLogger(tmp_path)
    first.start({"episode_id": "episode-fixed"})

    second = EpisodeLogger(tmp_path)
    with pytest.raises(FileExistsError):
        second.start({"episode_id": "episode-fixed"})


def test_episode_logger_writes_trace_and_handoff_ledgers(tmp_path):
    logger = EpisodeLogger(tmp_path)
    logger.start({"episode_id": "episode-ledger"})

    logger.append_trace({
        "observation_id": "obs-1",
        "evidence_id": "ev-1",
        "information_version": 3,
        "task_id": "task-1",
        "decision_id": "decision-1",
        "assignment_id": "assignment-1",
    })
    logger.append_handoff({
        "interruption_id": "interrupt-1",
        "status": "success",
        "assignment_id": "assignment-1",
    })

    logger.finish("completed")

    manifest = logger.read_manifest()
    assert manifest["record_counts"]["blue/decisions"] == 1
    assert manifest["record_counts"]["evaluation/handoffs"] == 1
    assert "assignment-1" in (
        tmp_path / "episode-ledger" / "blue" / "decisions.jsonl"
    ).read_text()


def test_episode_logger_rejects_incomplete_success_trace(tmp_path):
    logger = EpisodeLogger(tmp_path)
    logger.start({"episode_id": "episode-ledger-invalid"})

    with pytest.raises(ValueError, match="trace requires"):
        logger.append_trace({
            "observation_id": "obs-1",
            "evidence_id": "ev-1",
            "information_version": 1,
            "task_id": "task-1",
            "decision_id": "decision-1",
        })

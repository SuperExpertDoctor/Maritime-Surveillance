from dataclasses import replace


from src.mission.outcome_evaluator import EpisodeOutcome
from src.mission.strategy_memory import (
    StrategyMemory,
    StrategyMemoryStore,
    evaluate_paired_outcomes,
)


def _outcome(
    episode_id,
    *,
    score=0.7,
    false_civilian=0.0,
    coverage=0.7,
    intent=0.8,
    tracking=0.6,
    accuracy=0.9,
    terminal_coverage=1.0,
    valid=True,
):
    return EpisodeOutcome(
        episode_id,
        valid,
        (),
        coverage,
        intent,
        tracking,
        accuracy,
        false_civilian,
        1.0,
        10.0,
        2.0,
        1,
        score,
        1,
        1,
        0,
        terminal_coverage,
    )


def _memory():
    return StrategyMemory(
        "M0001",
        1,
        "candidate",
        {"has_active_intent": "yes"},
        "Prioritize eligible probe work before low-value coverage.",
        ("episode-1", "episode-2", "episode-3"),
        {"mean_score": 0.7},
        None,
        "2026-01-01T00:00:00+00:00",
    )


def test_false_civilian_regression_rejects_even_when_score_improves():
    baseline = (_outcome("baseline-1"), _outcome("baseline-2"))
    candidate = (
        _outcome("candidate-1", score=0.85, false_civilian=0.01),
        _outcome("candidate-2", score=0.85, false_civilian=0.01),
    )

    report = evaluate_paired_outcomes(baseline, candidate, {"phase": "validation"})

    assert not report.passed
    assert "false_civilian_regression" in report.reasons


def test_holdout_score_regression_rejects():
    baseline = (_outcome("baseline-1"), _outcome("baseline-2"))
    candidate = (
        _outcome("candidate-1", score=0.65),
        _outcome("candidate-2", score=0.65),
    )

    report = evaluate_paired_outcomes(baseline, candidate, {"phase": "holdout"})

    assert not report.passed
    assert "score_regression" in report.reasons


def test_invalid_pairs_and_config_mismatch_cannot_validate():
    baseline = (_outcome("baseline-1", valid=False),)
    candidate = (_outcome("candidate-1"),)
    report = evaluate_paired_outcomes(
        baseline,
        candidate,
        {"phase": "validation", "baseline_config_hash": "a", "candidate_config_hash": "b"},
    )

    assert not report.passed
    assert "no_valid_pair" in report.reasons
    assert "config_mismatch" in report.reasons


def test_fixture_pairs_and_unknown_accuracy_are_not_evidence():
    fixture_report = evaluate_paired_outcomes(
        (_outcome("fixture-baseline"),),
        (_outcome("fixture-candidate", score=0.9),),
        {"phase": "validation"},
    )
    unknown_report = evaluate_paired_outcomes(
        (_outcome("baseline-1", accuracy=0.8),),
        (_outcome("candidate-1", score=0.9, accuracy=None, terminal_coverage=0.0),),
        {"phase": "validation"},
    )

    assert "fixture_pair" in fixture_report.reasons
    assert "classification_metric_missing" in unknown_report.reasons


def test_missing_terminal_coverage_cannot_support_activation():
    baseline = (_outcome("baseline-1"), _outcome("baseline-2"))
    candidate = (
        _outcome("candidate-1", score=0.85, terminal_coverage=None),
        _outcome("candidate-2", score=0.85, terminal_coverage=None),
    )

    report = evaluate_paired_outcomes(
        baseline,
        candidate,
        {"phase": "validation"},
    )

    assert not report.passed
    assert "terminal_coverage_missing" in report.reasons


def test_pairs_without_terminal_classification_cannot_support_activation():
    baseline = (
        _outcome("baseline-1", accuracy=None, terminal_coverage=None),
        _outcome("baseline-2", accuracy=None, terminal_coverage=None),
    )
    candidate = (
        _outcome("candidate-1", score=0.85, accuracy=None, terminal_coverage=None),
        _outcome("candidate-2", score=0.85, accuracy=None, terminal_coverage=None),
    )

    report = evaluate_paired_outcomes(
        baseline,
        candidate,
        {"phase": "validation"},
    )

    assert not report.passed
    assert "classification_metric_missing" in report.reasons
    assert "terminal_coverage_missing" in report.reasons


def test_successful_report_can_activate_and_rollback_only_at_a_version_boundary(tmp_path):
    store = StrategyMemoryStore(tmp_path)
    store.save_candidate(_memory())
    report = evaluate_paired_outcomes(
        (_outcome("baseline-1"), _outcome("baseline-2")),
        (
            replace(
                _outcome("candidate-1", score=0.85, coverage=0.85, intent=0.85, tracking=0.62),
                civilian_probe_uav_min=0.5,
                mean_probe_wait_min=1.0,
            ),
            replace(
                _outcome("candidate-2", score=0.85, coverage=0.85, intent=0.85, tracking=0.62),
                civilian_probe_uav_min=0.5,
                mean_probe_wait_min=1.0,
            ),
        ),
        {
            "phase": "validation", "memory_id": "M0001",
            "baseline_config_hash": "same", "candidate_config_hash": "same",
        },
    )
    report = replace(
        report,
        holdout_episode_pairs=(("baseline-holdout", "candidate-holdout"),),
    )
    store.save_validation_report(report)

    assert store.activate("M0001", report.report_id) == "1"
    assert store.active_version() == "1"
    assert store.rollback("baseline") == "baseline"
    assert store.active_version() == "baseline"

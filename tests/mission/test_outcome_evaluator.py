import pytest

from src.mission.contracts import (
    Assessment,
    ContactSnapshot,
    IntentStatus,
    TaskRecord,
)
from src.mission.outcome_evaluator import (
    EvaluationTick,
    OutcomeEvaluator,
    VesselTruthSample,
)


def _contact(contact_id, *, identity="unknown", state="pending", assessment=None):
    return ContactSnapshot(
        contact_id,
        1,
        state,
        identity,
        None,
        0.0,
        10.0,
        (10.0, 10.0),
        (0.0, 0.0),
        0.5,
        None,
        assessment.probe_id if assessment else None,
        assessment,
        None,
        0.0,
        (),
    )


def _assessment(contact_id, identity):
    return Assessment(
        f"A-{contact_id}",
        contact_id,
        f"P-{contact_id}",
        2,
        5.0,
        identity,
        0.9,
        (f"sample-{contact_id}",),
        ("observed motion",),
        (),
        f"call-{contact_id}",
    )


def _tick(*, time, dt, vessels, operations=(), tasks=(), contacts=(), links=(), statuses=()):
    return EvaluationTick(
        time,
        dt,
        tuple(vessels),
        tuple(operations),
        tuple(tasks),
        tuple(contacts),
        tuple(links),
        tuple(statuses),
        0.4,
    )


def test_tracking_uses_actual_dt_and_classification_does_not_repair_a_wrong_alias():
    evaluator = OutcomeEvaluator("episode-eval-01")
    target_1 = VesselTruthSample("V1", "target", (10.0, 10.0), False, False, "normal")
    target_2 = VesselTruthSample("V2", "target", (12.0, 10.0), False, False, "normal")
    assessed_target = _assessment("C1", "target")

    evaluator.observe(_tick(
        time=5.0,
        dt=5.0,
        vessels=(target_1, target_2),
        operations=(("U1", "tracking"),),
        contacts=(_contact("C1", identity="target", state="tracking", assessment=assessed_target),),
        links=(("U1", "C1", "V1"),),
    ))
    evaluator.observe(_tick(
        time=10.0,
        dt=5.0,
        vessels=(target_1, target_2),
        operations=(("U1", "holding"),),
        contacts=(_contact("C1", identity="target", state="tracking", assessment=assessed_target),),
    ))

    outcome = evaluator.finalize()

    assert outcome.target_tracking_ratio == pytest.approx(0.25)
    assert outcome.classification_accuracy == pytest.approx(1.0)
    assert outcome.observed_vessels == 1
    assert outcome.terminal_classification_coverage == pytest.approx(1.0)


def test_unknown_contacts_are_reported_and_never_count_as_correct():
    evaluator = OutcomeEvaluator("episode-eval-02")
    evaluator.observe(_tick(
        time=10.0,
        dt=10.0,
        vessels=(VesselTruthSample("V1", "target", (5.0, 5.0), False, False, "normal"),),
        operations=(("U1", "searching"),),
        contacts=(_contact("C1"),),
    ))

    outcome = evaluator.finalize()

    assert outcome.classification_accuracy is None
    assert outcome.terminal_classified_vessels == 0
    assert outcome.unknown_contacts == 1
    assert outcome.observed_vessels == 0
    assert outcome.terminal_classification_coverage is None


def test_probe_cost_and_intent_satisfaction_only_count_executed_time():
    evaluator = OutcomeEvaluator("episode-eval-03")
    task = TaskRecord(
        "probe-C1",
        "probe",
        "executing",
        None,
        "C1",
        (),
        "U1",
        "call-1",
        0.0,
        2.0,
        None,
        None,
    )
    status = IntentStatus("I1", 1, 10.0, 4, 4, 0, 4, 1.0, 1.0, 0.0, (), None)
    evaluator.observe(_tick(
        time=10.0,
        dt=10.0,
        vessels=(VesselTruthSample("V1", "civilian", (5.0, 5.0), False, True, "normal"),),
        operations=(("U1", "searching"),),
        tasks=(task,),
        contacts=(_contact("C1", identity="civilian", state="cleared"),),
        statuses=(status,),
    ))

    outcome = evaluator.finalize()

    assert outcome.civilian_probe_uav_min == pytest.approx(8.0)
    assert outcome.total_uav_active_min == pytest.approx(10.0)
    assert outcome.mean_probe_wait_min == pytest.approx(2.0)
    assert outcome.intent_satisfaction_ratio == pytest.approx(1.0)


def test_unknown_classification_counts_as_error_in_balanced_accuracy():
    evaluator = OutcomeEvaluator("episode-eval-metrics-01")
    evaluator.add_classification(truth="research", predicted="unknown", eligible=True)
    evaluator.add_classification(truth="civilian", predicted="civilian", eligible=True)

    summary = evaluator.summary()

    assert summary["research_recall"] == 0.0
    assert summary["civilian_recall"] == 1.0
    assert summary["balanced_accuracy"] == pytest.approx(0.5)
    assert summary["classification_denominators"] == {"civilian": 1, "research": 1}


def test_zero_denominator_is_na_not_success():
    evaluator = OutcomeEvaluator("episode-eval-metrics-02")

    summary = evaluator.summary()

    assert summary["handoff_success_rate"] is None
    assert summary["continuous_observation_rate"] is None
    assert summary["na_reasons"]["handoff_success_rate"] == "no_eligible_handoff_interruptions"


def test_handoff_denominator_counts_one_interruption_and_excludes_no_successor():
    evaluator = OutcomeEvaluator("episode-eval-metrics-03")
    evaluator.register_handoff("interrupt-1", at_min=10.0, successor_uav_ids=("U2",))
    evaluator.register_handoff("interrupt-1", at_min=10.0, successor_uav_ids=("U2",))
    evaluator.record_handoff_assignment("interrupt-1", at_min=12.0)
    evaluator.record_handoff_lock("interrupt-1", at_min=18.0)
    evaluator.register_handoff("interrupt-2", at_min=20.0, successor_uav_ids=())

    summary = evaluator.summary()

    assert summary["handoff_success_rate"] == 1.0
    assert summary["handoff_denominator"] == 1
    assert summary["handoff_excluded_no_successor"] == 1


def test_survey_observation_rate_uses_union_of_hidden_intervals():
    evaluator = OutcomeEvaluator("episode-eval-metrics-04")
    evaluator.add_survey_interval("R1", 0.0, 10.0)
    evaluator.add_survey_interval("R1", 8.0, 20.0)
    evaluator.add_eo_lock_interval("R1", 5.0, 15.0)

    summary = evaluator.summary()

    assert summary["continuous_observation_denominator_min"] == pytest.approx(20.0)
    assert summary["continuous_observation_numerator_min"] == pytest.approx(10.0)
    assert summary["continuous_observation_rate"] == pytest.approx(0.5)


def test_decision_latency_keeps_failed_retry_duration_and_percentiles():
    evaluator = OutcomeEvaluator("episode-eval-metrics-05")
    evaluator.record_decision_latency(
        snapshot_frozen_wall=10.0,
        decision_finished_wall=11.5,
        llm_seconds=0.8,
        validation_seconds=0.2,
        matching_seconds=0.5,
        success=False,
        failure_reason="timeout",
    )

    summary = evaluator.summary()

    assert summary["decision_latency_seconds"]["count"] == 1
    assert summary["decision_latency_seconds"]["p50"] == pytest.approx(1.5)
    assert summary["decision_latency_seconds"]["failures"] == 1
    assert summary["decision_latency_seconds"]["max"] == pytest.approx(1.5)

from dataclasses import replace

import pytest

from src.schedule.config_loader import ConfigLoader
from tests.mission.test_trajectory_features import api, sample, session, snapshot


def advance(probe, samples=(), now=None, config=None):
    return api().advance_probe(probe, tuple(samples),
                               max((s.observed_at_min for s in samples), default=0.)
                               if now is None else now,
                               config or ConfigLoader.load().mission.contact)


def baseline_finished(start=0):
    samples = tuple(sample(t) for t in range(start, start + 5))
    return advance(session(), samples), samples


def test_incremental_baseline_then_near_uses_distinct_phase_evidence():
    original = session()
    probe = original
    baseline = tuple(sample(t) for t in range(5))
    for s in baseline:
        probe = advance(probe, (s,))
    assert original.baseline_started_at_min is None
    assert probe.phase == "closing" and probe.baseline_started_at_min == 0.
    assert probe.baseline_sample_ids == tuple(s.sample_id for s in baseline)
    assert probe.near_sample_ids == () and probe.close_exposure_min == 0.
    near = tuple(sample(t, distance=1.2) for t in range(5, 11))
    for i, s in enumerate(near):
        probe = advance(probe, (s,))
        assert probe.close_exposure_min == i
        assert probe.phase == ("awaiting_assessment" if i == 5 else "near")
    result = api().build_features(snapshot(baseline + near), probe, 10., ConfigLoader.load().mission.contact)
    assert result.sufficient_evidence and result.near_duration_min == 5.
    assert not set(probe.baseline_sample_ids) & set(probe.near_sample_ids)


def test_batch_and_incremental_progress_are_equivalent_and_replay_is_idempotent():
    samples = tuple(sample(t, distance=1.8 if t < 5 else 1.2) for t in range(11))
    batch = advance(session(), tuple(reversed(samples)))
    incremental = session()
    for s in samples:
        incremental = advance(incremental, (s,))
    assert batch == incremental
    assert advance(batch, samples, now=10.) == batch


def test_long_transit_does_not_consume_evidence_window():
    probe = advance(session(), (sample(60, distance=5.),), now=60.)
    assert probe.phase == "baseline" and probe.baseline_started_at_min is None
    probe = advance(probe, tuple(sample(t) for t in range(90, 95)))
    assert probe.phase == "closing" and probe.baseline_started_at_min == 90.
    assert advance(probe, now=109.).completed_reason is None
    assert advance(probe, now=110.).completed_reason == "probe_timeout"


def test_approach_timeout_is_measured_from_task_start():
    probe = session(started_at_min=7., phase_started_at_min=7.)
    assert advance(probe, now=126.9).completed_reason is None
    result = advance(probe, (sample(127),), now=127.)
    assert result.phase == "finished" and result.completed_reason == "approach_timeout"
    assert result.baseline_started_at_min is None
    assert advance(result, (sample(128),), now=128.) == result


def test_approach_deadline_does_not_end_probe_that_already_reached_baseline():
    probe, _ = baseline_finished(115)
    assert advance(probe, now=121.).completed_reason is None
    assert advance(probe, now=135.).completed_reason == "probe_timeout"


def test_phase_changes_and_measurement_resets_cannot_extend_first_baseline_deadline():
    probe = advance(session(), (sample(1), sample(2)))
    probe = advance(probe, (sample(10),))
    assert probe.baseline_started_at_min == 1.
    assert probe.baseline_sample_ids == (sample(10).sample_id,)
    probe = replace(probe, phase="baseline", phase_started_at_min=18.,
                    baseline_sample_ids=())
    probe = advance(probe, (sample(18), sample(19)))
    assert probe.baseline_started_at_min == 1.
    assert advance(probe, now=21.).completed_reason == "probe_timeout"


@pytest.mark.parametrize("phase", ["baseline", "near"])
def test_long_gap_resets_phase_local_duration_and_count_even_without_new_samples(phase):
    if phase == "baseline":
        probe = advance(session(), (sample(0), sample(1)))
    else:
        probe, _ = baseline_finished()
        probe = advance(probe, (sample(5, distance=1.2), sample(6, distance=1.2)))
    previous = probe.baseline_sample_ids
    now = 4 if phase == "baseline" else 9
    result = advance(probe, now=now)
    assert result.close_exposure_min == 0.
    assert getattr(result, f"{phase}_sample_ids") == ()
    if phase == "near":
        assert result.baseline_sample_ids == previous


def test_leaving_and_reentering_near_does_not_count_time_outside():
    probe, baseline = baseline_finished()
    first = tuple(sample(t, distance=1.2) for t in (5, 6, 7))
    probe = advance(probe, first)
    assert probe.close_exposure_min == 2.
    outside = sample(8, distance=1.6)
    probe = advance(probe, (outside,))
    assert probe.phase == "closing" and probe.close_exposure_min == 0.
    assert probe.near_sample_ids == ()
    second = (sample(9, distance=1.2), sample(10, distance=1.2))
    probe = advance(probe, second)
    assert probe.phase == "near" and probe.close_exposure_min == 1.
    result = api().build_features(snapshot(baseline + first + (outside,) + second), probe, 10.,
                                  ConfigLoader.load().mission.contact)
    assert result.near_duration_min == 1. and not result.sufficient_evidence


@pytest.mark.parametrize("distance,enters", [(1.35, True), (1.35001, False), (None, False)])
def test_near_requires_measured_range_with_tolerance(distance, enters):
    probe, _ = baseline_finished()
    probe = advance(probe, (sample(5, distance=distance),))
    assert (probe.phase == "near") is enters
    assert probe.close_exposure_min == 0.


@pytest.mark.parametrize("changes", [{"source": "ais"}, {"source_id": "U2"},
                                    {"contact_id": "C2"}, {"observer_position_cells": None}])
def test_unrelated_or_ais_measurements_cannot_advance_probe(changes):
    probe, _ = baseline_finished()
    samples = tuple(sample(t, distance=1.2, **changes) for t in range(5, 11))
    result = advance(probe, samples)
    assert result.phase == "closing" and result.close_exposure_min == 0.
    assert result.near_sample_ids == ()


def test_same_source_intervals_only_and_duplicate_times_do_not_satisfy_counts():
    config = replace(ConfigLoader.load().mission.contact, min_valid_samples_per_phase=4,
                     baseline_duration_min=1.)
    duplicates = tuple(sample(0, sample_id=f"dup{i}") for i in range(5))
    probe = advance(session(), duplicates + (sample(1),), config=config)
    assert probe.phase == "baseline" and len(probe.baseline_sample_ids) == 2
    alternating = (sample(0), sample(1, source="sar"))
    probe = advance(session(), alternating, config=config)
    assert probe.phase == "baseline"
    result = api().build_features(snapshot(alternating), probe, 1., config)
    assert result.baseline_duration_min == 0.


def test_external_phase_transition_discards_previous_phase_interval():
    probe = advance(session(), (sample(0), sample(1)))
    probe = replace(probe, phase="near", phase_started_at_min=2.)
    probe = advance(probe, (sample(2, distance=1.2), sample(3, distance=1.2)))
    assert probe.close_exposure_min == 1.
    assert probe.near_sample_ids == (sample(2).sample_id, sample(3).sample_id)


def test_future_and_delayed_samples_cannot_retroactively_advance_phase():
    probe = advance(session(), (sample(2), sample(10)), now=2.)
    assert probe.baseline_started_at_min == 2.
    result = advance(probe, (sample(0), sample(1)), now=2.)
    assert result == probe

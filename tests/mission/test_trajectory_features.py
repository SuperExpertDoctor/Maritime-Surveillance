from dataclasses import FrozenInstanceError, fields, replace
import importlib
import math

import pytest

from src.mission import contracts
from src.schedule.config_loader import ConfigLoader


def api():
    assert importlib.util.find_spec("src.mission.trajectory_features") is not None, (
        "Task 7 trajectory features module is missing")
    return importlib.import_module("src.mission.trajectory_features")


def sample(t, *, distance=1.8, source="eo", source_id="U1", heading=0,
           speed=.1, **changes):
    velocity = None if speed is None else (
        speed * math.cos(math.radians(heading)), speed * math.sin(math.radians(heading)))
    values = dict(sample_id=f"{source}:{source_id}:{t}", contact_id="C1",
                  observed_at_min=float(t), source=source, source_id=source_id,
                  position_cells=(float(t) * .1, 0.), velocity_cells_min=velocity,
                  position_uncertainty_cells=.05,
                  observer_position_cells=None if distance is None else (float(t) * .1, distance),
                  measured_range_cells=distance, navigation_context="open_water")
    values.update(changes)
    return contracts.ObservationSample(**values)


def session(**changes):
    values = dict(probe_id="P1", contact_id="C1", uav_id="U1", phase="baseline",
                  started_at_min=0., baseline_started_at_min=None, phase_started_at_min=0.,
                  baseline_sample_ids=(), near_sample_ids=(), close_exposure_min=0.,
                  completed_reason=None)
    values.update(changes)
    return contracts.ProbeSession(**values)


def snapshot(samples, **changes):
    values = dict(contact_id="C1", revision=42, state="observing", identity="unknown",
                  ais_mmsi=None, first_seen_min=0., last_seen_min=20.,
                  estimated_position_cells=(0., 0.), estimated_velocity_cells_min=None,
                  uncertainty_cells=.05, assigned_uav_id="U1", active_probe_id="P1",
                  last_assessment=None, cleared_at_min=None, next_probe_not_before_min=0.,
                  samples=tuple(samples))
    values.update(changes)
    return contracts.ContactSnapshot(**values)


def features(samples, *, baseline=None, near=(), now=None, probe=None, config=None):
    samples = tuple(samples)
    probe = probe or session(baseline_started_at_min=0.,
                             baseline_sample_ids=tuple(s.sample_id for s in (
                                 samples if baseline is None else baseline)),
                             near_sample_ids=tuple(s.sample_id for s in near))
    return api().build_features(snapshot(samples), probe,
                                max((s.observed_at_min for s in samples), default=0.)
                                if now is None else now,
                                config or ConfigLoader.load().mission.contact)


def test_contract_is_frozen_and_has_exact_design_fields_and_nullable_metrics():
    result = features(())
    assert isinstance(result, contracts.TrajectoryFeatures)
    assert tuple(f.name for f in fields(result)) == (
        "contact_id", "history_revision", "probe_id", "baseline_sample_ids", "near_sample_ids",
        "baseline_duration_min", "near_duration_min", "baseline_speed_mean_kn",
        "near_speed_mean_kn", "baseline_abs_turn_rate_deg_min", "near_abs_turn_rate_deg_min",
        "heading_change_deg", "min_observed_uav_distance_cells", "close_exposure_min",
        "near_land_fraction", "max_observation_gap_min", "sufficient_evidence", "confounders")
    for name in ("baseline_speed_mean_kn", "near_speed_mean_kn",
                 "baseline_abs_turn_rate_deg_min", "near_abs_turn_rate_deg_min",
                 "heading_change_deg", "min_observed_uav_distance_cells"):
        assert getattr(result, name) is None
    assert result.history_revision == 42 and not result.sufficient_evidence
    with pytest.raises(FrozenInstanceError):
        result.sufficient_evidence = True


def test_module_exports_exactly_the_three_task_apis():
    assert api().__all__ == ("build_features", "advance_probe", "select_keypoints")


def test_heading_wrap_and_rate_use_short_arc():
    assert api().wrap_delta_deg(1., 359.) == 2.
    result = features((sample(0, heading=359), sample(1, heading=1)))
    assert result.heading_change_deg == pytest.approx(2.)
    assert result.baseline_abs_turn_rate_deg_min == pytest.approx(2.)


def test_duplicate_timestamps_are_not_independent_evidence_or_motion():
    samples = (sample(0), sample(0, sample_id="duplicate", position_cells=(999., 0.)), sample(1))
    result = features(samples)
    assert result.baseline_duration_min == 1.
    assert len(result.baseline_sample_ids) == 2
    assert result.baseline_abs_turn_rate_deg_min == 0.


def test_position_differences_and_knots_use_configured_grid_scale():
    config = ConfigLoader.load().mission.contact
    assert hasattr(config, "cell_size_km"), "ContactConfig lacks the grid scale required for knots"
    config = replace(config, cell_size_km=2.)
    samples = tuple(sample(t, speed=None, position_cells=(.1 * t, 0.)) for t in range(4))
    result = features(samples, config=config)
    assert result.baseline_speed_mean_kn == pytest.approx(.1 * 2 * 60 / 1.852)


def test_three_point_median_removes_interior_speed_and_heading_spikes():
    samples = tuple(sample(t, speed=speed, heading=heading) for t, speed, heading in (
        (0, .1, 359), (1, .1, 359), (2, 10., 90), (3, .1, 1), (4, .1, 1)))
    result = features(samples)
    scale = ConfigLoader.load().grid.cell_size_km
    assert result.baseline_speed_mean_kn == pytest.approx(.1 * scale * 60 / 1.852)
    assert result.baseline_abs_turn_rate_deg_min == pytest.approx(.5)
    assert result.heading_change_deg == pytest.approx(2.)


@pytest.mark.parametrize("other", [{"source": "ais"}, {"source_id": "U2"}])
def test_source_offset_never_becomes_motion_or_turn(other):
    samples = (sample(0, speed=None, position_cells=(0., 0.)),
               sample(1, speed=None, position_cells=(100., 0.), **other))
    result = features(samples)
    assert result.baseline_speed_mean_kn is None
    assert result.baseline_abs_turn_rate_deg_min is None
    assert result.heading_change_deg is None
    assert result.baseline_duration_min == 0.


def test_interleaved_streams_derive_motion_separately_without_double_exposure():
    samples = tuple(s for t in range(5) for s in (
        sample(t, speed=None),
        sample(t, source="ais", source_id="M1", distance=None, speed=None,
               position_cells=(100. + .1 * t, 0.))))
    result = features(samples)
    assert result.baseline_duration_min == 4.
    assert result.baseline_speed_mean_kn == pytest.approx(.1 * ConfigLoader.load().grid.cell_size_km * 60 / 1.852)


def test_large_gap_breaks_derived_motion_and_reports_observation_gap():
    result = features((sample(0, speed=None), sample(5, speed=None)))
    assert result.baseline_speed_mean_kn is None
    assert result.heading_change_deg is None
    assert result.baseline_duration_min == 0.
    assert result.max_observation_gap_min == 5.
    assert "observation_gap" in result.confounders


def test_stationary_velocity_is_zero_speed_but_has_no_heading():
    result = features((sample(0, speed=0.), sample(1, speed=0.)))
    assert result.baseline_speed_mean_kn == 0.
    assert result.heading_change_deg is None
    assert result.baseline_abs_turn_rate_deg_min is None


def complete_history(**near_changes):
    baseline = tuple(sample(t) for t in range(5))
    near = tuple(sample(t, distance=1.2, **near_changes) for t in range(6, 12))
    return baseline, near


def test_sufficiency_requires_both_durations_sample_counts_and_own_approach():
    baseline, near = complete_history()
    assert features(baseline + near, baseline=baseline, near=near).sufficient_evidence
    assert not features(baseline + near, baseline=baseline[:3], near=near).sufficient_evidence
    assert not features(baseline + near, baseline=baseline, near=near[:3]).sufficient_evidence
    # Four widely spaced observations meet the count, but provide no duration.
    sparse = tuple(sample(t, distance=1.2) for t in (6, 9, 12, 15))
    assert not features(baseline + sparse, baseline=baseline, near=sparse).sufficient_evidence


@pytest.mark.parametrize("changes", [
    {"source": "ais"}, {"source_id": "U2"}, {"observer_position_cells": None},
    {"measured_range_cells": None}, {"measured_range_cells": 2.},
])
def test_ais_other_observer_or_missing_close_measurement_cannot_supply_near_evidence(changes):
    baseline, near = complete_history(**changes)
    assert not features(baseline + near, baseline=baseline, near=near).sufficient_evidence


def test_near_land_and_other_uav_approach_are_observed_confounders_not_identity():
    baseline, near = complete_history(navigation_context="near_land")
    other = sample(-1, source_id="U2", distance=1.)
    result = features((other,) + baseline + near, baseline=baseline, near=near)
    assert result.near_land_fraction == pytest.approx(6 / 12)
    assert {"near_land", "baseline_confounded"} <= set(result.confounders)
    assert not result.sufficient_evidence
    assert not hasattr(result, "identity")


def test_evidence_ids_are_resolved_against_current_revision_and_future_is_excluded():
    samples = (sample(0), sample(1), sample(5))
    probe = session(baseline_started_at_min=0.,
                    baseline_sample_ids=("missing", *(s.sample_id for s in samples)))
    result = features(samples, now=1., probe=probe)
    assert result.baseline_sample_ids == (samples[0].sample_id, samples[1].sample_id)
    assert result.history_revision == snapshot(samples).revision
    assert "missing_evidence" in result.confounders


def test_keypoints_preserve_events_and_neighbors_in_stable_order():
    samples = tuple(sample(t, distance=(4. if t < 8 else 4. - .1 * (t - 7))
                           if t != 20 else .2, heading=0. if t < 15 else 60.)
                    for t in range(30))
    result = api().select_keypoints(tuple(reversed(samples)), 99)
    indices = {int(s.observed_at_min) for s in result}
    assert {0, 29, 6, 7, 8, 19, 20, 21, 14, 15, 16} <= indices
    assert len(result) == 12
    assert result == api().select_keypoints(samples, 12)
    assert result == tuple(sorted(result, key=lambda s: (s.observed_at_min, s.sample_id)))
    assert all(s is samples[int(s.observed_at_min)] for s in result)


def test_keypoint_max_turn_uses_three_point_median_smoothing():
    samples = tuple(sample(t, distance=None, heading=heading) for t, heading in enumerate(
        (0., 0., 90., 0., 0., 0., 30., 30., 30., 30.)
    ))

    result = api().select_keypoints(samples, 5)

    assert tuple(s.observed_at_min for s in result) == (0., 5., 6., 7., 9.)


@pytest.mark.parametrize("limit", [0, 1, 2, 5, 12, 30])
def test_keypoint_limits_uniform_fill_and_duplicate_ids(limit):
    samples = tuple(sample(t, distance=None) for t in range(30))
    result = api().select_keypoints(samples + samples, limit)
    assert len(result) == min(limit, 12)
    assert len({s.sample_id for s in result}) == len(result)
    if limit >= 2:
        assert result[0] == samples[0] and result[-1] == samples[-1]
    if limit == 5:
        assert tuple(s.observed_at_min for s in result) == (0., 7., 14., 21., 29.)
    assert api().select_keypoints((), limit) == ()

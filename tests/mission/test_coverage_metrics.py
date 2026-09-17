import json

import numpy as np
import pytest

from src.mission.coverage_metrics import CoverageMetrics
from tests.mission.coverage_helpers import oracle_coverage


def _metric(shape=(2, 2), *, cell_size_km=10):
    return CoverageMetrics(
        episode_id="e1",
        fixed_mask=np.ones(shape, dtype=bool),
        cell_size_km=cell_size_km,
    )


def _window(snapshot, minutes):
    return next(window for window in snapshot["windows"] if window["minutes"] == minutes)


def test_window_boundary_and_overlap():
    m = _metric()
    m.record_sar(((0, 0),), at_min=0)
    m.record_sar(((0, 1), (1, 0)), at_min=1)
    m.record_sar(((0, 1), (0, 1)), at_min=59)

    snapshot = m.snapshot(now_min=60, feasible_mask=np.ones((2, 2), bool))
    window = _window(snapshot, 60)
    assert window["covered_cells"] == 2
    assert window["coverage_pct"] == 50.0
    assert window["covered_area_km2"] == 200.0
    assert snapshot["unseen_pct"] == 25.0
    assert snapshot["overdue_seen_pct"] == 25.0
    assert _window(
        m.snapshot(now_min=119, feasible_mask=np.ones((2, 2), bool)), 60
    )["covered_cells"] == 0


@pytest.mark.parametrize("windows_min", [(15, 30, 60), (30, 60), (30, 60, 120, 240)])
def test_constructor_requires_the_authoritative_window_set(windows_min):
    with pytest.raises(ValueError, match="exactly"):
        CoverageMetrics(
            episode_id="e1",
            fixed_mask=np.ones((1, 1), bool),
            cell_size_km=1,
            windows_min=windows_min,
            primary_window_min=60,
        )


def test_constructor_rejects_cell_size_with_overflowing_coverage_area():
    with pytest.raises(ValueError, match="area"):
        CoverageMetrics(
            episode_id="e1", fixed_mask=np.ones((1, 1), bool), cell_size_km=1e200
        )


def test_extreme_but_finite_coverage_area_remains_json_safe():
    metric = CoverageMetrics(
        episode_id="e1", fixed_mask=np.ones((1, 1), bool), cell_size_km=1e154
    )
    metric.record_sar(((0, 0),), at_min=1)
    snapshot = metric.snapshot(now_min=1, feasible_mask=np.ones((1, 1), bool))

    assert np.isfinite(snapshot["fixed_searchable_area_km2"])
    assert np.isfinite(snapshot["windows"][0]["covered_area_km2"])
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("shape", [(1, 10), (10, 10), (30, 30)])
@pytest.mark.parametrize("cell_size_km", [1, 2.5, 10])
def test_windows_match_the_independent_oracle(shape, cell_size_km):
    metric = _metric(shape, cell_size_km=cell_size_km)
    cells = ((0, 0), (shape[0] - 1, shape[1] - 1), (0, 0))
    metric.record_sar(cells, at_min=0.25)
    metric.record_sar(((0, shape[1] - 1),), at_min=31.5)
    metric.record_sar(((shape[0] - 1, 0),), at_min=95.75)
    events = [
        {"time": 0.25, "source": "sar", "cells": cells},
        {"time": 31.5, "source": "sar", "cells": ((0, shape[1] - 1),)},
        {"time": 95.75, "source": "sar", "cells": ((shape[0] - 1, 0),)},
        {"time": 100, "source": "eo", "cells": ((0, 1),)},
    ]
    domain = list(np.argwhere(np.ones(shape, dtype=bool)))
    snapshot = metric.snapshot(now_min=100, feasible_mask=np.ones(shape, bool))
    for minutes in (30, 60, 120):
        expected = oracle_coverage(
            events,
            domain,
            now_min=100,
            window_min=minutes,
            cell_size_km=cell_size_km,
        )
        actual = _window(snapshot, minutes)
        assert {key: actual[key] for key in expected} == expected


def test_sar_only_counters_fixed_and_dynamic_denominators():
    fixed = np.array(((True, True), (True, False)))
    feasible = np.array(((True, False), (True, True)))
    metric = CoverageMetrics(episode_id="e1", fixed_mask=fixed, cell_size_km=3)
    metric.record_sar(((0, 0), (1, 0), (1, 1)), at_min=10)

    snapshot = metric.snapshot(now_min=10, feasible_mask=feasible)
    assert snapshot["fixed_searchable_cells"] == 3
    assert snapshot["currently_searchable_cells"] == 2
    assert snapshot["weather_blocked_cells"] == 1
    assert snapshot["ever_scanned_cells"] == 2
    assert snapshot["cumulative_pct"] == pytest.approx(200 / 3)
    window = _window(snapshot, 60)
    assert window["covered_cells"] == 2
    assert window["coverage_pct"] == pytest.approx(200 / 3)
    assert window["currently_searchable_coverage_pct"] == 100.0


def test_window_boundary_epsilon_and_non_contiguous_ticks():
    metric = _metric((1, 3))
    metric.record_sar(((0, 0),), at_min=0)
    metric.record_sar(((0, 1),), at_min=2e-9)
    assert _window(metric.snapshot(now_min=60, feasible_mask=np.ones((1, 3), bool)), 60)["covered_cells"] == 1
    metric.record_sar(((0, 2),), at_min=60 + 1e-9)
    assert _window(metric.snapshot(now_min=60 + 1e-9, feasible_mask=np.ones((1, 3), bool)), 60)["covered_cells"] == 2
    assert _window(metric.snapshot(now_min=120.5, feasible_mask=np.ones((1, 3), bool)), 60)["covered_cells"] == 0


def test_repeated_same_time_scan_is_idempotent_and_fixed_external_cells_are_ignored():
    fixed = np.array(((True, False),))
    metric = CoverageMetrics(episode_id="e1", fixed_mask=fixed, cell_size_km=1)
    metric.record_sar(((0, 0), (0, 0), (0, 1)), at_min=3.5)
    metric.record_sar(((0, 0),), at_min=3.5)

    snapshot = metric.snapshot(now_min=3.5, feasible_mask=np.ones((1, 2), bool))
    assert snapshot["ever_scanned_cells"] == 1
    assert _window(snapshot, 30)["covered_cells"] == 1


@pytest.mark.parametrize("at_min", [True, -1, float("nan"), float("inf"), -float("inf")])
def test_record_rejects_invalid_times(at_min):
    with pytest.raises(ValueError):
        _metric().record_sar(((0, 0),), at_min=at_min)


def test_record_rejects_time_rollback_and_invalid_cell_indexes():
    metric = _metric()
    metric.record_sar(((0, 0),), at_min=2)
    with pytest.raises(ValueError, match="earlier"):
        metric.record_sar(((0, 1),), at_min=1.5)
    for cells in (((True, 0),), ((0, False),), ((2, 0),), ((0, -1),), ((0,),)):
        with pytest.raises(ValueError):
            metric.record_sar(cells, at_min=2)


def test_snapshot_rejects_invalid_time_shape_and_historical_query():
    metric = _metric()
    metric.record_sar(((0, 0),), at_min=2)
    with pytest.raises(ValueError, match="earlier"):
        metric.snapshot(now_min=1.5, feasible_mask=np.ones((2, 2), bool))
    with pytest.raises(ValueError):
        metric.snapshot(now_min=float("nan"), feasible_mask=np.ones((2, 2), bool))
    with pytest.raises(ValueError, match="shape"):
        metric.snapshot(now_min=2, feasible_mask=np.ones((3, 2), bool))


def test_masks_and_return_values_are_detached_and_snapshots_are_pure():
    fixed = np.array(((True, False),))
    metric = CoverageMetrics(episode_id="e1", fixed_mask=fixed, cell_size_km=1)
    fixed[:, :] = True
    metric.record_sar(((0, 0),), at_min=1)
    matrix = metric.last_scan_matrix()
    matrix[:, :] = 123
    first = metric.snapshot(now_min=1, feasible_mask=np.ones((1, 2), bool))
    first["windows"][0]["covered_cells"] = 999
    second = metric.snapshot(now_min=1, feasible_mask=np.ones((1, 2), bool))

    assert metric.last_scan_matrix()[0, 0] == 1
    assert metric.last_scan_matrix()[0, 1] == -np.inf
    assert second["fixed_searchable_cells"] == 1
    assert second["windows"][0]["covered_cells"] == 1
    assert second == metric.snapshot(now_min=1, feasible_mask=np.ones((1, 2), bool))


def test_empty_fixed_domain_and_new_episode_have_json_safe_zero_state():
    empty = CoverageMetrics(
        episode_id="new", fixed_mask=np.zeros((2, 2), bool), cell_size_km=1
    )
    snapshot = empty.snapshot(now_min=0, feasible_mask=np.ones((2, 2), bool))
    assert snapshot["status"] == "no_searchable_area"
    assert snapshot["fixed_searchable_cells"] == 0
    assert snapshot["currently_searchable_cells"] == 0
    assert snapshot["cumulative_pct"] is None
    assert snapshot["unseen_pct"] is None
    assert snapshot["overdue_seen_pct"] is None
    assert all(window["coverage_pct"] is None for window in snapshot["windows"])
    assert all(
        window["currently_searchable_coverage_pct"] is None
        for window in snapshot["windows"]
    )
    json.dumps(snapshot, allow_nan=False)


def test_zero_dynamic_domain_only_nulls_the_dynamic_window_percentage():
    metric = _metric((1, 1))
    metric.record_sar(((0, 0),), at_min=1)
    snapshot = metric.snapshot(now_min=1, feasible_mask=np.zeros((1, 1), bool))

    assert snapshot["status"] == "ok"
    assert snapshot["cumulative_pct"] == 100.0
    assert snapshot["windows"][0]["coverage_pct"] == 100.0
    assert snapshot["windows"][0]["currently_searchable_coverage_pct"] is None


def test_new_episode_with_the_same_domain_does_not_share_scan_state():
    prior = _metric((1, 1))
    prior.record_sar(((0, 0),), at_min=1)
    fresh = CoverageMetrics(
        episode_id="e2", fixed_mask=np.ones((1, 1), bool), cell_size_km=10
    )

    assert prior.snapshot(now_min=1, feasible_mask=np.ones((1, 1), bool))["ever_scanned_cells"] == 1
    assert fresh.snapshot(now_min=1, feasible_mask=np.ones((1, 1), bool))["ever_scanned_cells"] == 0

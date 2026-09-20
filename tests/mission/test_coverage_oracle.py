import json
from pathlib import Path

import pytest

from tests.mission.coverage_helpers import make_coverage_rig, oracle_coverage


EVENTS = [
    {"time": 0, "source": "sar", "cells": [(0, 0)]},
    {"time": 1, "source": "sar", "cells": [(0, 1), (1, 0)]},
    {"time": 59, "source": "sar", "cells": [(0, 1)]},
    {"time": 60, "source": "eo", "cells": [(1, 1)]},
]
DOMAIN = [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_oracle_uses_sar_only_and_a_left_open_right_closed_window():
    assert oracle_coverage(
        EVENTS, DOMAIN, now_min=60, window_min=60, cell_size_km=10
    ) == {
        "covered_cells": 2,
        "covered_area_km2": 200,
        "coverage_pct": 50.0,
    }
    assert oracle_coverage(
        EVENTS, DOMAIN, now_min=119, window_min=60, cell_size_km=10
    )["covered_cells"] == 0
    assert oracle_coverage(
        EVENTS, DOMAIN, now_min=59.999, window_min=60, cell_size_km=10
    )["covered_cells"] == 3


def test_oracle_deduplicates_cells_and_is_independent_of_event_order():
    repeated = EVENTS + [
        {"time": 1, "source": "sar", "cells": [(0, 1), (1, 0)]},
    ]
    expected = {
        "covered_cells": 2,
        "covered_area_km2": 200,
        "coverage_pct": 50.0,
    }
    deduplicated = [event for event in EVENTS if event["time"] != 59]
    assert oracle_coverage(
        repeated, DOMAIN, now_min=60, window_min=60, cell_size_km=10
    ) == expected
    assert oracle_coverage(
        deduplicated, DOMAIN, now_min=60, window_min=60, cell_size_km=10
    ) == expected
    assert oracle_coverage(
        list(reversed(repeated)), DOMAIN, now_min=60, window_min=60, cell_size_km=10
    ) == expected


@pytest.mark.parametrize("dt_min", [1.0, 0.25])
def test_coverage_rig_records_real_motion_and_time_state(dt_min):
    rig = make_coverage_rig(
        bbox=(10, 10, 16, 14),
        start_pose=(6.0, 12.0, 0.0),
        dt_min=dt_min,
    )
    before_fuel = rig.entity.fuel_remaining_pct
    record = rig.tick()

    assert record["before_pose"] != record["after_pose"]
    assert record["distance_cells"] == pytest.approx((160 / 10 / 60) * dt_min)
    assert rig.current_time == pytest.approx(dt_min)
    assert rig.state.current_time == pytest.approx(dt_min)
    assert rig.entity.fuel_remaining_pct < before_fuel
    assert record["sar_imaging"] is False
    assert record["phase"] == "transit_astar"
    assert set(record) == {
        "before_pose",
        "after_pose",
        "phase",
        "applied_command",
        "footprint",
        "progress_cells",
        "sar_imaging",
        "distance_cells",
        "max_speed_cells_min",
        "obstacle_intersection",
        "safety_intervened",
        "safety_interventions",
        "safety_obstacle_mask_cells",
    }


def test_oracle_returns_null_percentage_for_an_empty_domain():
    assert oracle_coverage(
        EVENTS, [], now_min=60, window_min=60, cell_size_km=10
    ) == {
        "covered_cells": 0,
        "covered_area_km2": 0,
        "coverage_pct": None,
    }


def test_audit_baseline_is_frozen_with_exact_counts():
    baseline_path = Path(__file__).parents[1] / "fixtures" / "coverage_audit_baseline.json"
    baseline = json.loads(baseline_path.read_text())
    assert baseline["seed"] == 42
    assert baseline["common_end_min"] == 177
    assert baseline["sar60_counts"] == {"legacy": 53, "new": 1}
    assert baseline["dynamic_denominator"] == 646
    assert baseline["fixed_denominator"] == 671
    assert baseline["new_frames"] == 500
    assert baseline["unique_sim_times"] == 177
    assert baseline["legacy_coverage_counts"] == {"legacy": 108, "new": 22}
    denominator = baseline["legacy_coverage_denominator"]
    assert baseline["legacy_coverage_pct"]["legacy"] == pytest.approx(
        100 * baseline["legacy_coverage_counts"]["legacy"] / denominator
    )
    assert baseline["legacy_coverage_pct"]["new"] == pytest.approx(
        100 * baseline["legacy_coverage_counts"]["new"] / denominator
    )

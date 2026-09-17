import json
from pathlib import Path

import pytest

from tests.mission.coverage_helpers import oracle_coverage


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
    expected = oracle_coverage(
        repeated, DOMAIN, now_min=60, window_min=60, cell_size_km=10
    )
    assert oracle_coverage(
        list(reversed(repeated)), DOMAIN, now_min=60, window_min=60, cell_size_km=10
    ) == expected


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

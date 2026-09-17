import json
from dataclasses import FrozenInstanceError, asdict

import numpy as np
import pytest

from src.mission.coverage_service import (
    CoverageCompletion,
    CoverageService,
    CoverageTaskProgress,
)


def test_route_finish_is_not_scan_completion():
    service = CoverageService(np.ones((6, 4), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 6, 4), at_min=10)
    scanned = tuple((col, row) for col in range(6) for row in range(4))[:11]

    service.record("S1", 1, scanned, at_min=12)
    result = service.finish("S1", 1, at_min=20)

    assert result.complete is False
    assert result.scanned_cells == 11
    assert result.required_cells == 24
    assert len(result.missing_cells) == 13
    assert result.completion_pct == pytest.approx(100 * 11 / 24)


def test_required_cells_are_the_frozen_bbox_intersection_and_progress_is_frozen():
    fixed = np.array(
        (
            (True, False, True),
            (False, True, True),
            (True, True, False),
            (False, False, True),
        ),
        dtype=bool,
    )
    service = CoverageService(fixed)
    service.start("S1", 1, "U1", (1, 0, 4, 3), at_min=10)
    fixed[:, :] = False

    progress = service.progress("S1", 1)

    assert isinstance(progress, CoverageTaskProgress)
    assert progress.required_cells == ((1, 1), (1, 2), (2, 0), (2, 1), (3, 2))
    assert progress.scanned_cells == ()
    with pytest.raises(FrozenInstanceError):
        progress.task_id = "mutated"


def test_start_rejects_a_task_with_no_fixed_cells():
    service = CoverageService(np.zeros((3, 2), dtype=bool))

    with pytest.raises(ValueError, match="required"):
        service.start("empty", 1, "U1", (0, 0, 3, 2), at_min=0)

    assert service.progress("empty", 1) is None


def test_record_counts_only_unique_fixed_in_bounds_integer_cells():
    fixed = np.array(((True, False), (False, True), (True, False)), dtype=bool)
    service = CoverageService(fixed)
    service.start("S1", 1, "U1", (0, 0, 3, 2), at_min=10)

    service.record(
        "S1",
        1,
        ((0, 0), (0, 0), (1, 0), (1, 1), (2, 1), (2, 0), (-1, 0), (3, 0)),
        at_min=11,
    )

    result = service.finish("S1", 1, at_min=12)
    assert result.complete is True
    assert result.scanned_cells == 3
    assert result.required_cells == 3
    assert result.missing_cells == ()
    assert result.completion_pct == 100.0


def test_record_before_start_is_ignored_and_audited():
    service = CoverageService(np.ones((2, 2), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 2, 2), at_min=10)

    service.record("S1", 1, ((0, 0),), at_min=9.5)

    assert service.progress("S1", 1).scanned_cells == ()
    assert service.audit_signals[-1]["reason"] == "before_start"
    assert service.audit_signals[-1]["task_id"] == "S1"


def test_stale_generation_does_not_contribute_to_a_new_same_id_task():
    service = CoverageService(np.ones((2, 2), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 2, 2), at_min=0)
    service.record("S1", 1, ((0, 0),), at_min=1)
    service.start("S1", 2, "U2", (0, 0, 2, 2), at_min=2)

    service.record("S1", 1, ((0, 1), (1, 0), (1, 1)), at_min=3)

    result = service.finish("S1", 2, at_min=4)
    assert result.scanned_cells == 0
    assert result.complete is False
    assert service.audit_signals[-1]["reason"] == "stale_generation"


def test_closed_task_ignores_late_records_and_keeps_audit_signal():
    service = CoverageService(np.ones((2, 2), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 2, 2), at_min=0)
    service.close("S1", 1, "operator_cancelled")

    service.record("S1", 1, ((0, 0),), at_min=1)

    assert service.progress("S1", 1).scanned_cells == ()
    assert service.audit_signals[-1]["reason"] == "closed"
    assert service.audit_signals[-1]["task_id"] == "S1"


def test_finish_is_idempotent_and_completion_is_frozen():
    service = CoverageService(np.ones((2, 1), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 2, 1), at_min=0)
    service.record("S1", 1, ((0, 0),), at_min=1)

    first = service.finish("S1", 1, at_min=2)
    service.record("S1", 1, ((1, 0),), at_min=3)
    second = service.finish("S1", 1, at_min=4)

    assert isinstance(first, CoverageCompletion)
    assert second is first
    assert first.scanned_cells == 1
    assert first.complete is False
    with pytest.raises(FrozenInstanceError):
        first.completion_pct = 100.0


def test_stale_close_cannot_close_the_replacement_generation():
    service = CoverageService(np.ones((1, 1), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 1, 1), at_min=0)
    service.start("S1", 2, "U2", (0, 0, 1, 1), at_min=1)

    service.close("S1", 1, "late_failure")
    service.record("S1", 2, ((0, 0),), at_min=2)

    assert service.finish("S1", 2, at_min=3).complete is True


def test_cross_uav_reassignment_can_reuse_a_local_lease_generation():
    service = CoverageService(np.ones((1, 1), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 1, 1), at_min=0)
    service.close("S1", 1, "handoff", uav_id="U1")

    service.start("S1", 1, "U2", (0, 0, 1, 1), at_min=1)
    service.record("S1", 1, ((0, 0),), at_min=2, uav_id="U2")

    assert service.finish("S1", 1, at_min=3, uav_id="U2").complete is True


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), -float("inf")])
def test_start_rejects_invalid_start_times(value):
    service = CoverageService(np.ones((1, 1), dtype=bool))

    with pytest.raises(ValueError):
        service.start("S1", 1, "U1", (0, 0, 1, 1), at_min=value)


@pytest.mark.parametrize(
    "cells",
    [((0.0, 0),), ((float("nan"), 0),), ((0, float("inf")),), ((True, 0),)],
)
def test_record_rejects_non_integer_or_non_finite_coordinates(cells):
    service = CoverageService(np.ones((1, 1), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 1, 1), at_min=0)

    with pytest.raises(ValueError):
        service.record("S1", 1, cells, at_min=1)


def test_all_public_snapshots_are_json_safe_without_nan_or_infinity():
    service = CoverageService(np.ones((2, 2), dtype=bool))
    service.start("S1", 1, "U1", (0, 0, 2, 2), at_min=0)
    service.record("S1", 1, ((0, 0),), at_min=1)
    completion = service.finish("S1", 1, at_min=2)
    progress = service.progress("S1", 1)

    json.dumps(asdict(completion), allow_nan=False)
    json.dumps(asdict(progress), allow_nan=False)
    json.dumps(service.audit_signals, allow_nan=False)

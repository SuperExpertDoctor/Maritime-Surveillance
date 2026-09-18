from dataclasses import dataclass

import numpy as np
import pytest

from src.mission.coverage_policy import CoveragePolicy


@dataclass(frozen=True)
class Candidate:
    task_id: str
    bbox: tuple[int, int, int, int]


def test_classify_partitions_fixed_domain_and_preserves_inputs():
    fixed = np.array(
        [
            [True, True, True],
            [True, True, False],
            [True, True, True],
        ],
        dtype=bool,
    )
    last_sar = np.array(
        [
            [90.0, -np.inf, 0.0],
            [0.0, 0.0, -np.inf],
            [0.0, 0.0, 0.0],
        ],
    )
    feasible = np.array(
        [
            [True, False, True],
            [True, True, False],
            [True, True, True],
        ],
    )
    assigned = np.array(
        [
            [False, False, False],
            [False, False, False],
            [True, False, False],
        ],
    )
    legal = np.array(
        [
            [True, True, True],
            [False, True, False],
            [True, True, True],
        ],
    )
    policy = CoveragePolicy(fixed, primary_window_min=60)

    classes = policy.classify(
        now_min=120,
        last_sar=last_sar,
        feasible_mask=feasible,
        assigned_mask=assigned,
        legal_candidate_mask=legal,
    )

    assert tuple(classes) == (
        "fresh",
        "deferred_weather",
        "assigned_due",
        "geometry",
        "waiting_due",
    )
    stack = np.stack(tuple(classes.values())).astype(int)
    assert np.array_equal(stack.sum(axis=0), fixed.astype(int))
    assert classes["fresh"][0, 0]
    assert classes["deferred_weather"][0, 1]
    assert classes["assigned_due"][2, 0]
    assert classes["geometry"][1, 0]
    assert classes["waiting_due"][1, 1]
    assert not classes["waiting_due"][1, 2]

    fixed[0, 0] = False
    last_sar[0, 0] = -np.inf
    feasible[0, 0] = False
    assert policy.fixed_mask[0, 0]
    assert classes["fresh"][0, 0]


def test_classify_uses_left_open_window_and_accepts_unseen_sentinel():
    policy = CoveragePolicy(np.ones((1, 3), dtype=bool))
    classes = policy.classify(
        now_min=120,
        last_sar=np.array([[60.0, 60.0001, -np.inf]]),
        feasible_mask=np.ones((1, 3), dtype=bool),
        assigned_mask=np.zeros((1, 3), dtype=bool),
        legal_candidate_mask=np.ones((1, 3), dtype=bool),
    )

    assert not classes["fresh"][0, 0]
    assert classes["waiting_due"][0, 0]
    assert classes["fresh"][0, 1]
    assert classes["waiting_due"][0, 2]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"now_min": -1}, "now_min"),
        ({"now_min": np.nan}, "now_min"),
        ({"last_sar": np.zeros((2, 2))}, "last_sar"),
        ({"feasible_mask": np.zeros((2, 2), dtype=bool)}, "feasible_mask"),
        ({"assigned_mask": np.zeros((2, 2), dtype=bool)}, "assigned_mask"),
        ({"legal_candidate_mask": np.zeros((2, 2), dtype=bool)}, "legal_candidate_mask"),
    ],
)
def test_classify_validates_shapes_and_times(kwargs, message):
    policy = CoveragePolicy(np.ones((2, 3), dtype=bool))
    values = {
        "now_min": 120.0,
        "last_sar": np.full((2, 3), -np.inf),
        "feasible_mask": np.ones((2, 3), dtype=bool),
        "assigned_mask": np.zeros((2, 3), dtype=bool),
        "legal_candidate_mask": np.ones((2, 3), dtype=bool),
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        policy.classify(**values)


def test_classify_rejects_future_or_nan_sar_timestamps():
    policy = CoveragePolicy(np.ones((1, 1), dtype=bool))
    common = {
        "now_min": 10.0,
        "feasible_mask": np.ones((1, 1), dtype=bool),
        "assigned_mask": np.zeros((1, 1), dtype=bool),
        "legal_candidate_mask": np.ones((1, 1), dtype=bool),
    }
    for timestamp in (11.0, np.nan, np.inf, -1.0):
        with pytest.raises(ValueError, match="last_sar"):
            policy.classify(last_sar=np.array([[timestamp]]), **common)


def test_rank_search_candidates_prioritizes_unseen_then_due_age_density_and_bbox():
    candidates = (
        Candidate("fresh", (0, 0, 1, 1)),
        Candidate("old-low-density", (1, 0, 3, 1)),
        Candidate("old-high-density", (3, 0, 5, 1)),
        Candidate("unseen", (0, 1, 1, 2)),
        Candidate("same-a", (1, 1, 2, 2)),
        Candidate("same-b", (2, 1, 3, 2)),
    )
    last_sar = np.full((5, 2), -np.inf, dtype=float)
    last_sar[0, 0] = 100.0
    last_sar[1:3, 0] = 0.0
    last_sar[3:5, 0] = 0.0
    last_sar[1, 1] = 0.0
    last_sar[2, 1] = 0.0

    ranked = CoveragePolicy.rank_search_candidates(
        candidates,
        now_min=120.0,
        last_sar=last_sar,
        estimated_minutes={
            "fresh": 1.0,
        "old-low-density": 1.0,
        "old-high-density": 0.5,
            "unseen": 10.0,
            "same-a": 1.0,
            "same-b": 1.0,
        },
    )

    assert tuple(candidate.task_id for candidate in ranked) == (
        "unseen",
        "old-high-density",
        "old-low-density",
        "same-a",
        "same-b",
        "fresh",
    )


def test_rank_search_candidates_uses_stable_bbox_order_and_conservative_fallback():
    candidates = (
        Candidate("z", (2, 0, 3, 1)),
        Candidate("a", (1, 0, 2, 1)),
    )
    last_sar = np.zeros((4, 1), dtype=float)

    ranked = CoveragePolicy.rank_search_candidates(
        candidates,
        now_min=120.0,
        last_sar=last_sar,
        estimated_minutes={},
    )

    assert tuple(candidate.task_id for candidate in ranked) == ("a", "z")
    assert ranked is not candidates


def test_rank_search_candidates_validates_inputs():
    policy = CoveragePolicy(np.ones((1, 1), dtype=bool))
    with pytest.raises(ValueError, match="estimated_minutes"):
        policy.rank_search_candidates(
            (Candidate("x", (0, 0, 1, 1)),),
            now_min=1.0,
            last_sar=np.zeros((1, 1)),
            estimated_minutes={"x": 0.0},
        )

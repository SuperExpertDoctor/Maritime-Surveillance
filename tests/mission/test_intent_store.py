import numpy as np
import pytest

from src.mission.intent_store import IntentStore, build_scheduling_value


@pytest.fixture
def searchable_mask():
    mask = np.ones((6, 6), dtype=bool)
    mask[0, :] = False
    mask[2:4, 2:4] = False
    return mask


@pytest.fixture
def store(searchable_mask):
    return IntentStore(searchable_mask)


def freshness_data(**changes):
    data = {
        "label": "North passage",
        "bbox": [1, 1, 5, 5],
        "mode": "maintain_freshness",
        "priority": "high",
        "weight": 0.5,
        "valid_duration_min": 10.0,
        "revisit_interval_min": 5.0,
    }
    data.update(changes)
    return data


def test_create_update_cancel_and_expire_keep_history_with_exact_revisions(store):
    created = store.create(freshness_data(), now_min=10.0)
    assert created.intent_id == "I0001"
    assert created.revision == 1

    updated = store.update(
        created.intent_id, created.revision, {"label": "North channel"}, 12.0
    )
    assert updated.revision == 2
    assert updated.created_at_min == 10.0

    cancelled = store.cancel(updated.intent_id, updated.revision, 13.0)
    assert cancelled.lifecycle == "cancelled"
    assert cancelled.revision == 3
    assert store.active() == ()

    expiring = store.create(freshness_data(label="South", valid_duration_min=1.0), 20.0)
    assert store.expire(21.0) == (expiring.__class__(
        **{**expiring.__dict__, "lifecycle": "expired"}
    ),)
    assert store.expire(30.0) == ()
    expired = [intent for intent in store.intents() if intent.intent_id == expiring.intent_id][0]
    assert expired.revision == 1
    assert expired.lifecycle == "expired"


@pytest.mark.parametrize(
    "data",
    [
        freshness_data(extra="nope"),
        freshness_data(bbox=[1, 1.5, 5, 5]),
        freshness_data(bbox=[2, 2, 4, 4]),
        freshness_data(mode="search_priority", revisit_interval_min=5.0),
        freshness_data(weight=True),
    ],
)
def test_create_rejects_invalid_payloads(store, data):
    with pytest.raises(ValueError):
        store.create(data, now_min=0.0)


def test_create_allows_partial_land_but_rejects_all_land(store):
    partial = store.create(freshness_data(bbox=[1, 1, 4, 4]), 0.0)
    assert partial.bbox == (1, 1, 4, 4)

    with pytest.raises(ValueError, match="searchable"):
        store.create(freshness_data(bbox=[2, 2, 4, 4]), 0.0)


def test_status_uses_static_water_denominator_and_reports_unseen_without_infinity(store, searchable_mask):
    intent = store.create(freshness_data(), 0.0)
    info = np.ones((6, 6), dtype=float)
    last_scan = np.full((6, 6), -np.inf)
    last_scan[1, 1] = 8.0
    last_scan[1, 2] = 1.0

    status = store.evaluate(info, last_scan, searchable_mask, (), 10.0)[0]

    # bbox has 16 cells, 4 are static land, and all remaining cells count
    # whether or not another temporary feasibility mask would hide them.
    assert status.searchable_cells == 12
    assert status.scanned_cells == 2
    assert status.unseen_cells == 10
    assert status.max_scan_age_min is None
    assert status.coverage_ratio == pytest.approx(2 / 12)
    assert status.freshness_ratio == pytest.approx(1 / 12)
    assert status.unmet_reason == "no_legal_candidate"


def test_status_ignores_temporary_weather_mask_for_coverage_and_freshness(store, searchable_mask):
    intent = store.create(freshness_data(), 0.0)
    info = np.ones((6, 6), dtype=float)
    last_scan = np.full((6, 6), -np.inf)
    last_scan[1, 1] = 8.0
    last_scan[1, 2] = 1.0
    weather_mask = searchable_mask.copy()
    weather_mask[1:5, 1:5] = False
    weather_mask[1, 1] = True
    weather_mask[1, 2] = True

    status = store.evaluate(info, last_scan, weather_mask, (), 10.0)[0]

    assert status.searchable_cells == 12
    assert status.scanned_cells == 2
    assert status.unseen_cells == 10
    assert status.coverage_ratio == pytest.approx(2 / 12)
    assert status.freshness_ratio == pytest.approx(1 / 12)


def test_status_reports_no_legal_candidate_for_an_unmet_intent(store, searchable_mask):
    intent = store.create(freshness_data(), 0.0)
    info = np.ones((6, 6), dtype=float)
    last_scan = np.full((6, 6), -np.inf)
    blocked_candidate = {
        "task_id": "T1",
        "intent_ids": (intent.intent_id,),
        "status": "blocked",
    }

    status = store.evaluate(info, last_scan, searchable_mask, (blocked_candidate,), 10.0)[0]

    assert status.unmet_reason == "no_legal_candidate"


def test_scheduling_value_uses_maximum_demand_and_never_mutates_base():
    base = np.full((4, 4), 0.25)
    original = base.copy()
    last_scan = np.full((4, 4), -np.inf)
    intents = (
        _intent("I0001", (1, 1, 3, 3), "search_priority", "medium", 1.0),
        _intent("I0002", (2, 2, 4, 4), "search_priority", "high", 0.5),
    )

    result = build_scheduling_value(base, intents, last_scan, now_min=20.0)

    assert np.array_equal(base, original)
    assert result[1, 1] == pytest.approx(2.25)
    # At the overlap max(1 * 2, .5 * 3) is 2, rather than 3.5.
    assert result[2, 2] == pytest.approx(2.25)


def test_scheduling_value_does_not_weight_partial_land_intent_cells():
    base = np.full((4, 4), 0.25)
    last_scan = np.full((4, 4), -np.inf)
    searchable_mask = np.ones((4, 4), dtype=bool)
    searchable_mask[:2, :2] = False
    intent = _intent("I0001", (0, 0, 3, 3), "search_priority", "high", 1.0)

    result = build_scheduling_value(
        base,
        (intent,),
        last_scan,
        now_min=20.0,
        searchable_mask=searchable_mask,
    )

    assert np.array_equal(result[:2, :2], base[:2, :2])
    assert result[2, 2] == pytest.approx(3.25)


def test_freshness_weight_uses_age_and_treats_never_scanned_as_one():
    base = np.zeros((2, 2))
    last_scan = np.array([[8.0, -np.inf], [0.0, 20.0]])
    intent = _intent(
        "I0001", (0, 0, 2, 2), "maintain_freshness", "low", 1.0,
        revisit_interval_min=10.0,
    )

    result = build_scheduling_value(base, (intent,), last_scan, now_min=20.0)

    assert result[0, 0] == pytest.approx(1.0)
    assert result[0, 1] == pytest.approx(1.0)
    assert result[1, 0] == pytest.approx(1.0)
    assert result[1, 1] == pytest.approx(0.0)


def _intent(intent_id, bbox, mode, priority, weight, revisit_interval_min=None):
    from src.mission.contracts import Intent

    return Intent(
        intent_id=intent_id,
        revision=1,
        label=intent_id,
        bbox=bbox,
        mode=mode,
        priority=priority,
        weight=weight,
        created_at_min=0.0,
        expires_at_min=100.0,
        revisit_interval_min=revisit_interval_min,
        lifecycle="active",
    )

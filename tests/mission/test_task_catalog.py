
import pytest

from src.mission.contracts import ContactSnapshot, Intent
from src.mission.task_catalog import TaskCatalog
from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import BBox
from src.schedule.state_manager import StateManager


def _contact(
    contact_id,
    *,
    state="pending",
    vessel_class="unknown",
    first_seen=2.0,
    last_seen=8.0,
    assigned=None,
    probe=None,
    next_probe=0.0,
    position=(12.0, 12.0),
):
    return ContactSnapshot(
        contact_id,
        1,
        state,
        vessel_class,
        "mmsi-" + contact_id if vessel_class == "unknown" else None,
        first_seen,
        last_seen,
        position,
        (0.1, 0.0),
        0.2,
            assigned,
            probe,
            None,
            None,
            next_probe,
            (),
        )


def _intent(intent_id="I0001"):
    return Intent(
        intent_id,
        1,
        "operator focus",
        (8, 8, 18, 18),
        "search_priority",
        "high",
        1.0,
        0.0,
        100.0,
        None,
        "active",
    )


class CandidateSource:
    def __init__(self, candidates):
        self.candidates = candidates

    def extract(self, state, scheduling_value=None, intents=()):
        del state, scheduling_value, intents
        return CandidateResult(candidate_regions=list(self.candidates))


@pytest.fixture
def state():
    return StateManager(ConfigLoader.load())


def _search_candidate(bbox, *, eligible_since=None, intent_ids=()):
    candidate = {
        "bbox": BBox(*bbox),
        "cell_count": (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]),
        "avg_info": 0.2,
        "total_value": 8.0,
        "intent_ids": tuple(intent_ids),
    }
    if eligible_since is not None:
        candidate["eligible_since_min"] = eligible_since
    return candidate


def test_catalog_keeps_probe_track_and_search_candidates_with_stable_ids(state):
    source = CandidateSource([
        _search_candidate((8, 8, 12, 13), eligible_since=4.0, intent_ids=("I0001",)),
    ])
    catalog = TaskCatalog(candidate_extractor=source)
    contacts = (
        _contact("C0001"),
        _contact("C0002", state="tracking", vessel_class="type_ii", first_seen=1.0),
        _contact("C0003", state="cleared", vessel_class="type_i"),
    )

    first = catalog.build(state, contacts, (_intent(),), now_min=10.0)
    second = catalog.build(state, contacts, (_intent(),), now_min=11.0)

    assert {(task.kind, task.contact_id) for task in first if task.contact_id} == {
        ("probe", "C0001"),
        ("track", "C0002"),
    }
    assert not any(task.contact_id == "C0003" for task in first)
    search = next(task for task in first if task.kind == "search")
    assert search.bbox == (8, 8, 12, 13)
    assert search.intent_ids == ("I0001",)
    assert search.eligible_since_min == 4.0
    assert [task.task_id for task in first] == [task.task_id for task in second]
    assert [task.eligible_since_min for task in first] == [task.eligible_since_min for task in second]


def test_catalog_does_not_duplicate_reserved_contact_and_retains_uncapped_queue(state):
    candidates = [
        _search_candidate((index + 1, 10, index + 3, 20))
        for index in range(0, 24, 2)
    ]
    source = CandidateSource(candidates)
    catalog = TaskCatalog(candidate_extractor=source)
    contacts = (
        _contact("C0001", assigned="UAV-1", probe="P0001"),
        _contact("C0002"),
    )

    tasks = catalog.build(state, contacts, (), now_min=10.0)
    search_tasks = [task for task in tasks if task.kind == "search"]

    assert len(search_tasks) == len(candidates)
    assert not any(task.contact_id == "C0001" for task in tasks)
    assert sum(task.contact_id == "C0002" for task in tasks) == 1
    assert len({task.task_id for task in tasks}) == len(tasks)


def test_catalog_uses_observed_age_and_does_not_treat_candidate_as_approved(state):
    source = CandidateSource([_search_candidate((8, 8, 12, 13))])
    catalog = TaskCatalog(candidate_extractor=source)

    task = catalog.build(
        state,
        (_contact("C0001", first_seen=3.0, last_seen=9.0),),
        (),
        now_min=10.0,
    )[0]

    assert task.kind == "probe"
    assert task.eligible_since_min == 3.0
    assert not hasattr(task, "status")


def test_catalog_rejects_contact_when_target_to_base_return_exceeds_range(state):
    for uav in state.get_all_uavs():
        uav.fuel_remaining_pct = 25.0 / 128.0
    catalog = TaskCatalog(candidate_extractor=CandidateSource([]))

    tasks = catalog.build(
        state,
        (_contact("C0001", position=(20.0, 14.0)),),
        (),
        now_min=10.0,
    )

    assert tasks[0].feasible_uav_ids == ()

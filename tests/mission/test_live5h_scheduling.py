from dataclasses import replace

import pytest
import numpy as np

from src.mission.contracts import FeasibleEdge, TaskCandidate, TaskRecord
from src.mission.mission_scheduler import MissionScheduler, validate_selection
from src.schedule.task_allocator import TaskAllocator
from tests.mission.test_feature_episode_integration import (
    _coverage_capacity_satisfied_snapshot,
)
from tests.mission.test_mission_scheduler import _snapshot, _task, _resource, _edge, _selection
from tests.mission.test_coverage_model_failure import _engine
from src.mission.contracts import Assignment, AssignmentBatch, IntentCommand
from tests.mission.test_feature_commands_integration import _payload
from src.control.common.contracts import ControlEvent, ControlMode, ControlOwner, OperationMode
from tests.mission.coverage_helpers import make_coverage_rig


def _owned_focus_engine():
    engine = _engine()
    snapshot = engine.allocator.build_mission_snapshot(0.)
    by_id = {t.task_id: t for t in snapshot.candidates}
    edge = next(e for e in snapshot.feasible_edges if by_id[e.task_id].kind == "search")
    task = by_id[edge.task_id]
    assert engine.apply_assignment_batch(AssignmentBatch(snapshot.snapshot_id,
        (Assignment(task.task_id, edge.uav_id, dict(snapshot.uav_generations)[edge.uav_id], None),), "fixture"))
    return engine, task, edge.uav_id


@pytest.mark.parametrize("partial", [False, True])
def test_focus_attaches_existing_owner_without_duplicate_reservations(partial):
    engine, task, uav_id = _owned_focus_engine()
    c0, r0, c1, r1 = task.bbox
    bbox = (c0, r0, c0+5, r0+5) if not partial else (c1-2, r0, c1+3, r0+5)
    engine.intent_commands.enqueue(IntentCommand("focus", engine.episode_id, "create", None, None,
                                                  _payload(bbox=bbox)))
    intent = engine.apply_pending_intent_commands()[0].intent
    snapshot = engine.allocator.build_mission_snapshot(0., intents=engine.intents.active(),
        active_tasks=tuple(engine._mission_task_records.values()))
    owner = next(t for t in snapshot.active_tasks if t.task_id == task.task_id)
    assert intent.intent_id in owner.intent_ids
    status = engine._evaluate_intent_statuses(0.)[0]
    assert task.task_id in status.assigned_task_ids
    assert status.unmet_reason != "no_legal_candidate"
    assert engine.control_coordinator.active_task(uav_id).task_id == task.task_id
    assert len(engine.allocator.sm.get_unfinished_search_regions()) == 1
    if partial:
        assert any(intent.intent_id in t.intent_ids for t in snapshot.candidates)


def test_owned_focus_is_reviewed_once_and_failed_review_stays_pending():
    engine, task, _uav_id = _owned_focus_engine()
    intent = engine.intents.create(_payload(bbox=task.bbox), 0.)
    snapshot = engine.allocator.build_mission_snapshot(0., intents=(intent,),
        active_tasks=tuple(engine._mission_task_records.values()))
    snapshot = replace(snapshot, candidates=(), feasible_edges=(), available_uav_ids=(),
                       coverage_constraint=None, prompt_task_ids=())
    scheduler = engine.allocator.mission_scheduler
    assert engine.allocator._model_selection_skip_reason(snapshot) is None
    assert scheduler.decide(snapshot) is None
    assert engine._mission_task_records[task.task_id].status == "executing"
    assert len(engine.allocator.sm.get_unfinished_search_regions()) == 1
    assert engine.allocator._model_selection_skip_reason(snapshot) is None
    calls = []
    def review(current, payload):
        calls.append(payload["snapshot"])
        return _selection(current, [], defer="retain existing owner and prioritize focus")
    scheduler.selection_provider = review
    assert scheduler.decide(snapshot) is not None
    assert calls[0]["pending_intent_reviews"][0]["intent_id"] == intent.intent_id
    assert calls[0]["active_tasks"][0]["intent_ids"] == [intent.intent_id]
    assert engine.allocator._model_selection_skip_reason(snapshot) == "no_model_candidates"
    revised = replace(snapshot, intents=(replace(intent, revision=2),))
    assert engine.allocator._model_selection_skip_reason(revised) is None


def test_focus_event_reorders_safe_remaining_swaths_without_changing_owner():
    rig = make_coverage_rig(bbox=(8, 8, 18, 18), start_pose=(5, 10, 0))
    controller = rig.controller
    swaths = controller.scan_swaths
    target = next(cell for cell in swaths[-1].footprint if cell not in swaths[0].footprint)
    box = (target.col, target.row, target.col+1, target.row+1)
    observation = rig.observations.build(rig.entity, rig.state,
        events=(ControlEvent(1, 0., "intent_focus", "simulation", rig.entity.id,
                             {"task_id": controller.task.task_id, "bbox": box}),),
        bases=(), control_mode=ControlMode.HEURISTIC, control_owner=ControlOwner.HEURISTIC,
        operation_mode=OperationMode.COVERAGE, safety_intervened=False, current_time=0., dt_min=1.)
    before = controller._route_revision
    controller.act(observation)
    assert target in controller.scan_swaths[0].footprint
    assert set(controller.scan_swaths) == set(swaths)
    assert controller._route_revision > before
    assert controller._route_blocked(controller.route, observation.planning_obstacle_mask) is None


@pytest.mark.parametrize("evidence,reason", [(None, "awaiting_planning"),
    ((), "no_legal_candidate"), ("blocked", "resource_blocked"), ("ready", "waiting_assignment")])
def test_intent_status_uses_candidate_and_resource_evidence(evidence, reason):
    engine = _engine()
    intent = engine.intents.create(_payload(), 0.)
    candidate = replace(_task("focus"), intent_ids=(intent.intent_id,))
    candidates = (candidate,) if isinstance(evidence, str) else evidence
    status = engine.intents.evaluate(engine.allocator.sm.get_info_matrix(),
        engine.allocator.sm.get_last_scan_matrix(), engine.allocator.sm.get_searchable_mask(), (), 0.,
        candidates=candidates, actionable_task_ids=(candidate.task_id,) if evidence == "ready" else ())[0]
    assert status.unmet_reason == reason
    assert status.assigned_task_ids == ()


def test_unusable_probe_edges_skip_the_model_like_the_prompt():
    base = _coverage_capacity_satisfied_snapshot()
    probe = replace(base.candidates[0], kind="probe", bbox=None, contact_id="C1")
    snapshot = replace(base, candidates=(probe,), available_uav_ids=())
    allocator = TaskAllocator.__new__(TaskAllocator)
    allocator.mission_scheduler = MissionScheduler()
    assert allocator.mission_scheduler._prompt_payload(snapshot)["snapshot"]["candidates"] == []
    assert allocator._model_selection_skip_reason(snapshot) == "no_actionable_candidates"


def test_retained_pending_edges_do_not_make_an_empty_prompt_actionable():
    base = _coverage_capacity_satisfied_snapshot()
    pending = TaskRecord("retained", "search", "approved", (20, 20, 22, 22), None,
                         (), None, "call", 0., None, None, None)
    probe = replace(base.candidates[0], kind="probe", bbox=None, contact_id="C1")
    snapshot = replace(base, candidates=(probe,), active_tasks=(pending,),
                       feasible_edges=(_edge("retained", "U1", 1.),),
                       pending_search_task_ids=("retained",))
    allocator = TaskAllocator.__new__(TaskAllocator)
    allocator.mission_scheduler = MissionScheduler()
    assert allocator.mission_scheduler._prompt_payload(snapshot)["snapshot"]["candidates"] == []
    assert allocator._model_selection_skip_reason(snapshot) == "no_actionable_candidates"


def test_pending_focus_review_requires_meaningful_acknowledgement():
    from tests.mission.test_intent_candidates import _intent
    snapshot = replace(_coverage_capacity_satisfied_snapshot(), candidates=(), feasible_edges=(),
                       intents=(_intent("I1", (1, 1, 6, 6)),), coverage_constraint=None)
    response = _selection(snapshot, [])
    response["notes"] = ""
    scheduler = MissionScheduler(selection_provider=lambda *_a: response)
    assert scheduler.decide(snapshot) is None
    assert scheduler.pending_intent_reviews(snapshot)
    response["notes"] = "No available legal candidate; retain current search ownership."
    assert scheduler.decide(snapshot) is not None
    assert not scheduler.pending_intent_reviews(snapshot)


def test_unexecutable_probe_does_not_limit_search_admission():
    search, probe = _task("S1"), _task("Q", kind="probe", contact_id="C1")
    snapshot = _snapshot([search, probe], [_resource("U1")], [_edge("S1", "U1", 1.)])
    assert validate_selection(_selection(snapshot, ["S1"]), snapshot) == ()


def test_search_edges_at_probe_cap_are_not_actionable_model_work():
    active = TaskRecord("S0", "search", "executing", (20, 20, 22, 22), None,
                        (), "U0", None, 0., 0., None, None)
    snapshot = _snapshot([_task("S1"), _task("Q", kind="probe", contact_id="C1")],
        [_resource("U0", operation="coverage", current_task_id="S0"), _resource("U1")],
        [_edge("S1", "U1", 1.), _edge("Q", "U1", 1.)], active_tasks=(active,))
    prompt = MissionScheduler()._prompt_payload(snapshot)["snapshot"]
    assert [t["task_id"] for t in prompt["candidates"]] == ["Q"]


def test_episode_reset_does_not_acknowledge_the_next_episodes_focus():
    engine = _engine()
    intent = engine.intents.create(_payload(), 0.)
    snapshot = replace(_coverage_capacity_satisfied_snapshot(), candidates=(), feasible_edges=(),
                       intents=(intent,), coverage_constraint=None)
    engine.allocator.mission_scheduler.selection_provider = lambda current, _p: _selection(current, [])
    assert engine.allocator.mission_scheduler.decide(snapshot) is not None
    engine.reset(seed=42)
    next_intent = engine.intents.create(_payload(), 0.)
    assert next_intent.intent_id == intent.intent_id
    assert engine.allocator.mission_scheduler.pending_intent_reviews(replace(snapshot, intents=(next_intent,)))


def test_undersized_focus_fragment_does_not_duplicate_its_pending_owner():
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.datatypes import BBox, Region
    from src.mission.intent_store import IntentStore
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    fixed = np.zeros(allocator.config.grid.resolution, dtype=bool)
    fixed[10:15, 10:15] = True
    allocator.sm.configure_coverage_metrics(fixed, "fragment")
    allocator.sm.set_search_regions([Region("owner", BBox(10, 10, 14, 15), "search")])
    owner = TaskRecord("owner", "search", "approved", (10, 10, 14, 15), None,
                       (), None, "call", 0., None, None, None)
    store = IntentStore(fixed)
    intent = store.create(_payload(bbox=(10, 10, 15, 15)), 0.)
    allocator._mission_edges = lambda *_a, **_k: ()
    snapshot = allocator.build_mission_snapshot(0., intents=(intent,), active_tasks=(owner,))
    assert snapshot.candidates == ()
    assert snapshot.active_tasks[0].intent_ids == (intent.intent_id,)
    status = store.evaluate(allocator.sm.get_info_matrix(), allocator.sm.get_last_scan_matrix(),
                           fixed, snapshot.active_tasks, 0., candidates=snapshot.candidates)[0]
    assert status.coverage_ratio == 0.
    assert status.unmet_reason == "coverage_below_target"


@pytest.mark.parametrize("count", [1, 2, 3, 10])
def test_executable_probe_reserves_real_search_admission(count):
    ids = range(count)
    tasks = [_task(f"S{i}", bbox=(i * 3, 0, i * 3 + 2, 2)) for i in ids]
    tasks.append(_task("Q", kind="probe", contact_id="C1"))
    snapshot = _snapshot(tasks, [_resource(f"U{i}") for i in ids],
                         [_edge(t.task_id, f"U{i}", 1.) for t in tasks for i in ids],
                         available=tuple(f"U{i}" for i in ids))
    errors = validate_selection(_selection(snapshot, [t.task_id for t in tasks[:-1]]), snapshot)
    assert any(e.startswith("probe_search_admission_limit:") for e in errors)
    cap = int(count * .8)
    legal = [t.task_id for t in tasks[:cap]] + ["Q"]
    assert validate_selection(_selection(snapshot, legal), snapshot) == ()


@pytest.mark.parametrize("blocked", [False, True])
def test_production_snapshot_probe_reserve_releases_search_preemption(blocked):
    engine = _engine()
    allocator = engine.allocator
    resources = tuple(replace(r, operation="coverage", current_task_id=f"S{i}",
                              last_reassigned_at_min=0.)
                      for i, r in enumerate(allocator._mission_resources()))
    records = tuple(TaskRecord(f"S{i}", "search", "executing", (i, 0, i + 1, 1),
                              None, (), r.uav_id, None, 0., 1., None, None)
                    for i, r in enumerate(resources))
    probe = replace(_task("Q", kind="probe", contact_id="C1"),
                    feasible_uav_ids=tuple(r.uav_id for r in resources))
    allocator.task_catalog.build = lambda *_a: (probe,)
    allocator._mission_resources = lambda: resources
    for r in resources:
        state = allocator.sm.get_uav(r.uav_id)
        state.control_mode = "heuristic"
        state.control_owner = "heuristic"
    allocator.sm.get_available_uavs = lambda: []
    regions = tuple(type("Region", (), {"id": r.task_id, "bbox": r.bbox})() for r in records)
    allocator.sm.get_unfinished_search_regions = lambda: regions
    allocator.sm.get_assigned_search_regions = lambda: regions
    allocator._mission_edges = lambda *_a, **_k: (() if blocked else tuple(
        _edge("Q", r.uav_id, 1.) for r in resources))
    snapshot = allocator.build_mission_snapshot(20., active_tasks=records)
    assert snapshot.coverage_constraint.desired_search_count == (len(resources) if blocked else int(len(resources) * .8))
    assert bool(snapshot.preemptible_uav_ids) is not blocked


def test_focus_utility_is_not_displaced_by_partition_admission():
    engine = _engine()
    allocator = engine.allocator
    allocator.mission_scheduler.max_tasks_in_prompt = 1
    broad = TaskCandidate("partition:broad", "search", (5, 5, 20, 20), None,
                          (), (engine.uavs[0].id,), 0., "medium", 10., 1., 1.)
    focus = replace(broad, task_id="search:focus", bbox=(5, 5, 10, 10),
                    intent_ids=("I1",), priority="high", utility=10.)
    window = allocator._coverage_prompt_window((broad, focus), 0.)
    assert window.tasks[0].task_id == focus.task_id


def test_focus_priority_survives_zone_containment_preference():
    from src.mission.coverage_policy import CoveragePolicy
    from src.mission.coverage_zones import ZonePartition
    fixed = np.ones((30, 30), dtype=bool)
    focus = replace(_task("focus", bbox=(8, 8, 16, 16)), intent_ids=("I1",), priority="high", utility=10.)
    ordinary = _task("ordinary", bbox=(11, 11, 15, 15))
    window = CoveragePolicy(fixed).select_window((focus, ordinary), ordinary_reserve=1,
        capacity=1, now_min=0., zones=ZonePartition(fixed, 3, 3))
    assert window.tasks == (focus,)


@pytest.mark.parametrize("count,blocked", [(1, False), (10, False), (1, True), (10, True)])
def test_pending_search_reassignment_respects_probe_admission_and_preserves_reservations(count, blocked):
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.datatypes import BBox, Region
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    resources = allocator._mission_resources()[:count]
    available = {resource.uav_id for resource in resources}
    idle = tuple(uav for uav in allocator.sm.get_available_uavs() if uav.id in available)
    allocator._mission_resources = lambda: resources
    allocator.sm.get_available_uavs = lambda: idle
    allocator.sm.configure_coverage_metrics(np.ones(allocator.config.grid.resolution, dtype=bool), "pending")
    records = tuple(TaskRecord(f"pending:{i}", "search", "approved", (i * 3, 0, i * 3 + 2, 2),
                               None, (), None, "validated", 0., None, None, None)
                    for i in range(count))
    regions = [Region(record.task_id, BBox(*record.bbox), "search", completion_pct=37.5)
               for record in records]
    allocator.sm.set_search_regions(regions)
    probe = replace(_task("Q", kind="probe", contact_id="C1"),
                    feasible_uav_ids=(resources[0].uav_id,))
    allocator.task_catalog.build = lambda *_a: (probe,)
    edges = tuple(_edge(record.task_id, resource.uav_id, 1.)
                  for record in records for resource in resources)
    if not blocked:
        edges += (_edge("Q", resources[0].uav_id, 1.),)
    allocator._mission_edges = lambda *_a, **_kw: edges

    batch = allocator.build_pending_search_batch(20., active_tasks=records)

    assignments = () if batch is None else batch.assignments
    expected = count if blocked else int(count * .8)
    assert len(assignments) == expected
    assert allocator.last_mission_snapshot.coverage_constraint.matchable_pending_count == expected
    assert allocator.pending_search_match_count(20., active_tasks=records) == expected
    if not blocked:
        assert resources[0].uav_id not in {item.uav_id for item in assignments}
    assert all(record.status == "approved" and record.assigned_uav_id is None for record in records)
    assert allocator.sm.get_search_regions() == regions
    assert all(region.status == "active" and region.assigned_uav_id is None
               and region.completion_pct == 37.5 for region in regions)


def test_approved_unassigned_search_cannot_bypass_selection_admission_limit():
    pending = TaskRecord("S1", "search", "approved", (1, 1, 5, 6), None,
                         (), None, "validated", 0., None, None, None)
    snapshot = _snapshot([_task("Q", kind="probe", contact_id="C1")], [_resource("U1")],
                         [_edge("Q", "U1", 1.), _edge("S1", "U1", 1.)], active_tasks=(pending,))
    errors = validate_selection(_selection(snapshot, ["S1"]), snapshot)
    assert "probe_search_admission_limit:0:1" in errors


def test_light_trigger_preserves_probe_reserve_for_pending_search():
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.datatypes import BBox, Region
    allocator = TaskAllocator(ConfigLoader.load(), llm_gateway=object())
    pending = TaskRecord("S1", "search", "approved", (1, 1, 5, 6), None,
                         (), None, "validated", 0., None, None, None)
    allocator.sm.set_search_regions([Region("S1", BBox(*pending.bbox), "search", completion_pct=37.5)])
    snapshot = _snapshot([_task("Q", kind="probe", contact_id="C1")], [_resource("U1")],
                         [_edge("Q", "U1", 1.), _edge("S1", "U1", 1.)], active_tasks=(pending,))
    allocator.build_mission_snapshot = lambda *_a, **_kw: snapshot

    result, batch = allocator._handle_light_mission_trigger(10., None, (pending,))

    assert batch is None
    assert result["action"] == "approved_tasks_deferred"
    assert pending.status == "approved" and pending.assigned_uav_id is None
    assert allocator.sm.get_pending_search_regions()[0].completion_pct == 37.5


def test_committed_pending_batch_cannot_be_followed_by_a_second_cap_bypass():
    from src.env.simulation import SimulationEngine
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.datatypes import BBox, Region
    from tests.mission.test_coverage_model_failure import OfflineGateway
    config = ConfigLoader.load()
    config = replace(config, uav=replace(config.uav, count_max=2), environment=replace(
        config.environment, island_count_min=0, island_count_max=0,
        thunderstorm_count_min=0, thunderstorm_count_max=0))
    engine = SimulationEngine(config, seed=42, llm_gateway=OfflineGateway())
    records = tuple(TaskRecord(f"retained:{i}", "search", "approved", bbox, None,
                               (), None, "validated", 0., None, None, None)
                    for i, bbox in enumerate(((8, 8, 13, 13), (16, 8, 21, 13))))
    engine._mission_task_records.update((record.task_id, record) for record in records)
    engine.allocator.sm.set_search_regions([
        Region(record.task_id, BBox(*record.bbox), "search", completion_pct=37.5) for record in records])

    first = engine.allocator.build_pending_search_batch(0., active_tasks=records)
    assert first is not None and len(first.assignments) == 1
    assert engine.apply_assignment_batch(first)
    current = tuple(engine._mission_task_records.values())
    assert sum(record.status == "executing" for record in current) == 1

    second = engine.allocator.build_pending_search_batch(0., active_tasks=current)
    assert second is None
    _result, light = engine.allocator._handle_light_mission_trigger(0., None, current)
    assert light is None
    assert engine._mission_task_records == {record.task_id: record for record in current}
    assert len(engine.allocator.sm.get_pending_search_regions()) == 1
    assert len(engine.allocator.sm.get_unfinished_search_regions()) == 2
    assert engine.allocator.sm.get_pending_search_regions()[0].completion_pct == 37.5

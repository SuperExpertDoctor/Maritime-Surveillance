"""Deterministic geometry, matching, validator and real allocator acceptance."""

from dataclasses import replace
from types import SimpleNamespace
import json

import numpy as np
import pytest

from src.mission.config import CoverageConfig
from src.mission.contracts import (
    CoverageConstraint,
    FeasibleEdge,
    ZoneCoverageRequirement,
)
from src.mission.coverage_metrics import CoverageMetrics
from src.mission.coverage_policy import (
    CoveragePolicy,
    adaptive_search_fraction,
    build_coverage_constraint,
)
from src.mission.coverage_zones import (
    ZonePartition,
    ZoneQuotaInput,
    build_zone_coverage_summary,
    build_zone_quota_inputs,
)
from src.mission.mission_scheduler import MissionScheduler
from tests.mission.test_coverage_prompt_window import _task, _edge
from tests.mission.test_coverage_decision_budget import _snapshot, _selection


def _summary(zones, last=None, now=0, tasks=()):
    return build_zone_coverage_summary(
        zones,
        now_min=now,
        last_sar=np.full(zones.fixed_mask.shape, -np.inf) if last is None else last,
        feasible_mask=zones.fixed_mask,
        primary_window_min=60,
        in_flight_tasks=tasks,
    )


def _constraint(ids=("U1", "U2"), reps=("S1", "S2"), edges=None, quota=(), fraction=1):
    return build_coverage_constraint(
        available_ids=ids,
        representatives=reps,
        edges=tuple(_edge(s, u) for s in reps for u in ids) if edges is None else edges,
        active_search_count=0,
        fraction=fraction,
        zone_requirements_input=quota,
    )


def _fleet_snapshot():
    ids = tuple(f"U{i}" for i in range(1, 11))
    tasks = tuple(
        replace(
            _task(f"S{c}{r}", bbox=(c * 10 + 1, r * 10 + 1, c * 10 + 4, r * 10 + 4)),
            feasible_uav_ids=ids,
        )
        for c in range(3)
        for r in range(3)
    )
    tasks += (replace(_task("extra", bbox=(5, 5, 8, 8)), feasible_uav_ids=ids),)
    zones = ZonePartition(np.ones((30, 30), bool))
    summary = _summary(zones)
    quota = build_zone_quota_inputs(zones, summary, tasks, threshold=0.5, max_slots=10)
    edges = tuple(_edge(task.task_id, u) for task in tasks for u in ids)
    constraint = _constraint(ids, tuple(t.task_id for t in tasks), edges, quota)
    snapshot = replace(
        _snapshot(candidate_count=0, resource_count=10),
        candidates=tasks,
        feasible_edges=edges,
        coverage_constraint=constraint,
        coverage_summary=summary,
        reviewer_summary="",
        prompt_task_ids=tuple(t.task_id for t in tasks),
    )
    return snapshot


def test_partition_exhaustive_remainder_containment_and_tie():
    fixed = np.ones((7, 8), bool)
    fixed[0, 0] = False
    zones = ZonePartition(fixed, 3, 2)
    cells = [cell for zone in zones.zone_ids for cell in zones.zone_cells(zone)]
    assert len(cells) == len(set(cells)) == 55
    assert zones.zone_bbox("zone:2:1") == (4, 4, 7, 8)
    assert zones.contains_bbox("zone:0:0", (0, 0, 2, 4))
    assert not zones.contains_bbox("zone:0:0", (0, 0, 3, 4))
    assert zones.zone_of_bbox((1, 1, 3, 2)) == "zone:0:0"
    assert zones.zone_of_bbox((8, 8, 9, 9)) is None
    fixed[:] = False
    assert len(zones.zone_ids) == 6
    assert ZonePartition(fixed).zone_ids == ()


@pytest.mark.parametrize("now", [60, 120])
def test_summary_matches_metric_and_inflight_union(now):
    fixed = np.ones((6, 6), bool)
    metrics = CoverageMetrics(episode_id="test", fixed_mask=fixed, cell_size_km=1)
    metrics.record_sar(((0, 0), (0, 1)), at_min=0)
    metrics.record_sar(((2, 2),), at_min=now)
    task = SimpleNamespace(
        task_id="inflight",
        kind="search",
        status="executing",
        assigned_uav_id="U1",
        bbox=(0, 0, 2, 3),
    )
    zones = ZonePartition(fixed, 3, 2)
    summary = _summary(zones, metrics.last_scan_matrix(), now, (task, task))
    actual = metrics.snapshot(now_min=now, feasible_mask=fixed)
    assert summary["gap_pct"] == pytest.approx(
        actual["unseen_pct"] + actual["overdue_seen_pct"]
    )
    assert summary["zones"][0]["in_flight_cells"] == 6
    assert summary["zones"][0]["effective_gap_fraction"] == 0
    assert "zone:0:0" not in {
        q.zone_id for q in build_zone_quota_inputs(zones, summary, (), threshold=0.5)
    }
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize(
    "status,assigned,kind",
    [
        ("completed", "U1", "search"),
        ("candidate", "U1", "search"),
        ("approved", None, "search"),
        ("executing", "U1", "probe"),
    ],
)
def test_ineligible_work_does_not_reduce_gap(status, assigned, kind):
    task = SimpleNamespace(
        task_id="x",
        status=status,
        assigned_uav_id=assigned,
        kind=kind,
        bbox=(0, 0, 3, 3),
    )
    summary = _summary(ZonePartition(np.ones((3, 3), bool), 1, 1), tasks=(task,))
    assert summary["zones"][0]["effective_gap_fraction"] == 1


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, 101])
def test_budget_rejects_bad_gap(value):
    with pytest.raises(ValueError):
        adaptive_search_fraction(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(zone_cols=0),
        dict(zone_rows=True),
        dict(search_uav_fraction_max=0.3),
        dict(zone_quota_gap_threshold=1.1),
        dict(search_uav_fraction_max=float("nan")),
    ],
)
def test_config_rejects_invalid_zone_settings(kwargs):
    with pytest.raises(ValueError):
        CoverageConfig(**kwargs)


def test_budget_endpoints_and_available_anchor():
    assert [adaptive_search_fraction(x) for x in (0, 50, 100)] == [0.4, 0.7, 1]
    assert _constraint(fraction=0.4).required_new_search_count == 1
    assert (
        build_coverage_constraint(
            healthy_count=10,
            active_search_count=8,
            available_ids=("U1", "U2"),
            representatives=("S1", "S2"),
            edges=(_edge("S1"), _edge("S2", "U2")),
            fraction=1,
        ).required_new_search_count
        == 2
    )


def test_one_task_cannot_match_two_uavs():
    result = _constraint(reps=("S1",), quota=(ZoneQuotaInput("z", ("S1",)),))
    assert result.required_new_search_count == 1
    assert result.infeasible_reason == "insufficient_available_resources"


def test_distinct_zone_candidates_and_augmenting_paths():
    quota = (ZoneQuotaInput("z1", ("S1",)), ZoneQuotaInput("z2", ("S2",)))
    result = _constraint(
        edges=(_edge("S1"), _edge("S1", "U2"), _edge("S2")), quota=quota
    )
    assert result.infeasible_reason is None
    assert [
        (z.representative_task_ids, z.must_service_task_ids)
        for z in result.zone_requirements
    ] == [(("S1",), ("S1",)), (("S2",), ("S2",))]
    assert result.required_new_search_count == 2


def test_zone_quota_matching_minimizes_global_transit_cost():
    quota = (
        ZoneQuotaInput("z1", ("A", "B")),
        ZoneQuotaInput("z2", ("C",)),
    )
    edges = (
        FeasibleEdge("A", "U1", 1.0, 1.0, 1.0, 0.0, "A:U1"),
        FeasibleEdge("A", "U2", 100.0, 1.0, 1.0, 0.0, "A:U2"),
        FeasibleEdge("B", "U2", 2.0, 1.0, 1.0, 0.0, "B:U2"),
        FeasibleEdge("C", "U1", 1.0, 1.0, 1.0, 0.0, "C:U1"),
    )

    result = _constraint(
        ids=("U1", "U2"),
        reps=("A", "B", "C"),
        edges=edges,
        quota=quota,
    )

    assert [
        zone.must_service_task_ids for zone in result.zone_requirements
    ] == [("B",), ("C",)]
    assert result.required_new_search_count == 2


def test_zone_quota_does_not_reuse_one_task_for_multiple_zones():
    quota = (
        ZoneQuotaInput("z1", ("shared", "z1-only")),
        ZoneQuotaInput("z2", ("shared", "z2-only")),
    )
    edges = (
        FeasibleEdge("shared", "U1", 1.0, 1.0, 1.0, 0.0, "shared:U1"),
        FeasibleEdge("shared", "U2", 1.0, 1.0, 1.0, 0.0, "shared:U2"),
        FeasibleEdge("z1-only", "U1", 2.0, 1.0, 1.0, 0.0, "z1-only:U1"),
        FeasibleEdge("z2-only", "U2", 2.0, 1.0, 1.0, 0.0, "z2-only:U2"),
    )

    result = _constraint(
        ids=("U1", "U2"),
        reps=("shared", "z1-only", "z2-only"),
        edges=edges,
        quota=quota,
    )

    selected = tuple(
        task_id
        for zone in result.zone_requirements
        for task_id in zone.must_service_task_ids
    )
    assert len(result.zone_requirements) == 2
    assert len(selected) == len(set(selected)) == 2


def test_zone_quota_front_loads_the_candidate_with_shortest_minimum_transit():
    quota = (ZoneQuotaInput("remote-zone", ("near", "remote")),)
    edges = (
        _edge("near", "U1"),
        FeasibleEdge("remote", "U1", 10.0, 1.0, 1.0, 0.0, "remote:U1"),
    )

    result = _constraint(
        ids=("U1",),
        reps=("near", "remote"),
        edges=edges,
        quota=quota,
    )

    assert result.zone_requirements[0].must_service_task_ids == ("near",)


def test_unreachable_zone_and_global_shortage_are_explicit():
    result = _constraint(
        edges=(_edge("S1"),),
        quota=(ZoneQuotaInput("z1", ("S1",)), ZoneQuotaInput("z2", ("S2",))),
    )
    assert result.required_new_search_count == 1
    assert len(result.zone_requirements) == 1
    assert result.zone_infeasible == (("z2", "no_feasible_zone_representative"),)


def test_window_skips_blocked_candidates_and_spreads_contained_representatives():
    zones = ZonePartition(np.ones((20, 10), bool), 2, 1)
    tasks = (
        _task("a", bbox=(1, 1, 4, 4)),
        _task("b", bbox=(11, 1, 14, 4)),
        _task("blocked-a", bbox=(1, 1, 4, 4)),
        _task("blocked-b", bbox=(11, 1, 14, 4)),
        _task("later-a", bbox=(5, 5, 8, 8)),
        _task("later-b", bbox=(15, 5, 18, 8)),
    )
    result = CoveragePolicy(zones.fixed_mask).select_window(
        tasks, ordinary_reserve=4, capacity=4, now_min=0, zones=zones
    )
    assert result.representative_task_ids == ("a", "b", "later-a", "later-b")


def test_first_round_ten_searches_nine_quotas_and_prompt_budget():
    snapshot = _fleet_snapshot()
    scheduler = MissionScheduler(selection_provider=lambda *_: {})
    assert snapshot.coverage_constraint.required_new_search_count == 10
    assert len(snapshot.coverage_constraint.zone_requirements) == 9
    payload = _selection(snapshot)
    payload["selected_task_ids"] = [task.task_id for task in snapshot.candidates]
    assert scheduler.validate_selection(payload, snapshot) == ()
    prompt = scheduler._prompt_payload(snapshot)
    assert prompt["snapshot"]["coverage_summary"] == snapshot.coverage_summary
    assert len(json.dumps(prompt, ensure_ascii=False).encode()) < 100_000
    visible = {task["task_id"] for task in prompt["snapshot"]["candidates"]}
    assert set(snapshot.coverage_constraint.must_service_task_ids) <= visible
    payload["selected_task_ids"] = payload["selected_task_ids"][:4]
    errors = scheduler.validate_selection(payload, snapshot)
    assert "coverage_floor_not_met:10:4" in errors
    assert any(e.startswith("zone_quota_not_met:") for e in errors)


def test_coverage_floor_allows_idle_capacity_fill_even_with_zero_residual():
    snapshot = _fleet_snapshot()
    scheduler = MissionScheduler(selection_provider=lambda *_: {})
    zero_budget = replace(
        snapshot,
        coverage_constraint=CoverageConstraint(0, 0, 0, (), ()),
    )
    payload = _selection(zero_budget)
    payload["selected_task_ids"] = [t.task_id for t in snapshot.candidates]
    errors = scheduler.validate_selection(payload, zero_budget)
    assert not any(error.startswith("coverage_floor_not_met:") for error in errors)
    assert errors == ()

    required = 4
    constrained = replace(
        snapshot,
        coverage_constraint=CoverageConstraint(required, 0, required, (), ()),
    )
    payload = _selection(constrained)
    payload["selected_task_ids"] = [t.task_id for t in snapshot.candidates]
    assert scheduler.validate_selection(payload, constrained) == ()
    payload["selected_task_ids"] = [
        t.task_id for t in snapshot.candidates[:required - 1]
    ]
    assert f"coverage_floor_not_met:{required}:{required - 1}" in scheduler.validate_selection(
        payload, constrained
    )


@pytest.mark.parametrize("kind", ["investigation", "direction_search"])
def test_urgent_search_shapes_do_not_count_as_ordinary(kind):
    snapshot = _fleet_snapshot()
    snapshot = replace(
        snapshot,
        candidates=(
            replace(snapshot.candidates[0], kind=kind),
            *snapshot.candidates[1:],
        ),
    )
    payload = _selection(snapshot)
    payload["selected_task_ids"] = [t.task_id for t in snapshot.candidates]
    assert "coverage_floor_not_met:10:9" in MissionScheduler(
        selection_provider=lambda *_: {}
    ).validate_selection(payload, snapshot)


def test_real_allocator_wiring_and_legacy():
    from tests.mission.test_coverage_scan_integration import _engine
    from src.schedule.config_loader import ConfigLoader
    from src.schedule.task_allocator import TaskAllocator

    engine = _engine(uav_count=10)
    snapshot = engine.allocator.build_mission_snapshot()
    assert snapshot.coverage_summary["gap_pct"] == 100
    assert snapshot.coverage_constraint.required_new_search_count == 10
    # Fleet partitions may cross the fixed reporting zones; do not force
    # nine tiny contained representatives back into the candidate window.
    assert len(snapshot.coverage_constraint.zone_requirements) + len(snapshot.coverage_constraint.zone_infeasible) == 9
    assert any(task.task_id.startswith("partition:") for task in snapshot.candidates
               if task.task_id in snapshot.prompt_task_ids)
    assert set(snapshot.coverage_constraint.must_service_task_ids) <= set(
        snapshot.prompt_task_ids
    )
    legacy = TaskAllocator(ConfigLoader.load()).build_mission_snapshot()
    assert legacy.coverage_summary is None and legacy.coverage_constraint is None


def test_real_completion_event_refreshes_snapshot_and_periodic_expiry():
    from scripts.persistent_coverage_scenarios import build_coverage_scenario
    from src.control.common.contracts import ControlEvent, ControlTask, OperationMode
    from src.mission.contracts import TaskRecord
    from src.schedule.datatypes import BBox

    engine = build_coverage_scenario(
        "coverage-open-water", seed=42, transport="fixture"
    )
    allocator = engine.allocator
    initial = allocator.build_mission_snapshot()
    assert initial.coverage_constraint.required_new_search_count == 10
    # Feed actual SAR accounting, then invoke the real completion callback.
    # This tests event/scheduler wiring without pretending to fly a whole sortie.
    zones = ZonePartition(allocator.sm.coverage_metrics.fixed_mask)
    all_cells = tuple(
        cell for zone in zones.zone_ids for cell in zones.zone_cells(zone)
    )
    cells = all_cells[: len(all_cells) // 2]
    allocator.sm.coverage_metrics.record_sar(cells, at_min=10)
    uav = engine.uavs[0]
    task = ControlTask("completed-test", OperationMode.COVERAGE, BBox(1, 1, 4, 4))
    engine._mission_task_records[task.task_id] = TaskRecord(
        task.task_id,
        "search",
        "executing",
        (1, 1, 4, 4),
        None,
        (),
        uav.id,
        "fixture",
        0,
        0,
        None,
        None,
    )
    allocator.sm.cycle = 1
    allocator.trigger_manager.mark_triggered("heavy", 9)
    engine._record_search_completion_event(
        uav,
        ControlEvent(
            1, 10, "search_complete", "test", uav.id, {"task_id": task.task_id}
        ),
        previous_task=task,
    )
    result, batch = allocator.mission_step(
        10, active_tasks=tuple(engine._mission_task_records.values())
    )
    assert result["trigger_type"] == "heavy"
    assert batch is not None, allocator.mission_scheduler.last_selection_errors
    fresh = allocator.last_mission_snapshot
    assert fresh.snapshot_id != initial.snapshot_id
    assert fresh.coverage_summary["gap_pct"] < 100
    assert fresh.coverage_constraint.required_new_search_count < 10
    assert "zone:0:0" not in {
        q.zone_id for q in fresh.coverage_constraint.zone_requirements
    }
    result, batch = allocator.mission_step(
        70, active_tasks=tuple(engine._mission_task_records.values())
    )
    assert result["trigger_type"] == "heavy"
    assert batch is not None, allocator.mission_scheduler.last_selection_errors
    assert allocator.last_mission_snapshot.coverage_summary["gap_pct"] == 100
    assert (
        allocator.last_mission_snapshot.coverage_constraint.required_new_search_count
        == 10
    )


def test_feasible_zone_must_service_stays_binding_during_global_shortage():
    snapshot = _fleet_snapshot()
    constraint = replace(
        snapshot.coverage_constraint,
        infeasible_reason="insufficient_available_resources",
    )
    snapshot = replace(snapshot, coverage_constraint=constraint)
    payload = _selection(snapshot)
    payload["selected_task_ids"] = ["extra"]
    errors = MissionScheduler(selection_provider=lambda *_: {}).validate_selection(
        payload, snapshot
    )
    assert not any(e.startswith("search_count_not_exact:") for e in errors)
    assert "zone_must_service_not_selected:zone:0:0" in errors


def test_prompt_budget_guard_includes_large_zone_summary():
    snapshot = replace(
        _fleet_snapshot(), coverage_summary={"zones": [{"details": "x" * 110_000}]}
    )
    scheduler = MissionScheduler(
        selection_provider=lambda *_: pytest.fail("must not call provider")
    )
    assert scheduler.decide(snapshot) is None
    assert scheduler.last_selection_errors[0].startswith("prompt_budget_exceeded:")


def test_prompt_preserves_geometry_safety_and_coverage_floor_policy():
    scheduler = MissionScheduler(selection_provider=lambda *_: {})
    for fragment in (
        "never invent a bbox",
        "AT LEAST required_new_search_count",
        "required_new_search_count is zero",
        "available UAV",
        "minimum coverage floor, not a fleet utilization ceiling",
        "zone_requirements",
        "160 characters",
        "generation",
        "must not be preempted",
    ):
        assert fragment in scheduler.system_prompt


def test_real_inflight_assignment_and_failed_uav_filter():
    from tests.mission.test_coverage_scan_integration import _engine
    from src.mission.contracts import TaskRecord

    engine = _engine(uav_count=10)
    sm = engine.allocator.sm
    uav = sm.get_all_uavs()[0]
    uav.status = "transit"
    uav.operation_mode = "coverage"
    task = TaskRecord(
        "inflight",
        "search",
        "executing",
        (0, 0, 10, 10),
        None,
        (),
        uav.id,
        "fixture",
        0,
        0,
        None,
        None,
    )
    snapshot = engine.allocator.build_mission_snapshot(active_tasks=(task,))
    assert uav.id not in snapshot.available_uav_ids
    zone = next(
        z for z in snapshot.coverage_summary["zones"] if z["zone_id"] == "zone:0:0"
    )
    assert zone["effective_gap_fraction"] == 0
    assert "zone:0:0" not in {
        q.zone_id for q in snapshot.coverage_constraint.zone_requirements
    }
    uav.status = "failed"
    snapshot = engine.allocator.build_mission_snapshot(active_tasks=(task,))
    zone = next(
        z for z in snapshot.coverage_summary["zones"] if z["zone_id"] == "zone:0:0"
    )
    assert zone["in_flight_cells"] == 0
    assert zone["effective_gap_fraction"] == 1


def test_six_probes_four_searches_are_not_first_round_coverage():
    snapshot = _fleet_snapshot()
    probes = tuple(
        replace(
            _task(f"P{i}", kind="probe"),
            contact_id=f"C{i}",
            feasible_uav_ids=snapshot.available_uav_ids,
        )
        for i in range(6)
    )
    snapshot = replace(
        snapshot,
        candidates=(*snapshot.candidates, *probes),
        prompt_task_ids=(*snapshot.prompt_task_ids, *(p.task_id for p in probes)),
        feasible_edges=(
            *snapshot.feasible_edges,
            *(_edge(p.task_id, u) for p in probes for u in snapshot.available_uav_ids),
        ),
    )
    payload = _selection(snapshot)
    payload["selected_task_ids"] = [
        *(p.task_id for p in probes),
        *(t.task_id for t in snapshot.candidates[:4]),
    ]
    errors = MissionScheduler(selection_provider=lambda *_: {}).validate_selection(
        payload, snapshot
    )
    assert "coverage_floor_not_met:10:4" in errors


def test_matching_cardinality_agrees_with_exhaustive_small_graph_oracle():
    from itertools import permutations

    ids = ("U1", "U2", "U3")
    reps = ("S1", "S2", "S3")
    all_pairs = tuple((s, u) for s in reps for u in ids)
    for bits in range(1 << len(all_pairs)):
        pairs = {pair for i, pair in enumerate(all_pairs) if bits & (1 << i)}
        expected = max(
            (
                sum((s, u) in pairs for s, u in zip(reps, order))
                for order in permutations(ids)
            )
        )
        result = _constraint(
            ids,
            reps,
            tuple(_edge(s, u) for s, u in sorted(pairs)),
            tuple(ZoneQuotaInput(s, (s,)) for s in reps),
        )
        assert result.required_new_search_count == expected
        assert len(set(result.must_service_task_ids)) == len(
            result.must_service_task_ids
        )
        assert len(result.zone_requirements) <= expected


def test_summary_rejects_future_and_negative_time_and_bad_shape():
    zones = ZonePartition(np.ones((3, 3), bool))
    for matrix in (np.ones((3, 3)), np.full((3, 3), -1), np.zeros((2, 2))):
        with pytest.raises(ValueError):
            _summary(zones, matrix)


def test_empty_zones_and_weather_accounting():
    zones = ZonePartition(np.zeros((3, 3), bool))
    assert _summary(zones)["gap_pct"] == 0
    assert build_zone_quota_inputs(zones, _summary(zones), (), threshold=0.5) == ()
    summary = build_zone_coverage_summary(
        ZonePartition(np.ones((3, 3), bool), 1, 1),
        now_min=0,
        last_sar=np.full((3, 3), -np.inf),
        feasible_mask=np.zeros((3, 3), bool),
        primary_window_min=60,
    )
    assert summary["zones"][0]["searchable_cells"] == 0
    assert summary["gap_pct"] == 100


def test_summary_copy_and_default_empty_zone_serialization():
    snapshot = _fleet_snapshot()
    source = {"zones": [{"gap": 1}]}
    copied = replace(snapshot, coverage_summary=source)
    source["zones"][0]["gap"] = 0
    assert copied.coverage_summary["zones"][0]["gap"] == 1
    constraint = CoverageConstraint(1, 0, 1, ("extra",), ("extra",))
    snapshot = replace(snapshot, coverage_constraint=constraint, coverage_summary=None)
    payload = MissionScheduler(selection_provider=lambda *_: {})._prompt_payload(
        snapshot
    )["snapshot"]
    assert "coverage_summary" not in payload
    assert "zone_requirements" not in payload["coverage_constraint"]


def test_real_engine_first_step_applies_ten_parallel_searches():
    from scripts.persistent_coverage_scenarios import build_coverage_scenario

    engine = build_coverage_scenario(
        "coverage-open-water", seed=42, transport="fixture"
    )
    result = engine.step()
    assert result["action"] == "mission_selection_approved", result
    records = tuple(engine._mission_task_records.values())
    assigned = [
        r
        for r in records
        if r.status in {"approved", "executing"} and r.assigned_uav_id
    ]
    assert len(assigned) == 10
    assert len({r.assigned_uav_id for r in assigned}) == 10
    assert all(r.kind == "search" for r in assigned)


def test_real_weather_blockage_reports_infeasible_without_inventing_work():
    from scripts.persistent_coverage_scenarios import build_coverage_scenario

    engine = build_coverage_scenario(
        "coverage-open-water", seed=42, transport="fixture"
    )
    sm = engine.allocator.sm
    sm.set_environment_obstacles([], np.ones(sm.obstacle_mask.shape, dtype=bool))
    snapshot = engine.allocator.build_mission_snapshot()
    assert snapshot.coverage_summary["gap_pct"] == 100
    assert all(
        zone["searchable_cells"] == 0 for zone in snapshot.coverage_summary["zones"]
    )
    constraint = snapshot.coverage_constraint
    assert constraint.desired_search_count == 10
    assert constraint.required_new_search_count == 0
    assert constraint.infeasible_reason == "insufficient_available_resources"
    assert constraint.zone_requirements == ()
    assert len(constraint.zone_infeasible) == 9

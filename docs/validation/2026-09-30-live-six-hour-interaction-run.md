# Six-Hour Live Interaction Acceptance Run

Date: 2026-09-30. Branch: `main`. Seed: `7`.
Requested window: **six** real wall-clock hours of `main.py` including
model-response latency, with hourly 5x5 operator focus areas and three
blue-fleet intervention rounds at 1.5/3/4.5 hours.

## Verdict

**PASS.** `outputs/live-interaction-20260930T061848776Z-7-a09ee325/audit.json`
reports `passed: true` with zero failures, produced by the runner's own
`auditAcceptance` block after `main.py` exited cleanly
(`processExitCode: 0`).

## Run Identity

- Command: `node scripts/run_live_interaction_acceptance.mjs --hours 6 --seed 7`
- Child: `python main.py --steps 1000 --step-delay 60 --wall-seconds 21720 --llm-probe-timeout 120 --port 39659 --memory-root .../strategy_memory --run-report-dir .../main-report`
- Episode: `episode-5f9dccfb7d7544c48027d6d17db1606c`
- Wall runtime: **21,765.27 s** (target 21,720 s = 6 h window + 12 min
  observation tail; required >= 21,600 s). Model latency is inside the
  measured window (synchronous LLM calls block step advancement).
- Simulation steps: 259 (mean ~84 s/step vs the 60 s floor; model calls
  and paused/recovery cycles explain the gap).
- All eight scheduled events dispatched with **<= 3 ms** drift
  (`maxDispatchDriftMs: 3`).

## Scheduled Events (all confirmed in authoritative frames)

| Event | Scheduled | Result |
| --- | --- | --- |
| focus I0001 | +1 h | intent applied, bbox `[2,13,7,18]` (5x5) |
| fleet round 1 | +1.5 h | del `scenario-vessel-1` (I) / `scenario-vessel-3` (II); create `scenario-vessel-4` (I) @ `[25.5,13.5]`, `scenario-vessel-5` (II) @ `[5.5,2.5]`; AIS off on `scenario-vessel-5`, `scenario-vessel-2` |
| focus I0002 | +2 h | intent applied, bbox `[16,25,21,30]` |
| focus I0003 | +3 h | intent applied, bbox `[1,8,6,13]` |
| fleet round 2 | +3 h | del `scenario-vessel-4` (I) / `scenario-vessel-2` (II); create `scenario-vessel-6` (I) @ `[17.5,18.5]`, `scenario-vessel-7` (II) @ `[24.5,3.5]`; AIS `Ship-1` on, `scenario-vessel-5` on |
| focus I0004 | +4 h | intent applied, bbox `[3,22,8,27]` |
| fleet round 3 | +4.5 h | del `scenario-vessel-6` (I) / `scenario-vessel-5` (II); create `scenario-vessel-8` (I), `scenario-vessel-9` (II); AIS `Ship-1`, `scenario-vessel-7` toggled |
| focus I0005 | +5 h | intent applied, bbox `[3,2,8,7]` |

Every operation carries queued->applied->confirmed receipts with
frame-capture ids in `operator.jsonl`. Six replacements and six AIS
state changes confirmed (toggles in both directions across rounds).
`audit.metrics`: `focusAreasConfirmed 5`, `focusAreasWithRedTasks 5`,
`confirmedFleetRounds 3`, `confirmedAisChanges 6`.

## Red-Side Response Evidence (final authoritative frame, `intent_statuses`)

| Intent | bbox | Assigned tasks (sample) | Coverage ratio |
| --- | --- | --- | --- |
| I0001 | `[2,13,7,18]` | `search:0:15:4:21` | 0.08 |
| I0002 | `[16,25,21,30]` | `direction:OBS-...` x3, `partition:18:18:24:30`, `search:12:21:18:29` | 0.08 |
| I0003 | `[1,8,6,13]` | `direction:OBS-...` x2, `partition:0:0:2:15` | 0.44 |
| I0004 | `[3,22,8,27]` | `partition:4:21:12:30`, `search:0:21:4:29` | 0.16 |
| I0005 | `[3,2,8,7]` | `search:2:0:11:5`, `search:2:5:6:13` | 0.20 |

All five focus areas received red-side task assignments
(`focusAreasWithRedTasks 5`). Low coverage ratios reflect the ~259-min
sim window on a 900-cell searchable grid, not missing assignments.

## Main-Process Summary (`main-report/report.json`)

`heavy_triggers 188`, `light_triggers 9`, `llm_success_rate 0.277`,
`coverage_pct 7.8`, `region_changes 36`, `ship_count 5`,
`base_refuel_counts {Base-1: 39, Base-2: 79}`,
`opponent_population_paused_by_manual_edit true` (expected: manual
fleet edits pause automatic releases by design).
146 `allocation_decision` events, 108 `mission_assignment_committed`,
250 model calls attempted / 158 succeeded.

## Mid-Run Defects and Repairs (pre-run, this episode)

The earlier six-hour attempt (`...043634419Z-7-b211abe2`, same seed)
dead-locked in `paused_model` at sim 71. Three defects were fixed in
the working tree before this run:

1. **Impossible passive candidates** — `TaskCatalog` clamped
   direction/investigation bboxes to the whole grid, admitting boxes
   overlapping mainland/islands/storm margins that `plan_search_route`
   could never satisfy. Candidates now migrate to the nearest
   all-searchable bbox via `get_searchable_mask()` and are omitted when
   no legal placement exists (legacy clamp retained for state objects
   without the mask API).
2. **All-or-nothing assignment application** —
   `SimulationEngine.apply_assignment_batch()` returned `False` when any
   single search or standoff route plan failed, rejecting valid
   siblings. Failures are now per-assignment: the bad task is skipped
   (and remains pending), the rest commit atomically, and the committed
   batch is returned to callers.
3. **Paused-model livelock on reassignment cooldown** —
   `build_mission_snapshot()` advertised cooldown-ineligible UAVs as
   `preemptible`; the model kept selecting `UAV-4`, validation rejected
   it, and frozen sim time meant the cooldown could never expire. Two
   defenses: cooldown-ineligible UAVs are filtered from `preemptible`,
   and `retry_blocked_decision()` now bounds operator retries —
   after `max_consecutive_decision_failures` (3) failed retries it
   emits `mission_model_retry_exhausted`, defers the decision, and
   resumes the clock so the next trigger re-decides on an advanced
   snapshot.

In this run the defenses were exercised for real: 14 `mission_model_paused`
events, 38 `mission_model_retry_failed`, 2 `mission_model_retry_succeeded`,
and ~12 bounded deferrals — sim time advanced through every one, and the
run ended `finished` rather than trapped.

## Limitations

- `detected_ships 0`: no blue vessel was classified within the window.
- Aggregate `llm_success_rate` is low because validation-failed model
  outputs and budget-exhausted provider attempts count against it;
  deterministic planning kept assigning tasks throughout.
- Focus-area coverage ratios are modest; coverage accrues with sim
  time and the intent expiry windows (360 min) extend beyond the run.

## Pre-existing Test Debt (post-run cleanup)

Baseline failures observed on clean HEAD and disposition after this work:

- `test_candidate_regions_keep_thirty_kilometres_clear_of_land_base` —
  **fixed**: `CandidateExtractor` mission-candidate paths now reject bboxes
  overlapping `land_mask` (`_land_free`), while `extract_pool` keeps
  whole-grid coverage (`test_candidate_pool_includes_entire_task_grid`
  stays green; the two invariants are intentionally distinct).
- `test_decision_maker_retry_uses_one_frozen_clock_decision_and_recovers` —
  **fixed**: fixture now uses a real `AssignmentBatch` (the prior
  `SimpleNamespace` lacked `.assignments` for `build_decision_record`).
- `probe_batch`-dependent tests in `test_simulation_flow.py` — **fixed**:
  the helper assumed the old `max_active: 12` fleet; it now injects
  synthetic SAR `VisualDetection`s through `ContactStore.ingest_visual`
  to raise enough pending contacts before snapshotting.
- `test_unmatchable_pending_geometry_remains_pending` — passes after the
  assignment-application changes (no dedicated fix needed).

Still red on clean HEAD (lifecycle/coverage-gate semantics drift, left
untouched): `test_lifecycle_rotation_waits_for_time_and_coverage_gate`,
`test_post_coverage_search_assignment_keeps_stale_revisit_swaths`,
`test_freshness_patrol_caps_local_revisit_fleet_size`
(`tests/env/test_simulation_integration.py`).

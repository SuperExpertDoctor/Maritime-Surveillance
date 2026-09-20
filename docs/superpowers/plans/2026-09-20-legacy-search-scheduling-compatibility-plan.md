# Legacy Search Scheduling Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Execute with model `gpt-5.6-luna` and reasoning effort `max`. Do not begin implementation until the user approves this plan.

**Goal:** Restore the stable search-region retention and deterministic idle-UAV reassignment behavior of `8e86e9915053d3e1eeb4e4d84cc1aa87572b1281` while preserving the current mixed-mission scheduler, SAR coverage, zone quotas, control coordinator, safety, replay, and evaluation features.

**Architecture:** `StateManager.search_regions` becomes authoritative for ordinary `kind == "search"` geometry and pending/assigned state. The mission task record remains the audit/control projection, and the existing atomic `apply_assignment_batch()` remains the only installation boundary. Before asking the LLM for new ordinary search regions, the allocator emits a deterministic assignment batch for feasible pending regions; only unmatched capacity becomes the exact new-search budget.

**Tech Stack:** Python 3, dataclasses, NumPy, pytest, existing MissionScheduler/TaskAllocator/ControlCoordinator, JSONL replay pipeline, React/Vite/Playwright acceptance tests.

**Design:** [2026-09-20-legacy-search-scheduling-compatibility-design.md](../specs/2026-09-20-legacy-search-scheduling-compatibility-design.md)

## Global Constraints

- Preserve all current mixed maritime features listed in design §2.
- Apply legacy retention only to ordinary `kind == "search"`; `direction_search` and `investigation` keep their current urgent-task lifecycle.
- Never install a control task outside `SimulationEngine.apply_assignment_batch()` and `ControlCoordinator.assign_tasks_atomically()`.
- Never weaken route, fuel reserve, planning-map-version, generation, ownership, or overlap validation.
- `active + assigned_uav_id=None` is a legal pending Region state; `executing + assigned_uav_id=None` is invalid.
- Pending geometry blocks creation of overlapping new regions but consumes no UAV.
- Existing user changes and unrelated dirty-worktree files must be preserved.
- Every implementation task follows red → focused green → regression green → commit.
- Validation outputs go under `outputs/validation/legacy-search-scheduling/<run-id>/`; never clear `outputs/`.
- A fixture run proves deterministic engine behavior; a live run proves external-model integration. Never report fixture evidence as live evidence.

---

## File and responsibility map

- `src/schedule/state_manager.py`: authoritative assigned/pending/unfinished ordinary-region queries.
- `src/mission/contracts.py`: immutable snapshot fields that publish pending IDs and already-satisfied search capacity.
- `src/schedule/task_allocator.py`: pending edge construction, deterministic matching, and residual new-region budget.
- `src/mission/mission_scheduler.py`: validation boundary for new search selections only.
- `src/mission/coverage_policy.py`: exact coverage constraint from residual capacity and legal new candidates.
- `src/env/simulation.py`: pending batch application and synchronized Region/TaskRecord transitions.
- `src/mission/prompts/mission_scheduler.txt`: tell the model that pending regions are retained and absent from its new-task choice.
- `scripts/validate_legacy_search_scheduling.py`: reproducible short/long validation and JSONL invariant audit.
- `tests/mission/test_legacy_search_scheduling.py`: end-to-end scheduler compatibility contracts.
- Existing focused tests: `test_mission_task_lifecycle.py`, `test_mission_scheduler.py`, `test_coverage_policy.py`, `test_zone_rolling_acceptance.py`, `test_simulation_flow.py`.
- Replay/browser tests: `test_replay_restoration_runner.py`, `tests/replay-restoration.spec.js`, `playwright.replay.config.js`.
- `docs/validation/legacy-search-scheduling/acceptance-report.md`: commands, artifacts, hashes, outcomes, and unresolved live blockers.

### Task 1: Lock the broken-state reproduction and invariants

**Files:**
- Create: `tests/mission/test_legacy_search_scheduling.py`
- Modify: `tests/mission/test_mission_task_lifecycle.py`
- Test: the same files

**Interfaces:**
- Consumes: current `SimulationEngine._close_mission_task()`, `TaskAllocator.build_mission_snapshot()`, `MissionScheduler.validate_selection()`.
- Produces: executable contracts for pending state and the exact 77-minute contradiction.

- [ ] **Step 1: Add a fixture that creates one assigned ordinary search and preempts it**

Reuse `_engine()` and the real assignment setup from `test_search_preemption_keeps_region_and_completion_for_reassignment`. Return `(engine, task_id, uav_id, bbox)` after calling `_close_mission_task(... preserve_search=True)`.

```python
def pending_search_fixture():
    engine = _engine()
    # Select and apply one real feasible ordinary search edge.
    # Record partial completion, then close with reason="preempted".
    return engine, candidate.task_id, edge.uav_id, tuple(candidate.bbox)
```

- [ ] **Step 2: Add failing cross-layer invariant tests**

```python
def test_preempted_ordinary_search_is_pending_not_executing():
    engine, task_id, _uav_id, bbox = pending_search_fixture()
    region = next(r for r in engine.allocator.sm.get_search_regions() if r.id == task_id)
    record = engine._mission_task_records[task_id]
    assert (region.status, region.assigned_uav_id, tuple(region.bbox)) == (
        "active", None, bbox,
    )
    assert (record.status, record.assigned_uav_id) == ("approved", None)

def test_pending_geometry_cannot_be_required_and_rejected_in_same_snapshot():
    engine, task_id, _uav_id, bbox = pending_search_fixture()
    snapshot = engine.allocator.build_mission_snapshot(
        active_tasks=tuple(engine._mission_task_records.values())
    )
    required = set(snapshot.coverage_constraint.must_service_task_ids)
    errors_by_candidate = {
        candidate.task_id: engine.allocator.mission_scheduler.validate_selection(
            selection_for(snapshot, candidate.task_id), snapshot,
        )
        for candidate in snapshot.candidates if candidate.kind == "search"
    }
    assert not any(
        task_id in required and any("overlapping_active_search" in e for e in errors)
        for task_id, errors in errors_by_candidate.items()
    )
```

- [ ] **Step 3: Add an invariant helper for later integration tests**

```python
def assert_search_projection_consistent(engine):
    regions = {r.id: r for r in engine.allocator.sm.get_search_regions()}
    for record in engine._mission_task_records.values():
        if record.kind != "search":
            continue
        if record.status == "executing":
            assert record.assigned_uav_id is not None
            assert regions[record.task_id].assigned_uav_id == record.assigned_uav_id
            assert engine.control_coordinator.active_task(record.assigned_uav_id).task_id == record.task_id
        if record.status == "approved" and record.assigned_uav_id is None:
            assert regions[record.task_id].status == "active"
            assert regions[record.task_id].assigned_uav_id is None
```

- [ ] **Step 4: Run red tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_legacy_search_scheduling.py \
  tests/mission/test_mission_task_lifecycle.py -q --maxfail=3
```

Expected: the existing preemption preservation test passes; at least one new contradiction test fails against current snapshot/validation behavior.

- [ ] **Step 5: Commit tests only**

```bash
git add tests/mission/test_legacy_search_scheduling.py tests/mission/test_mission_task_lifecycle.py
git commit -m "test: reproduce pending search scheduling contradiction"
```

### Task 2: Make ordinary search regions an explicit authoritative view

**Files:**
- Modify: `src/schedule/state_manager.py`
- Modify: `src/mission/contracts.py`
- Modify: `tests/schedule/test_state_manager.py`
- Modify: `tests/mission/test_contracts.py`

**Interfaces:**
- Produces:
  - `StateManager.get_pending_search_regions() -> tuple[Region, ...]`
  - `StateManager.get_assigned_search_regions() -> tuple[Region, ...]`
  - `StateManager.get_unfinished_search_regions() -> tuple[Region, ...]`
  - `MissionSnapshot.pending_search_task_ids: tuple[str, ...]`
- Ordinary search means `region.type == "search"`; do not infer urgent search-like task kinds from Region alone.

- [ ] **Step 1: Add failing query tests**

Create active assigned, active unassigned, completed, stale, and track Regions. Assert sorted immutable results:

```python
assert tuple(r.id for r in sm.get_pending_search_regions()) == ("pending",)
assert tuple(r.id for r in sm.get_assigned_search_regions()) == ("assigned",)
assert tuple(r.id for r in sm.get_unfinished_search_regions()) == ("assigned", "pending")
```

- [ ] **Step 2: Implement the three side-effect-free queries**

Use one private predicate and return tuples sorted by `region.id`. Do not mutate status or assignments during reads.

```python
def get_pending_search_regions(self) -> tuple[Region, ...]:
    return tuple(sorted(
        (r for r in self.search_regions
         if r.type == "search" and r.status == "active" and r.assigned_uav_id is None),
        key=lambda r: r.id,
    ))
```

- [ ] **Step 3: Add the snapshot field with validation and serialization**

Add `pending_search_task_ids: tuple[str, ...] = ()` to `MissionSnapshot`. Normalize to a unique sorted tuple of non-empty strings in `__post_init__`; include it in the prompt payload under `snapshot.pending_search_task_ids`. Old callers remain valid because the default is empty.

- [ ] **Step 4: Run focused tests**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/schedule/test_state_manager.py tests/mission/test_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/schedule/state_manager.py src/mission/contracts.py \
  tests/schedule/test_state_manager.py tests/mission/test_contracts.py
git commit -m "feat: expose authoritative pending search regions"
```

### Task 3: Build deterministic pending-region matching without mutating state

**Files:**
- Modify: `src/schedule/task_allocator.py`
- Modify: `tests/mission/test_legacy_search_scheduling.py`
- Modify: `tests/mission/test_mission_scheduler.py`

**Interfaces:**
- Produces:
  - `TaskAllocator.build_pending_search_batch(now_min, *, active_tasks) -> AssignmentBatch | None`
  - `TaskAllocator.pending_search_match_count(...) -> int`
- Consumes: `_mission_resources()`, `_mission_edges()`, existing minimum-cost matching, UAV generation, Region bbox.

- [ ] **Step 1: Add failing matching tests**

Cover:

- two pending regions/two idle UAVs choose maximum cardinality then minimum transit cost;
- a region with no feasible edge remains pending;
- failed/returning/refueling/busy UAVs are excluded;
- stale generation is copied from the snapshot and later rejected by normal atomic application;
- output is deterministic under reversed Region/UAV insertion order;
- direction/investigation records are never included.

- [ ] **Step 2: Convert pending Regions to candidates without adding them to the LLM candidate list**

Use existing stable task ID and bbox, `kind="search"`, existing record creation time, and feasible UAV IDs derived only from legal edges. Build against the current `planning_map_version`.

- [ ] **Step 3: Reuse the scheduler's exact matcher**

Extract the current private maximum/minimum-cost matching implementation into an internal function callable by both validation and `TaskAllocator`; do not introduce a greedy nearest-neighbor algorithm. Sort tasks and edges before matching.

```python
assignments = match_task_ids(
    pending_task_ids,
    usable_edges,
    expected_generations=dict(snapshot.uav_generations),
)
```

The resulting batch uses a call ID such as `deterministic-pending:<snapshot-id>` and contains no preemption IDs.

- [ ] **Step 4: Keep construction read-only**

Assert that calling `build_pending_search_batch()` twice produces equal assignments and does not change Region, TaskRecord, UAV, coverage-service, or coordinator state.

- [ ] **Step 5: Run focused tests**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_legacy_search_scheduling.py \
  tests/mission/test_mission_scheduler.py -q --maxfail=5
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/schedule/task_allocator.py src/mission/mission_scheduler.py \
  tests/mission/test_legacy_search_scheduling.py tests/mission/test_mission_scheduler.py
git commit -m "feat: match pending search regions deterministically"
```

### Task 4: Apply pending assignments through the existing atomic engine boundary

**Files:**
- Modify: `src/env/simulation.py`
- Modify: `src/schedule/task_allocator.py`
- Modify: `tests/mission/test_simulation_flow.py`
- Modify: `tests/mission/test_mission_task_lifecycle.py`
- Modify: `tests/mission/test_legacy_search_scheduling.py`

**Interfaces:**
- Produces: engine scheduling order `pending reassignment → residual heavy decision`.
- Consumes: `build_pending_search_batch()` and `apply_assignment_batch()`.

- [ ] **Step 1: Add failing atomic reassignment tests**

Test that a preempted partially covered Region is assigned to a different idle UAV with:

- identical task ID, bbox, completion percentage, and SAR history;
- TaskRecord `executing` with the new UAV;
- Region `assigned_uav_id` matching the record;
- a new coverage assignment generation owned by the new UAV;
- a real coverage ControlTask and planned route;
- no second Region object with overlapping geometry.

Also inject route-planning and coordinator-commit failures. Assert the entire pending state remains unchanged and no partial lease/contact/coverage mutation survives.

- [ ] **Step 2: Add a narrow engine method**

```python
def _apply_pending_search_reassignments(self, current_time: float) -> int:
    batch = self.allocator.build_pending_search_batch(
        current_time,
        active_tasks=tuple(self._mission_task_records.values()),
    )
    if batch is None:
        return 0
    return len(batch.assignments) if self.apply_assignment_batch(batch) else 0
```

Do not duplicate route planning or coordinator assignment logic.

- [ ] **Step 3: Place it before LLM snapshot construction**

At a mission scheduling boundary, apply pending reassignment first, then rebuild the active-task tuple and resource snapshot before calculating residual work. Do not reuse the pre-assignment snapshot or information version.

- [ ] **Step 4: Correct lifecycle projection**

On successful ordinary-search assignment, replace the existing record with `executing`, the new UAV ID, and unchanged `created_at_min`; clear `release_reason` and keep `finished_at_min=None`. On preemption, normal return, and recoverable work-controller release, use `approved + None` only when the Region remains active. Never leave `executing + None`.

- [ ] **Step 5: Run focused tests**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_simulation_flow.py \
  tests/mission/test_mission_task_lifecycle.py \
  tests/mission/test_legacy_search_scheduling.py -q --maxfail=5
```

Expected: PASS and the original `test_search_preemption_keeps_region_and_completion_for_reassignment` remains green.

- [ ] **Step 6: Commit**

```bash
git add src/env/simulation.py src/schedule/task_allocator.py \
  tests/mission/test_simulation_flow.py \
  tests/mission/test_mission_task_lifecycle.py \
  tests/mission/test_legacy_search_scheduling.py
git commit -m "feat: reassign pending searches through atomic mission batches"
```

### Task 5: Compute residual zone coverage budget after pending reuse

**Files:**
- Modify: `src/mission/contracts.py`
- Modify: `src/mission/coverage_policy.py`
- Modify: `src/schedule/task_allocator.py`
- Modify: `tests/mission/test_coverage_policy.py`
- Modify: `tests/mission/test_zone_rolling_acceptance.py`
- Modify: `tests/mission/test_legacy_search_scheduling.py`

**Interfaces:**
- `CoverageConstraint.active_search_count` remains the assigned count for schema compatibility.
- Add `reserved_search_count: int = 0` and `matchable_pending_count: int = 0` as backward-compatible fields.
- `required_new_search_count = max(0, desired - active - matchable_pending)`, bounded by the remaining feasible matching cardinality.

- [ ] **Step 1: Add failing budget tests**

Cover these exact tables:

| desired | assigned | matchable pending | expected new |
|---:|---:|---:|---:|
| 10 | 0 | 10 | 0 |
| 10 | 4 | 3 | 3 |
| 4 | 4 | 2 | 0 |
| 4 | 1 | 0 | min(3, feasible new slots) |

Also prove that unmatchable pending geometry remains reserved but does not falsely satisfy parallel capacity.

- [ ] **Step 2: Extend the immutable contract and prompt serialization**

Validate non-negative integers and `active_search_count <= reserved_search_count`. Preserve decoding/fixture compatibility through defaults. Publish both fields so live diagnostics explain why the new-task budget is zero.

- [ ] **Step 3: Filter legal new candidates before zone quota construction**

In `build_mission_snapshot()`, remove ordinary search candidates whose bbox overlaps any unfinished Region, including pending Regions. Apply this filter before:

- prompt-window representative selection;
- feasible-edge construction for new tasks;
- `build_zone_quota_inputs()`;
- `must_service_task_ids` selection.

Urgent direction/investigation tasks retain current policy and are not silently filtered as ordinary searches.

- [ ] **Step 4: Build the residual constraint**

After pending matching (or its dry-run cardinality), pass assigned, reserved, and matchable-pending counts to `build_coverage_constraint()`. `max_slots` for new zone requirements uses residual available UAVs, not the original available fleet.

- [ ] **Step 5: Prove must-service is selectable**

For every snapshot produced by real engine fixtures:

```python
for task_id in snapshot.coverage_constraint.must_service_task_ids:
    assert task_id in snapshot.prompt_task_ids
    assert any(edge.task_id == task_id for edge in snapshot.feasible_edges)
    errors = scheduler.validate_selection(selection_for(snapshot, task_id), snapshot)
    assert not any(error.startswith("overlapping_active_search") for error in errors)
```

- [ ] **Step 6: Run focused tests**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_coverage_policy.py \
  tests/mission/test_zone_rolling_acceptance.py \
  tests/mission/test_legacy_search_scheduling.py -q --maxfail=5
```

Expected: PASS, including first-round ten-search behavior when no pending regions exist.

- [ ] **Step 7: Commit**

```bash
git add src/mission/contracts.py src/mission/coverage_policy.py \
  src/schedule/task_allocator.py tests/mission/test_coverage_policy.py \
  tests/mission/test_zone_rolling_acceptance.py \
  tests/mission/test_legacy_search_scheduling.py
git commit -m "fix: budget new coverage after pending search reuse"
```

### Task 6: Align validator, prompt, and failure semantics with add-only search planning

**Files:**
- Modify: `src/mission/mission_scheduler.py`
- Modify: `src/mission/prompts/mission_scheduler.txt`
- Modify: `src/schedule/task_allocator.py`
- Modify: `src/env/simulation.py`
- Modify: `tests/mission/test_mission_scheduler.py`
- Modify: `tests/mission/test_coverage_model_failure.py`
- Modify: `tests/mission/test_legacy_search_scheduling.py`

**Interfaces:**
- LLM selected task IDs contain only new candidates and urgent current candidates, never pending ordinary searches.
- Pending reassignment is deterministic and does not count as an LLM success/failure.

- [ ] **Step 1: Add failing prompt and validator tests**

Assert the system prompt contains all of:

```text
pending_search_task_ids are retained work
do not select or recreate pending search regions
selected ordinary searches are additions only
required_new_search_count is the residual addition count
```

Assert pending task IDs are absent from selectable candidates but present in `pending_search_task_ids` and active audit records.

- [ ] **Step 2: Narrow overlap validation**

Continue rejecting every newly selected bbox that overlaps assigned or pending unfinished geometry. Do not treat a pending record as a selected task through `_task_maps()`. The validator must report one clear overlap error, never pair it with `coverage_oldest_not_selected` caused by the same illegal must-service choice.

- [ ] **Step 3: Preserve no-op scheduling semantics**

If deterministic pending assignment consumes all residual capacity, return an explicit light/non-model result such as:

```python
{"trigger_type": "light", "action": "pending_searches_reassigned", "assignments": count}
```

Do not call the LLM and do not increment `_decision_failure_streak`. If residual capacity requires new work, proceed to the current heavy decision path.

- [ ] **Step 4: Test LLM failure after successful pending reassignment**

Force the residual new-region model call to fail. Assert:

- pending assignments remain installed;
- existing regions remain present;
- the model failure counter reflects only the failed external decision;
- no rollback or duplicate assignment occurs on retry.

- [ ] **Step 5: Run focused tests**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_mission_scheduler.py \
  tests/mission/test_coverage_model_failure.py \
  tests/mission/test_legacy_search_scheduling.py -q --maxfail=5
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mission/mission_scheduler.py \
  src/mission/prompts/mission_scheduler.txt src/schedule/task_allocator.py \
  src/env/simulation.py tests/mission/test_mission_scheduler.py \
  tests/mission/test_coverage_model_failure.py \
  tests/mission/test_legacy_search_scheduling.py
git commit -m "fix: make model search planning additive only"
```

### Task 7: Complete lifecycle cleanup, telemetry, and frame/replay projection

**Files:**
- Modify: `src/env/simulation.py`
- Modify: `src/vis/backend/frame_builder.py` only if current Region serialization cannot express pending unambiguously
- Modify: `tests/mission/test_coverage_failure_cleanup.py`
- Modify: `tests/env/test_mixed_frame.py`
- Modify: `tests/vis/test_replay_adapter.py`
- Modify: `tests/mission/test_legacy_search_scheduling.py`

**Interfaces:**
- Produces events `pending_search_created` and `pending_search_reassigned` only if existing `mission_task_released/mission_decision` events cannot convey the transition without inference.
- Frame Region remains `status="active", assigned_uav_id=null`; no breaking frame schema change is required.

- [ ] **Step 1: Add transition matrix tests**

Exercise ordinary-search transitions caused by:

- probe/track preemption → pending;
- normal lifecycle return with incomplete SAR → pending;
- coverage complete → completed;
- unrecoverable controller/UAV failure → pending if another feasible UAV exists, otherwise still pending until an explicit map infeasibility audit marks stale;
- track-region overlap retirement → stale/blocked, not pending;
- map change with no healthy feasible edge → stale/blocked with reason;
- temporary lack of idle UAV → remain pending.

- [ ] **Step 2: Centralize Region/record projection**

Introduce one helper in `SimulationEngine`, for example:

```python
def _set_search_task_projection(
    self, task_id: str, *, state: Literal["pending", "executing", "completed", "stale"],
    uav_id: str | None, current_time: float, reason: str | None,
) -> None:
    ...
```

All ordinary-search close/reassign/complete paths use it. It must reject illegal combinations immediately.

- [ ] **Step 3: Add runtime invariant diagnostics**

At frame/status publication, validate without mutating:

- no executing search with null/non-operational UAV;
- no Region/record assignee mismatch;
- no UAV bound to two mission records;
- no two unfinished ordinary Regions overlap unless they are the same stable task ID.

In tests, violations raise. In production, emit one structured `mission_state_invariant_failed` event and fail closed rather than silently continuing.

- [ ] **Step 4: Verify visual projection**

Frames and replay must show pending regions continuously, with no UAV label/route attached until reassignment. After reassignment, the same Region ID gains the new UAV assignment; it must not disappear/reappear as a different rectangle.

- [ ] **Step 5: Run focused backend/replay tests**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_coverage_failure_cleanup.py \
  tests/mission/test_legacy_search_scheduling.py \
  tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py -q --maxfail=5
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/env/simulation.py src/vis/backend/frame_builder.py \
  tests/mission/test_coverage_failure_cleanup.py \
  tests/mission/test_legacy_search_scheduling.py \
  tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py
git commit -m "fix: unify search lifecycle and replay projection"
```

### Task 8: Add reproducible validation runner and browser acceptance

**Files:**
- Create: `scripts/validate_legacy_search_scheduling.py`
- Modify: `tests/mission/test_replay_restoration_runner.py`
- Modify: `src/vis/frontend/tests/replay-restoration.spec.js`
- Create: `docs/validation/legacy-search-scheduling/acceptance-report.md`

**Interfaces:**
- CLI:

```text
python scripts/validate_legacy_search_scheduling.py \
  --seed 42 --steps 120 --transport fixture --output-dir <new-dir>
python scripts/validate_legacy_search_scheduling.py --check-log <frames.jsonl>
```

- Produces `manifest.json`, `frames.jsonl`, `events.jsonl`, `metrics.json`, `audit.json`.

- [ ] **Step 1: Write failing runner tests**

The checker must reject logs containing:

- `executing` ordinary record with null assignee;
- Region/record/UAV assignment mismatch;
- selected must-service task rejected as active overlap;
- duplicate/overlapping unfinished ordinary Regions;
- repeated Region disappearance/recreation across a pending interval;
- `runtime_status=paused_model` caused by the contradiction under test;
- incomplete requested steps without a recorded terminal reason.

- [ ] **Step 2: Implement the runner by reusing production scenario/frame code**

Reuse `scripts.persistent_coverage_scenarios` and replay artifact writers; do not synthesize frames. Record seed, requested/completed steps, transport, git SHA, config hash, input hashes, role bindings, final sim minute, event counts, Region ID continuity, pending durations, reassignment counts, coverage metrics, model failures, and operational failures.

- [ ] **Step 3: Add deterministic scenario assertions**

The fixture provider must cause at least one ordinary-search preemption and later release an idle UAV so the same task ID is reassigned. The audit requires:

```text
pending_search_intervals >= 1
pending_search_reassignments >= 1
projection_invariant_failures == 0
must_service_overlap_contradictions == 0
completed_steps == requested_steps
```

- [ ] **Step 4: Add browser replay checks**

Using the real runner JSONL through the existing replay server, Playwright must:

- seek to before preemption, pending interval, and after reassignment;
- assert the same Region ID and bbox remain visible in all three frames;
- assert assigned UAV changes to null and then the successor UAV;
- assert SAR coverage panel advances from authoritative frame data;
- assert probe/track markers, vessel/contact layers, routes, event drawer, timeline, and paused/running status render without console errors;
- capture screenshots at 1440×1000 into the validation output.

- [ ] **Step 5: Run short acceptance**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_replay_restoration_runner.py \
  tests/env/test_replay_acceptance_server.py -q
python scripts/validate_legacy_search_scheduling.py \
  --seed 42 --steps 120 --transport fixture \
  --output-dir outputs/validation/legacy-search-scheduling/fixture-42-120
python scripts/validate_legacy_search_scheduling.py --check-log \
  outputs/validation/legacy-search-scheduling/fixture-42-120/frames.jsonl
npm --prefix src/vis/frontend run build
cd src/vis/frontend && \
REPLAY_ACCEPTANCE_DIR="$OLDPWD/outputs/validation/legacy-search-scheduling/fixture-42-120" \
npx playwright test --config playwright.replay.config.js \
  --grep 'pending search|search reassignment|coverage panel'
```

Expected: all commands exit 0; screenshots visibly preserve the region across the pending interval.

- [ ] **Step 6: Commit**

```bash
git add scripts/validate_legacy_search_scheduling.py \
  tests/mission/test_replay_restoration_runner.py \
  src/vis/frontend/tests/replay-restoration.spec.js \
  docs/validation/legacy-search-scheduling/acceptance-report.md
git commit -m "test: validate legacy search scheduling and replay continuity"
```

### Task 9: Final full regression and long-duration runtime acceptance

**Files:**
- Modify: `docs/validation/legacy-search-scheduling/acceptance-report.md`
- Modify only the owning source/test files if this gate exposes a defect; return to that task's red/green cycle before rerunning the gate.

**Interfaces:**
- Consumes all prior tasks.
- Produces the final evidence package. This task is not complete after unit tests; all long runs and replay checks below are mandatory unless an external credential is genuinely unavailable and explicitly reported as blocked.

- [ ] **Step 1: Run formatting/static sanity and focused regression**

```bash
git diff --check
python -m compileall -q src scripts/validate_legacy_search_scheduling.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/mission/test_legacy_search_scheduling.py \
  tests/mission/test_mission_task_lifecycle.py \
  tests/mission/test_mission_scheduler.py \
  tests/mission/test_coverage_policy.py \
  tests/mission/test_zone_rolling_acceptance.py \
  tests/mission/test_coverage_model_failure.py \
  tests/mission/test_coverage_failure_cleanup.py \
  tests/mission/test_simulation_flow.py \
  tests/control tests/schedule \
  tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py -q --maxfail=1
```

Expected: zero failures.

- [ ] **Step 2: Run the complete Python suite and frontend suite**

```bash
LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -q --maxfail=1
npm --prefix src/vis/frontend run build
npm --prefix src/vis/frontend run test:acceptance
```

Expected: zero failures; record exact pass counts and durations.

- [ ] **Step 3: Run mandatory fixture soak matrix**

Run all to completion in new directories:

```bash
python scripts/validate_legacy_search_scheduling.py --seed 42  --steps 480 --transport fixture --output-dir outputs/validation/legacy-search-scheduling/fixture-42-480
python scripts/validate_legacy_search_scheduling.py --seed 101 --steps 480 --transport fixture --output-dir outputs/validation/legacy-search-scheduling/fixture-101-480
python scripts/validate_legacy_search_scheduling.py --seed 202 --steps 720 --transport fixture --output-dir outputs/validation/legacy-search-scheduling/fixture-202-720
```

For every output run `--check-log`. Required gates:

```text
completed_steps == requested_steps
runtime_status != paused_model at terminal frame
mission_state_invariant_failed == 0
must_service_overlap_contradictions == 0
executing_without_assignee == 0
assignment_projection_mismatches == 0
duplicate_unfinished_search_overlaps == 0
pending_search_intervals >= 1 across the matrix
pending_search_reassignments >= 1 across the matrix
terminal JSONL parses fully with strictly increasing frame_id/sim_time
persistent coverage sample count equals completed simulated minutes represented
```

Operational safety failures such as `no_safe_recovery_path` are not silently ignored: each must be listed by UAV, minute, map version, pose, remaining range, and independent cause. A safety failure may not be relabeled as a scheduling success.

- [ ] **Step 4: Reproduce the original seed and compare the former failure window**

Run seed 42 past minute 79 and inspect minutes 70–100. Assert:

- no three-cycle chain of `model_selection_unavailable/model_selection_unavailable/decision_deadline_exceeded` caused by impossible search constraints;
- every `must_service_task_id` was legal in its frozen snapshot;
- Region IDs and bboxes survive preemption;
- available UAVs preferentially take pending regions before new ordinary Regions appear;
- coverage continues updating after minute 79.

- [ ] **Step 5: Run live-model long acceptance with explicit `gpt-5.6-luna`, effort `max` execution context**

Use the repository's configured LongCat/live transport for simulation decisions; `gpt-5.6-luna` with `max` is the implementation agent, not a substitution for the simulator's configured provider. With valid credentials:

```bash
python scripts/validate_legacy_search_scheduling.py \
  --seed 42 --steps 480 --transport live \
  --output-dir outputs/validation/legacy-search-scheduling/live-42-480
python scripts/validate_legacy_search_scheduling.py --check-log \
  outputs/validation/legacy-search-scheduling/live-42-480/frames.jsonl
```

Required: completes all 480 steps without the old contradictory validation chain or unexplained pause. Record decision latency p50/p95/max, failures by category, prompt bytes, candidate count, and all retries. If credentials/provider capacity are unavailable, mark this single gate `BLOCKED` with the exact external error; do not claim live verification from fixture runs.

- [ ] **Step 6: Run final browser acceptance on a 480-step artifact**

```bash
cd src/vis/frontend && \
REPLAY_ACCEPTANCE_DIR="$OLDPWD/outputs/validation/legacy-search-scheduling/fixture-42-480" \
npx playwright test --config playwright.replay.config.js
```

Manually inspect and document screenshots for minutes 1, preemption, pending, reassignment, 79, 120, and terminal. Required visual effects:

- stable search rectangles without flicker/recreation;
- assigned UAV labels/routes detach and reattach correctly;
- probe, track, vessel, contact, obstacle/weather, bases, SAR footprints, coverage panel, events and timeline remain visible and truthful;
- replay seek and playback work at beginning/middle/end;
- no browser console/page errors or layout overflow at the configured viewport.

- [ ] **Step 7: Publish the acceptance report**

The report must include:

- approved design and plan SHAs;
- implementation commit list;
- exact commands, timestamps, durations, pass counts and exit codes;
- artifact paths and SHA-256 hashes;
- per-run seed/config/provider/requested/completed steps;
- invariant audit table;
- coverage and task-continuity metrics;
- model timing/failure table;
- safety failures kept separate from scheduler failures;
- screenshot links and manual visual conclusions;
- explicit PASS/BLOCKED/FAIL for fixture soak, live soak, full tests, and browser acceptance.

- [ ] **Step 8: Final commit**

```bash
git add docs/validation/legacy-search-scheduling/acceptance-report.md
git commit -m "docs: publish legacy search scheduling acceptance evidence"
```

**Final Gate:** Do not declare completion unless all non-external gates pass, the fixture soak matrix reaches every requested terminal step, the original minute-79 failure window is crossed cleanly, the JSONL invariant checker reports zero state contradictions, and the real replay visually demonstrates stable region retention and reassignment. Live-model verification must either pass or remain transparently BLOCKED by an external credential/provider condition.

---

## Plan self-review checklist

- Design §§1–12 are mapped to Tasks 1–9.
- Ordinary search is distinguished from direction/investigation work.
- Pending assignment never bypasses the atomic controller boundary.
- Pending geometry both blocks overlap and consumes no UAV.
- Zone quota is calculated only after pending capacity reuse.
- Prompt and validator describe the same add-only contract.
- Failure semantics do not disguise navigation/safety faults.
- Backend state, JSONL, replay, browser visuals, short runs, 480-step and 720-step soak runs are all tested.
- Fixture and live evidence are reported separately.
- No implementation starts before user review and approval.

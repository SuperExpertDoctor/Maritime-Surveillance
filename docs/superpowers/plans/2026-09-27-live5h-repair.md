# Live Five-Hour Replay Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Execute the user-approved design without further design checkpoints.

**Goal:** Repair the control, scheduling, model-budget, evaluation and interaction defects exposed by the September 26 five-hour live replay.

**Architecture:** Preserve the existing simulation engine, command queues, controller contracts and UI workflows. Add regression coverage at the real component boundaries and repair each owner, rather than changing replay files or fabricating decisions. Keep physical observation and evaluation truth separate.

**Tech Stack:** Python, pytest, NumPy, React, Vite, Playwright.

## Global Constraints

- Vessel edits and focus submissions must use the existing live command/interaction flows in the same episode, not offline configuration edits.
- Preserve classification evidence requirements; never infer an identification from navigation progress or fabricate LLM decisions.
- Ordinary search may consume at most 80% of healthy resources when executable probe demand exists; without such demand full search utilization remains allowed.
- A successful manual vessel create/delete pauses automatic replenishment for the rest of that episode; invalid edits and automatically created vessels do not trigger that pause. Episode reset restores configured automatic population behavior.
- Preserve existing validated tasks on model failure. All LLM roles have bounded total request time and retries must not exceed the remaining deadline.
- Command application must be observable before subsequent slow model work; create, delete and AIS UI controls wait for authoritative frames.
- Evaluation truth links remain evaluator-only and must never leak to blue-side observations or model prompts.
- No new production dependencies, no unrelated refactors, no real provider calls in automated tests, no changes to historical replay artifacts.
- Run Python tests with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider`.
- Use TDD: record expected failing regression output before production edits, then passing focused and related suites. Commit each task on `fix/live5h-repair` only.

## Audit Evidence

Replay: `outputs/simulation_20260926_170835.jsonl` in the original checkout. It spans 18000 seconds wall time but only 177 simulation minutes. Nine focus submissions were correct 25-cell areas; hourly edits eventually applied but were delayed. A probe entry endpoint was outside the 30x30 map and persisted under repeated safety intervention. Twenty-one EO samples never passed the strict baseline range band. Removing the vessel caused airborne HOLDING to be treated as a base landing and teleported/refuelled the UAV. Existing search reservations suppressed focus candidates, and 100% search floors delayed probe allocation. Most normal LLM prompts had no actionable candidates. EO-only associations were omitted from evaluation links.

### Task 1: Safe Probe Acquisition and Recovery

**Files:** `src/utils/track_orbit.py`, `src/control/heuristic/tracking.py`, `src/control/heuristic/probe.py`; regression tests in `tests/control/heuristic/test_probe.py`, `tests/mission/test_probe_navigation_integration.py`, and existing tracking tests as needed.

**Interfaces:** Consume `ControlObservation.planning_obstacle_mask`, map version, observed contacts, frozen probe session and controller events. Keep `ControlTask`/`ControlDecision` unchanged. Optional checked-route parameters must preserve existing tracker callers and test doubles.

- [x] Add failing real-geometry tests proving orbit entry poses and connecting segments remain within the map and outside obstacles, including a contact near the east boundary. If no legal entry exists, return an explicit recoverable failure instead of installing an invalid route.
- [x] Add failing tests for a moving contact during orbit entry, changed planning map, and a stalled entry/safety intervention. Verify bounded replanning and route revisions rather than permanent `_orbit_entry_active` suppression.
- [x] Add a controller/engine integration test proving baseline evidence acquisition and progression for a reachable target, with actual post-move EO samples. Preserve the range band and evidence duration. A route endpoint alone is not acceptance.
- [x] Implement legal candidate filtering in orbit planning and bounded recovery using existing observable state. Avoid permanent failure on transient geometry. Correct pre-/post-move EO gating and guidance radius where demonstrated by the regression. Do not relax evidence requirements.
- [x] Run focused regression tests red, then green; run `tests/control/heuristic/test_probe.py tests/mission/test_probe_navigation_integration.py tests/mission/test_probe_session.py` and related tracking/navigation tests.
- [x] Commit and report RED/GREEN output, files, remaining physical limitations, and changed public interfaces.

### Task 2: Lifecycle, Manual Population and EO Evaluation

**Files:** `src/env/simulation.py`, `src/mission/opponent_population.py`, existing vessel command contracts only if required. Tests: `tests/mission/test_dynamic_vessel_lifecycle.py`, `tests/mission/test_vessel_commands.py`, `tests/mission/test_opponent_runtime.py`, `tests/mission/test_outcome_evaluator.py`.

**Interfaces:** Existing runtime vessel queue and receipts, `_holding_base_by_uav`, real EO ingestion return values, `_evaluation_contact_links`, episode reset.

- [x] Reproduce vessel removal followed by `_process_refuelling`: an airborne holding aircraft far from a base must neither teleport nor refuel. Test valid landing queue release and arrival remains functional.
- [x] Require a registered landing queue/base and physical arrival before refuelling. Keep ordinary airborne HOLDING separate from landing HOLDING and clean stale queue entries through existing lifecycle paths.
- [x] Reproduce automatic population undoing successful manual count edits. Distinguish manual and automatic command provenance using existing fields where possible; pause automatic population on successful manual create/delete only. Reset the pause per episode; expose/log the state through existing event/serialization patterns.
- [x] Reproduce a true EO-only observation with no preceding SAR: the real engine ingestion must create an evaluator-only physical-vessel/contact association. Verify observed/identified accounting and incorrect associations still count correctly without exposing truth to the model.
- [x] Run focused failures then the four test files above plus affected refuelling/evaluation tests, all passing.
- [x] Commit and report RED/GREEN evidence and any new observable state or command provenance interface needed by Task 5.

### Task 3: Focus Ownership, Scheduling Priority and Actionable Work

**Files:** `src/schedule/task_allocator.py`, `src/mission/task_catalog.py`, `src/mission/coverage_policy.py`, `src/mission/mission_scheduler.py`, `src/mission/intent_store.py`, `src/env/simulation.py`, coverage controller/config only when necessary. Tests: `tests/mission/test_intent_candidates.py`, `tests/mission/test_task_catalog.py`, `tests/mission/test_coverage_prompt_window.py`, `tests/mission/test_mission_scheduler.py`, `tests/mission/test_intent_store.py`, `tests/mission/test_coverage_model_failure.py`, `tests/mission/test_coverage_decision_budget.py`.

**Interfaces:** Production `extract_pool` -> task catalog -> mission snapshot -> scheduler prompt. Existing immutable task records, controller route updates and intent serialization.

- [x] Reproduce focus fully and partially overlapping unfinished search reservations through the production snapshot path. Preserve single ownership: attach focus to existing owners and prioritize/replan their scan route; uncovered focus portions remain eligible candidates.
- [x] Preserve submission to the LLM: a new focus must reach a meaningful model review, even with an existing owner. Owner reprioritization is actionable; do not replace the human-to-LLM workflow with local-only bookkeeping or repeat empty calls after acknowledgement. Prefer existing strict selection contracts; report a required new schema before introducing it.
- [x] Reproduce equal SAR freshness with higher focus utility losing prompt admission. Preserve human priority in candidate/partition selection, without discarding coverage data or duplicating reservations.
- [x] Reproduce ten healthy aircraft occupied by search with an executable probe. Cap ordinary search at 80% while demand exists, allowing legal preemption; without executable probe demand retain existing full search utilization. Exercise infeasible/blocked probe demand so it does not reserve unusable capacity.
- [x] Cover small-fleet rounding and new ordinary-search admission, not merely a lower floor. Preserve existing valid tasks while making legal probe preemptions available; never abort searches due to model failure.
- [x] Reproduce candidates with no usable assignment edges still causing an LLM call. Share actionable-candidate filtering with prompt construction, skipping truly empty decisions before calling the gateway, and retaining calls for legal probe/focus work.
- [x] Correct intent statuses using actual ownership/candidate/resource evidence: distinguish no legal candidate, resource blocked, and waiting assignment. An overlapping active owner must not report no candidate.
- [x] Run focused failures then the named scheduler/intent suites and coverage progress tests, all passing.
- [x] Diagnose the pre-existing V07 first-step missing probe in `tests/mission/test_feature_control_integration.py::test_v07_real_eo_samples_classify_then_handoff_and_eo_lock`. Inspect the real snapshot and fixture selection, repair an in-scope defect or stale fixture, and preserve classification/handoff assertions.
- [x] Commit and document route update/status contracts for the UI integration task.

### Task 4: Uniform LLM Deadlines and Useful Retry Budgets

**Files:** `src/mission/llm_gateway.py`, role clients/config if necessary. Tests: `tests/mission/test_llm_gateway.py`, `tests/mission/test_coverage_decision_budget.py`, existing red/contact/reviewer tests.

**Interfaces:** `request_json` caller-provided deadline, gateway role bindings and transport timeout. Preserve model identity, strict JSON validation, failure logging, and validated-task preservation.

- [x] Add fake-clock transport tests proving red commander, contact assessor and reviewer calls without explicit caller deadlines still have one finite total budget including all retries. Explicit shorter deadlines always win.
- [x] Reproduce truncated output followed by a retry with less than a useful response budget. Stop before a hopeless retry, report exhaustion, and never exceed the original total deadline. Keep meaningful retries possible when sufficient time remains.
- [x] Keep connectivity probes bounded and independent. Avoid unbounded provider defaults and hidden per-attempt multiplication. Use existing timeout/config conventions with a documented total default.
- [x] Run focused failures then gateway, decision-budget and role-client suites, all passing; verify no external calls were made.
- [x] Commit and report exact default budgets and backward compatibility notes.

### Task 5: Immediate Mutation Frames and UI Confirmation

**Files:** `main.py`, `src/env/simulation.py`, frame publication code only as needed, `src/vis/frontend/src/App.jsx` and existing UI receipt helpers. Tests: `tests/test_live_runtime_loop.py`, `tests/env/test_frame_publisher.py`, `src/vis/frontend/tests/vessel-interactions.spec.js`, related intent/mixed-maritime browser tests.

**Interfaces:** Existing command queues, receipts, authoritative frame revisions and runtime phases. Consume manual population and intent status behavior from Tasks 2/3.

- [x] Add a runtime test which queues a vessel/focus mutation then blocks the following model call. The applied mutation and receipt must already be published in an authoritative frame, not delayed until the step completes.
- [x] Publish a coherent command-boundary frame before subsequent model work, without an extra simulation tick or step-delay sleep, while preserving paused/reset/retry behavior and event/recorder semantics.
- [x] Browser regression: create, delete and AIS commands remain busy until an authoritative frame confirms the requested result. Deletion must retain the vessel identity and wait for confirmed absence; stale receipts/frames cannot unblock a later episode's command.
- [x] Browser regression for focus submission through the existing 5x5 map interaction and owner/status display. Do not redesign the UI.
- [x] Run focused Python failures then green and related runtime/frame suites. Install frontend dependencies using the lockfile; run relevant Playwright tests and production build. Check desktop/mobile for interaction regressions if the existing test harness supports them.
- [x] Commit and report test outputs, UI screenshots/artifacts, and how to run the repaired app.

## Final Verification

- [x] Attempt the full Python suite and record its existing credential-dependent failure separately: 284 passed, 1 failed (fail-fast). Final affected offline suite: 751 passed, 50 deselected. Frontend build and 48 browser tests passed. Full-repository green is not claimed.
- [x] Request an independent whole-branch review against this plan; fix all Critical/Important findings and re-run covering tests. All Minor coverage suggestions also addressed; final production commit 27a96be independently approved.
- [x] Write a validation report mapping each audit finding to code/tests and distinguish accelerated/offline verification from a new real-time five-hour provider-backed acceptance run. See docs/validation/2026-09-27-live5h-repair.md.
- [x] Preserve the worktree and repair branch for user review; do not merge or push without request.

# Replay Restoration Execution Log

This log records evidence for the replay-restoration implementation plan. A
line is added for each task only after its task gate has been checked. Paths
under `outputs/validation/replay-restoration/` are ignored runtime artifacts;
the source logs listed below are read-only inputs.

## T00 Baseline

- Implementation branch: `feature/replay-visual-restoration`
- Starting `branch1` commit: `5cca8b8`
- Design evidence baseline commit: `bc8d76f`
- Python baseline command:
  `LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/mission/test_simulation_flow.py tests/mission/test_mission_scheduler.py tests/control/test_simulation_ownership.py tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py -q`
- Python baseline result: `32 passed in 11.66s`
- Historical input directory: `/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/outputs`
- Historical input SHA-256 and size/frame counts:
  - `simulation_20260812_200432.jsonl`: `8759a25cd8e14f17a63b26df74d0359b0a1ca90de62f0efc015c73453abc32aa`, 190302543 bytes, 471 frames.
  - `simulation_20260916_204010.jsonl`: `ff07850296485e2a2596a26639b936eff0f8bfae93743aa4908809c50367957e`, 1281703 bytes, 20 frames.
  - `simulation_20260916_210604.jsonl`: `6a9800c21139039d9c0907e795e55da692cec90f21c432cfbc4465b6849b8ebe`, 1302263 bytes, 20 frames.
- Historical statistics agree with `baseline-manifest.json`: old final coverage is
  `56.35528330781011`; both new logs end at `0.0`. The old final frame has 9
  non-empty planned paths and 10 non-empty mission routes; each new final frame
  has zero of both.
- Existing frontend build: passed (`vite v8.2.0`, 1508 modules).
- Existing frontend acceptance: `1 passed, 1 failed`; the pre-existing failure is
  `tests/acceptance.spec.js:143`, where the I-class vessel button remained
  disabled after connection. The sensor-beam test passed. This remains an open
  baseline item until the responsible task supplies a regression result.
- Baseline screenshots: `t00-baseline/legacy-t20.png`,
  `t00-baseline/current-t20.png`, and `t00-baseline/sensor-beams-live.png`.
- Browser render timing baseline: `t00-baseline/performance.json` contains 500
  samples from Chromium `151.0.7922.34` at a 900x700 canvas. P50 is
  `0.6000003814697266 ms` and P95 is `17.5 ms`; the same frame set and browser
  command will be reused at T15.

## Status Vocabulary

`PASS` means the task gate has fresh evidence. `OPEN` means a baseline or
environment issue is recorded but not silently counted as a pass. `N/A` is used
only when the plan explicitly defines a capability as compatibility or an
extension interface.

## T01 Probe Transaction

- Red evidence: the new six-assignment test initially observed only one unique
  probe ID (`P0001`) for six assignments.
- Implemented local probe-ID allocation, same-owner continuation reuse,
  owner-conflict validation, contact reservation rollback, and pre-commit
  handoff deadline validation.
- Gate command:
  `python -m pytest tests/mission/test_simulation_flow.py tests/mission/test_contact_store.py tests/schedule/test_state_manager.py tests/mission/test_handoff.py -q`
- Gate result: `70 passed in 21.29s`.
- Covered evidence includes six concurrent sessions, allocation across two
  batches, continuation phase preservation, invalid-batch rollback, expired
  handoff rejection before lease installation, reservation snapshot restore,
  same-owner session updates, and reset isolation.

## T02 Mission Task Lifecycle

- Red evidence: holding left the previous probe record assigned to the UAV;
  search preemption also needed to preserve the region's completion percentage
  while clearing its assignment.
- Implemented the idempotent `SimulationEngine._close_mission_task` boundary.
  It synchronizes mission records, coordinator/state ownership, probe sessions,
  contact/track reservations, search regions, and one release event without
  touching a replacement task.
- Added coverage for holding, repeated closure, search preemption, probe
  reservation transition, task failure, range return, target release, and
  vessel removal paths through the existing lifecycle regressions.
- Focused regression after the final implementation patch:
  `LONGCAT_API_KEY=offline-test python -m pytest tests/mission/test_simulation_flow.py tests/mission/test_mission_task_lifecycle.py tests/mission/test_contact_release.py tests/mission/test_dynamic_vessel_lifecycle.py -q`
  -> `70 passed in 331.80s`.
- Gate command:
  `LONGCAT_API_KEY=offline-test python -m pytest tests/mission/test_mission_task_lifecycle.py tests/mission/test_contact_release.py tests/mission/test_dynamic_vessel_lifecycle.py tests/control/test_simulation_ownership.py tests/control/heuristic/test_task_flow.py tests/schedule/test_trigger_manager.py -q`
- Gate result: `96 passed in 319.34s`.

## T03 Work-Conserving Selection

- Red evidence: a model could return an empty selection with a defer reason,
  or select one task while an independent task could use an idle UAV; hidden
  prompt candidates also had no validation boundary.
- Split selection validation from the previous legal-work shortcut. The public
  and `MissionScheduler` validators now accept a fixed `visible_task_ids` set,
  permit only visible candidates or approved active continuations, and run a
  bounded augmentation check after basic legality succeeds.
- Augmentation uses the full immutable edge graph, allows rematching selected
  tasks, requires no extra preemption, and reports the first deterministic
  visible witness only when a newly used UAV is in `available_uav_ids`.
  A defer reason no longer overrides such a witness; an empty selection with
  no feasible edge remains legal.
- Red-to-green commands:
  `python -m pytest tests/mission/test_mission_scheduler.py::test_reason_does_not_allow_empty_selection_with_idle_feasible_work tests/mission/test_mission_scheduler.py::test_partial_selection_must_add_independent_idle_work tests/mission/test_mission_scheduler.py::test_underutilization_allows_rematching_selected_work_to_use_idle_uav tests/mission/test_mission_scheduler.py::test_underutilization_does_not_force_work_without_an_idle_resource tests/mission/test_mission_scheduler.py::test_hidden_candidate_cannot_be_selected_or_create_underutilization_witness -q`
  -> `5 passed in 0.32s`.
- Gate command:
  `python -m pytest tests/mission/test_mission_scheduler.py tests/mission/test_prompt_window.py tests/mission/test_intent_candidates.py -q`
- Gate result: `31 passed in 1.63s` (including the prompt-hidden selection
  regression).

## T04 Model Boundary and Failure Visibility

- Red evidence: the deterministic evaluation gateway selected only the first
  feasible candidate, so T03 correctly rejected it as underutilized; mission
  failures emitted only the legacy `decision_failed` event without a stable
  failure category payload.
- Kept the production `LLMGateway` correction limit unchanged and added the
  structured `mission_selection_failed` event with snapshot ID, failure
  category, validator error codes, and available-resource count. Existing
  `decision_failed` retry behavior remains intact.
- Restricted fixture expansion to the visible candidate payload. It iterates
  at most one round per visible candidate, accepts only a clean selection or
  errors consisting solely of `underutilized_feasible_work:*`, and performs a
  final validator pass. The contact assessor remains explicitly `unknown` and
  uses only supplied sample IDs.
- Red-to-green tests:
  `python -m pytest tests/mission/test_simulation_flow.py::test_failed_mission_decision_emits_failure_and_retries_after_one_minute tests/mission/test_evaluation_cli.py::test_fixture_gateway_expands_selection_until_validator_accepts -q`
  -> `2 passed in 2.14s`.
- Gate command:
  `LONGCAT_API_KEY=offline-test python -m pytest tests/mission/test_llm_gateway.py tests/mission/test_failure_paths.py tests/mission/test_evaluation_cli.py tests/mission/test_mission_scheduler.py -q`
- Gate result: `104 passed in 37.78s`. No live model or credential was used.

## T05 Immutable Route Snapshot Contract

- Red evidence: controllers had no common route export, and StateManager had
  no episode/generation/revision boundary for cached route data.
- Added frozen `ControlRouteSnapshot` and `UavRouteSnapshot` contracts with
  finite nested poses, bounded next indexes, non-negative revisions/generations,
  and explicit ready/pending/guidance-only/unavailable/cleared statuses.
- Added the optional `ControllerBase.route_snapshot()` default, a locked
  `ControlCoordinator.route_snapshot()` that wraps the current StateManager
  episode and lease generation, and monotonic StateManager route publication.
  Cleared snapshots retain the revision floor so an old route cannot revive.
- Red-to-green command:
  `python -m pytest tests/control/test_route_snapshot.py -q`
  -> `4 passed` after the stale-cleared error correction.
- Gate command:
  `python -m pytest tests/control/test_route_snapshot.py tests/control/test_base_classes.py tests/control/test_coordinator.py tests/control/test_ownership.py -q`
- Gate result: `39 passed in 1.09s`. Snapshot reads do not call navigation or
  execution and learning controllers remain unforced.

## T06 Authoritative Heuristic Routes

- Red evidence: all five heuristic controller families inherited the optional
  `None` route export; Probe also had no route immediately after `start_task`.
  The real-navigation test initially could not observe an authoritative route.
- Added the shared `next_route_index` conversion, immutable route exports for
  Coverage, Probe, Tracking, Return, and Holding, monotonic route revisions,
  and explicit pending/guidance-only/unavailable/cleared status transitions.
  Probe now plans its baseline standoff route from the current contact
  observation during `start_task`; route planning still uses only the existing
  navigator and contact estimate.
- Tracking exports the active avoidance follower before the approach follower;
  steady LGVF feedback is `guidance_only`. Return exports its reserved recovery
  path, while Holding has an explicit empty guidance-only snapshot. Stopped or
  invalidated routes cannot remain `ready`.
- Added a non-horizontal real `AStarNavigator` plus `UAVDynamicsExecutor`
  integration test. It checks finite poses, heading change, decreasing contact
  distance, and arrival within the baseline standoff envelope.
- Gate command:
  `python -m pytest tests/control/heuristic/test_coverage.py tests/control/heuristic/test_probe.py tests/control/heuristic/test_tracking.py tests/control/heuristic/test_navigation.py tests/mission/test_probe_navigation_integration.py -q`
- Gate result: `71 passed in 1.15s`.

## T07 Route and Sensor Frame Publication

- Red evidence: the frame builder had no route sampling helpers, reported an
  empty transit route as `1.0`, and serialized only the legacy entity tails;
  runtime state also had no published controller envelope.
- Added bounded endpoint-preserving route sampling, current-pose plus
  unconsumed-route serialization, `visual_schema_version`, task phase/source
  metadata, ProbeSession-backed `observation_started`, and stale
  episode/generation diagnostics. Explicit controller `cleared`,
  `unavailable`, and `guidance_only` states do not fall back to entity paths;
  historical frames still use the legacy fallback when no envelope exists.
- Added Coordinator pending-task enrichment so the first successful assignment
  frame retains its task identity before the controller executes its first
  tick. Simulation publishes envelopes at initialization, runtime boundaries,
  successful assignment, and after every successful control tick; reset uses
  the fresh StateManager cache.
- Red-to-green command:
  `python -m pytest tests/env/test_route_visual_frame.py -q`
  -> `8 passed in 0.80s`.
- Gate command:
  `LONGCAT_API_KEY=t07-offline-regression python -m pytest tests/env/test_route_visual_frame.py tests/env/test_mixed_frame.py tests/env/test_frame_publisher.py tests/mission/test_sensor_modes.py tests/env/test_sensors_and_obstacles.py -q`
- Gate result: `24 passed in 1.20s`.
- Coordinator and existing simulation integration regression:
  `LONGCAT_API_KEY=t07-offline-regression python -m pytest tests/control/test_route_snapshot.py tests/env/test_route_visual_frame.py tests/env/test_simulation_integration.py -q`
  -> `40 passed in 14.71s`.

## T08 Visual Target, Phase, and Label Restoration

- Red evidence: the frontend had no shared task-phase display contract, probe
  baseline could be read as tracking by status, observed contacts used a
  generic dot with inline text, and scenario vessels had no independent render
  switch or label layout.
- Added `uavDisplayState` with the task visual contract first and legacy status
  fallback. A probe in `baseline` with `observation_started=false` is rendered
  as `接近调查`; map labels, UAV sidebar rows/details, and contact details use
  the same mapping.
- Added a deterministic screen-space `layoutLabels` placer with priority,
  eight fixed offsets, boundary/overlap checks, and selected-object leader
  lines. Object text is drawn in one pass after symbols; hidden low-priority
  text does not remove its clickable symbol.
- Contacts remain the observation branch when present, legacy ships are used
  only when contacts are absent, and scenario vessels are drawn/labeled only
  when the explicit scene-truth switch is enabled. Initialization placement
  temporarily reveals the layer without changing the user's persisted switch.
  Unknown contacts use a neutral vessel symbol and zero velocity does not
  fabricate a heading.
- Red-to-green browser checks:
  `npx playwright test tests/replay-restoration.spec.js --grep 'probe|contact|label|scenario'`
  -> `3 passed`.
- Gate checks:
  `npx playwright test tests/mixed-maritime.spec.js --grep 'vessel|AIS|replay renders intent|operator can draw|drag geometry'`
  -> `8 passed`; `npm run build` -> Vite build passed.

## T09 Replay Events, Identity, and Historical Compatibility

- Red evidence: replay markers used insertion-order-sensitive JSON
  serialization, only covered three legacy event names, and unloaded seeks
  displayed a nearest loaded frame. The replay adapter normalized vessel data
  but left missing historical collections and route fields undefined.
- Added `collectReplayMarkers`/`replayEventKey` with recursively sorted object
  keys, numeric time normalization, old/new event aliases, first loaded frame
  binding, and an empty-assignment guard. The drawer and timeline consume the
  same canonical event objects.
- Replay requests now reject stale generations after the response, coalesce
  same-generation chunk requests, expose target-frame loading and loaded-frame
  progress, clear markers on file changes, and return `null` until the target
  frame is present. LLM details likewise use only the current loaded frame.
- The replay adapter returns detached historical frames with explicit list
  defaults and empty legacy routes; it does not write the source JSONL.
- Red-to-green checks:
  `python -m pytest tests/vis/test_replay_adapter.py -q` -> `3 passed`;
  `npx playwright test tests/replay-restoration.spec.js --grep 'markers|unloaded|switching'`
  -> `3 passed`.
- Gate checks:
  `python -m pytest tests/vis/test_replay_adapter.py tests/env/test_server_runtime.py -q`
  -> `16 passed`; frontend replay/legacy/mixed gate -> `16 passed`; `npm run build`
  -> Vite build passed.

## T10 Real-Engine Scenario Runner

- Added `build_scenario`, `capture_frame`, `run_scenario`, and the
  `validate_replay_restoration.py` CLI.  The runner advances only through
  `SimulationEngine.step()`, rejects a repeated output manifest, preserves
  fixture/live role bindings, writes canonical event keys, and stops with a
  blocked result when simulation time does not advance.
- The frame audit checks finite entity and sensor geometry, route continuity,
  controller generation, monotonic time, and bounded metric values.  The
  first runner gate passed with `5 passed` in
  `tests/mission/test_replay_restoration_runner.py`.
- Real-engine smoke artifacts passed:
  `outputs/validation/replay-restoration/t10-v01` (2/2 frames) and
  `outputs/validation/replay-restoration/t10-v03` (20/20 frames).  They are
  production frame-builder outputs, not static visual fixtures.

## T11 Sensing, Information, and Runtime Commands

- Added cross-module integration coverage for AIS toggle, passive sensing
  gates/aggregation, information version and fairness propagation, contact
  assessment, operator intent lifecycle, and vessel/red lifecycle.  The two
  new feature integration modules passed `10 passed`.
- The public regression set for information, visibility, maritime acceptance,
  prompt fairness, passive sensing, and vessel boundaries passed `10 passed`.
  The feature suite later produced finished 120-minute real-engine runs for
  `ais-toggle`, `passive-gates`, `information-loop`,
  `contact-assessment`, `intent-lifecycle`, and `vessel-red-lifecycle` under
  `outputs/validation/replay-restoration/t15-features-42-20260917`.
- Negative paths remain explicit: AIS-off creates no new AIS sample, one
  passive bearing does not release a position, stale intent revisions are
  rejected, and vessel/red failures do not leak deleted or hidden truth into
  blue prompts.

## T12 Safety, Recovery, and Handoff

- Connected live controller route snapshots to conflict detection and kept
  conflict handling edge-triggered.  Shared departure prefixes are ignored
  only while routes are geometrically identical; after divergence, the
  requested separation is enforced.  A persistent conflict pair does not
  reset a coverage follower on every tick.  Coverage yields to a probe/track
  entry when the real conflict requires it, while BC/RL ownership remains
  untouched.  The follow-up regression is committed as `be1eead`.
- T12 gate:
  `LONGCAT_API_KEY=t12-control-gate-final2 python -m pytest
  tests/mission/test_feature_control_integration.py tests/mission/test_handoff.py
  tests/control/test_simulation_ownership.py tests/control/test_safety.py
  tests/env/test_storm_avoidance.py tests/utils/test_conflict_detector.py
  tests/env/test_goal2_foundation.py -q`
  -> `72 passed in 32.05s`.
- Latest V07 fixture run:
  `outputs/validation/replay-restoration/t12-v07-480-20260917` has
  `480/480` frames, `sim_time=480.0`, `audit_issue_count=0`, and the latest
  manifest commit `be1eead`.  Its observed causal chain is:
  `assessment_applied` at t24, fixture fuel preparation at t25, fuel warning
  and `handoff_required` at t26, `handoff_assignment_committed` at t26, and
  `handoff_eo_lock_acquired` at t27.  The manifest records the source and
  successor UAV positions used for the observed-contact fixture.
- The same gate covers dynamic storm route revision and unaffected task
  preservation, real return/refuel/reset/redispatch, capacity exhaustion with
  `no_safe_recovery_path`, sensor-unavailable/normal-return distinction, and
  the no-omniscient-track regressions.  No instant refuelling or synthetic
  frame was used.
- Merge review also covered the shared-prefix exception: the detector now
  checks the first geometrically divergent prediction step instead of waiting
  one tick.  The focused conflict/control regression after that fix passed
  `12 passed` in `18.81s` (`fb43889`).
- Re-running the complete T12 command after the fix passed `73 passed` in
  `32.60s`; the earlier `72 passed` line above records the pre-review gate.

## T13 Reviewer, Metrics, Memory, and Providers

- The T13 gate passed `72 passed in 3.32s`.  Reviewer summaries reach the next
  mission prompt while reviewer failure keeps the unified scheduler; evaluator
  outputs retain `None` for missing denominators and mark empty episodes
  invalid; all episode logger streams receive the episode ID.
- Runtime memory selection is resolved once at episode initialization.  The
  `baseline`/`active`/explicit-version and unknown-version paths, reset
  behavior, cleanup protection, and `--memory-root`/`--memory-version` parity
  are covered.  The provider contract runs factory -> observation -> action ->
  SafetyEnvelope -> executor, and absent providers fail explicitly.
- The temporary memory tests exercise propose/save/validate/report/activate/
  select/rollback with holdout checks.  No valid production live history was
  available in this run, so I14 remains `待验证`; fixture episodes are not
  eligible for formal activation or the production prompt.

## T14 Browser and Visual Acceptance

- `npm run build` passed.  The existing static acceptance suite passed
  `13 passed`.  The dedicated replay configuration passed `7 passed` using
  the real artifacts in
  `outputs/validation/replay-restoration/t14-browser-20260917`.
- The browser input directory records eight source entries and SHA-256 values
  for V01/V03 smoke data, V06 SAR data, and the latest V07 EO/handoff data in
  `sources.json`.  The test waits for the target frame, fonts, and image
  completion; it verifies canonical markers, unloaded seeks, file switching,
  layer pixels, responsive overflow, and a real MP4 download.
- Visual evidence includes event-timed assignment, assessment, return,
  handoff, probe baseline/near/tracking, V06 SAR (`08-sar-v06.png`), final
  V07 map, and t120/t480 screenshots.  The MP4 is
  `screenshots-final/v07-seed42-replay.mp4`; `ffprobe` reports MP4 format,
  26.888 seconds, and 29,617,626 bytes.  Manual inspection found the map,
  tracks, sensor layer, labels, and sidebar state visible without an empty
  canvas or material overlap.
- An initial V07-only visual attempt correctly failed because that scenario
  contains EO but no SAR.  The final test uses the real V06 SAR run for the
  SAR frame and keeps V07 for the EO/handoff storyline; no sensor mode was
  fabricated.

## T15 Long-Run and Final Gates

- Full Python regression:
  `LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m
  pytest tests -q` -> `1383 passed, 1 warning in 518.33s`.  The warning is
  the known unregistered `pytest.mark.timeout` warning caused by disabling
  external plugins; it is not a test failure.
- Required current-commit fixture runs all finished with zero frame-audit
  issues:

  | Run | Frames / sim time | Modes or result | Audit |
  | --- | ---: | --- | ---: |
  | `t15-v06-20-20260917` | 20 / 20 min | startup transit | 0 |
  | `t15-v06-120-20260917` | 120 / 120 min | SAR + EO, 1 search completion | 0 |
  | `t15-v06-480-20260917` | 480 / 480 min | SAR + EO, 6 returns, 3 search completions | 0 |
  | `t15-v06-seed101-480-20260917` | 480 / 480 min | SAR + EO, 1 return, 1 search completion | 0 |
  | `t12-v07-480-20260917` | 480 / 480 min | EO, classification and handoff lock | 0 |

- Read-only `--check-log` re-audits passed for the V06 seed42 480 run, V07
  480 run, and V06 seed101 480 run; each returned `frames=480`, `issues=[]`.
  The feature suite contains 11 finished 120-minute subruns, each with
  `audit_issue_count=0`.
- The fixed 900x700 browser render sample remains within the recorded baseline
  comparison: remeasure P50/P95 `0.9/29.4 ms`, current P50/P95 `1.0/29.0 ms`,
  500 samples on the same source frame set.  This is an environment-local
  comparison only; no performance improvement claim is made.
- No `LONGCAT_API_KEY` was present, so the requested live model run was not
  started and no fixture result is labeled live.  Formal cross-episode I14
  memory validation and online model accuracy are therefore explicitly open.

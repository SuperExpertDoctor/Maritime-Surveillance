# Five-Hour Replay Repair Validation

Date: 2026-09-27. Branch: `fix/live5h-repair`. Base: `53e5e3f`.
Production changes through `27a96be`; final verification is recorded below.
Worktree: `.worktrees/live5h-repair` in the original checkout.
The original checkout, its branch, and historical replay were not modified.

## Historical Audit Conclusion

`outputs/simulation_20260926_170835.jsonl` spans 18000.022 wall-clock seconds
in one episode but only 177 simulation minutes. Nine focus submissions were
25-cell areas. The four hourly fleet/AIS adjustment groups eventually applied,
but approximately 9m33s, 21m33s, 8m40s, and 12m41s late. Therefore the replay
does not establish the requested timely intervention and reconnaissance effects.

Both code defects and model latency contributed. An invalid orbit entry,
unsuccessful evidence acquisition, search monopolizing probe resources,
focus ownership/filtering, airborne HOLDING treated as a landing, and missing
EO-only evaluation associations are independent of LLM latency. Synchronous
model calls and repeated empty decisions consumed wall time; receipt/frame
visibility lag additionally held up UI operations. Missing delete controls in
hours 2/4 were not conclusively diagnosed from the retained evidence and are
not asserted to have been a proven React rendering defect.

## Finding-to-Repair Mapping

| Finding | Repair | Main regression coverage |
| --- | --- | --- |
| Out-of-bounds or blocked orbit entry | Validate the complete entry path; recover on observed motion, map change, safety intervention or stall; retain PROBE ownership during transient failure | `test_probe.py`, `test_probe_navigation_integration.py`, tracking/navigation/orbit suites |
| EO samples never complete baseline | Correct guidance radius, angular feedforward and post-move sensing; refresh completed ingress immediately; moving-contact updates no longer reset active orbit guidance | Real executor at production dt/speed for 0/12/18-knot contacts; real-engine EO baseline; V07 physical classification/handoff with offline assessor |
| Removed target causes airborne teleport/refuel | Require registered landing queue/base and physical arrival; checked recovery with fuel reserve and atomic installation | Immediate turnaround, removal, impossible route, rejected installation and diversion tests |
| Automatic arrivals undo manual population edits | Successful manual create/delete pauses owned automatic releases for that episode; reset restores policy | Manual/automatic order, spoofed command IDs, invalid/AIS controls, reset and population-limit tests |
| EO-only detections absent from evaluator | Link actual EO ingestion returns only in evaluator state; retain concurrent returns and reject stale/off-axis evidence | EO-only/current-return/multiple-return outcome tests; no truth added to blue observations/prompts |
| Existing reservations hide focus areas | Attach existing owners, preserve reservations/progress, reorder safe remaining swaths, keep legal uncovered candidates, require revision-specific model acknowledgement | Production snapshot full/partial overlap, once-only review, failed review, reset, route safety and priority tests |
| Search consumes probe resources | Enforce actual 80% ordinary-search admission when executable probe demand exists, including pending/light dispatch | 1/2/3/10-aircraft fleets, infeasible-demand controls, committed-batch follow-up and preserved leases/progress |
| Repeated empty model calls | Share usable assignment-edge filtering between prompt construction and skip decisions; focus review remains actionable | Empty/unusable/pending-edge controls and owned-focus acknowledgement tests |
| Multiplied role timeout and hopeless retries | One total configured provider budget, shorter caller deadline wins, minimum useful retry budget and explicit diagnostics | Gateway, scheduler-budget, assessor and role-client suites with fake clocks |
| Applied receipt precedes visible state | Publish a neutral authoritative command-boundary frame before subsequent model work, without another tick or sleep | Real engine/publisher blocked-model tests, paused/reset/retry/abort tests |
| UI unlocks before create/delete/AIS confirmation | Match entity/revision/state and episode/reset generation; deletion retains selected identity until authoritative absence | Receipt-first and frame-first browser tests, stale receipt/frame and episode changes |
| Intent GET mutates simulation state | Public API reads detached published snapshots; only simulation thread annotates owners and emits events | Read-only API regression; independent publication freshness diagnostic |
| Reassigned owner misses focus delivery | Include assigned UAV and controller generation in delivery identity; keep model acknowledgement separate | Real retained-task reassignment preserving progress; same-UAV new-controller generation; no duplicate unchanged delivery |
| Multiple owner IDs are clipped | Localized flex/wrapping fix in existing intent row | Four production-shaped task IDs at desktop/mobile widths |

No classification threshold, duration, range band, evidence minimum, or
model identity was weakened to obtain passing results. No production
dependencies were added. Interface changes preserve existing command queues,
receipts, task identities, and the existing map selection workflow.

## Verification

- Final affected Python suite at `27a96be`: **751 passed, 50 deselected,
  676.75s (11m16s)**. Fifty unchanged,
  expensive multi-seed vessel-motion cases are excluded from this covering run;
  they were exercised in the earlier broad baseline.
- Final browser suite at `e3fd243`: **48 passed, 1.3 minutes**, warning-free.
  Later changes affect Python probe transitions/tests only.
- Frontend production build: **passed**, 1512 modules, 710ms.
- Supplementary probe suite at `27a96be`: **26 passed, 15.62s**.
  Before the final transition fix, the 18-knot dynamics case failed with
  `probe_timeout`; two isolated transition regressions also failed. All pass
  without changing evidence gates or the 20-minute probe deadline.
- Real reassignment and same-UAV new-generation regressions: **2 passed, 32.71s**.
- Independent whole-branch review found three Important issues, fixed in
  `e3fd243`. Independent re-review approved those fixes and `27a96be` with no
  remaining Critical/Important findings. All four Minor coverage suggestions
  were subsequently implemented.
- Final whole-repository fail-fast run at `27a96be`: **284 passed, 1 failed,
  5.97s**. The failure is the existing live-gateway constructor dependency in
  `tests/control/test_simulation_ownership.py::test_default_simulation_starts_heuristic_leases_after_real_scheduler_tick`:
  `LONGCAT_API_KEY is required for the decision_maker binding`.

Commands use `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
-p no:cacheprovider -o 'markers=timeout: legacy timeout marker'`.
The final covering run selected the following files/nodes, with
`-k 'not runtime_vessel_motion_stays_in_bounds_across_seeds'`:

```text
tests/control/heuristic/test_probe.py
tests/control/heuristic/test_tracking.py
tests/control/heuristic/test_navigation.py
tests/control/heuristic/test_coverage.py
tests/utils/test_track_orbit.py
tests/mission/test_probe_navigation_integration.py
tests/mission/test_probe_session.py
tests/mission/test_dynamic_vessel_lifecycle.py
tests/mission/test_vessel_commands.py
tests/mission/test_opponent_runtime.py
tests/mission/test_opponent_population.py
tests/mission/test_vessel_population_limits.py
tests/mission/test_outcome_evaluator.py
tests/mission/test_immediate_base_turnaround.py
tests/mission/test_recovery_diversion_handoff.py
tests/mission/test_live5h_scheduling.py
tests/mission/test_intent_candidates.py
tests/mission/test_task_catalog.py
tests/mission/test_coverage_prompt_window.py
tests/mission/test_coverage_policy.py
tests/mission/test_mission_scheduler.py
tests/mission/test_intent_store.py
tests/mission/test_coverage_model_failure.py
tests/mission/test_coverage_decision_budget.py
tests/mission/test_feature_commands_integration.py
tests/mission/test_feature_episode_integration.py
tests/mission/test_feature_control_integration.py::test_v07_real_eo_samples_classify_then_handoff_and_eo_lock
tests/mission/test_coverage_scan_integration.py
tests/mission/test_coverage_failure_cleanup.py
tests/mission/test_llm_gateway.py
tests/mission/test_contact_assessor.py
tests/schedule/test_llm_client.py
tests/test_live_runtime_loop.py
tests/env/test_frame_publisher.py
tests/env/test_intent_api.py
tests/env/test_coverage_frame.py
tests/mission/test_legacy_search_scheduling.py
tests/mission/test_idle_fleet_dispatch.py
```

Browser command, from `src/vis/frontend`:

```bash
env -u NO_COLOR PLAYWRIGHT_FRONTEND_PORT=5196 npx playwright test \
  --config=playwright.interactions.config.js \
  --output=test-results/live5h-final-reviewed
npm run build
```

Results from different runs overlap and must not be summed as unique tests.
The earlier broad mission baseline had 1184 passes and 21 failures, including
legacy sensing, information-loop, motion/prompt, replay and batch fixtures.
Several in-scope stale fixtures were repaired, but this report does not claim
that every unrelated baseline failure was resolved or that the repository is
entirely green. No credentials were supplied and no real provider was called.

## Real UI Smoke

**Passed: six operations in one episode**,
`episode-24c7a3d181c84a20a17399a003d51ed2`. This check uses real `main.py`, HTTP, WebSocket,
simulation command queues and canvas interactions. Only the model boundary
is replaced with the existing deterministic fixture. It creates I and II
vessels, toggles II AIS, deletes both, and draws/submits one 5x5 focus area in
one episode. Acceptance checks receipts plus authoritative frames, and matches
the actual recorded model request to the successful published call ID.

All six commands applied and were confirmed in authoritative frames. The
focus bbox was `[6, 0, 11, 5]` (25 cells); intent `I0001` revision 1 appeared in
`pending_intent_reviews` of successful call `fixture-00000005`. Fourteen frames
were observed without an episode change or JavaScript error. Desktop canvas
was 1136x952 with 381 sampled colors; desktop/mobile screenshots were inspected.
Measured operation confirmation times were 8.199-10.437 seconds in this
offline fixture run, not a live-provider latency benchmark.

Evidence: `.superpowers/sdd/offline-smoke-1790490371903/report.json`,
`desktop.png`, and `mobile.png` in the same directory. The report includes the
actual offline model request; it does not claim an external model was called.

Diagnostic artifacts and the preview driver are retained under
`.superpowers/sdd/`. Earlier smoke attempts caught valid spacing/capacity
rejections in the driver and an incorrect assumption that public telemetry
contains full model prompts. These were diagnostic-harness issues, not treated
as successful UI acceptance. Production intentionally omits full prompts from
public telemetry; the final harness records its own offline gateway inputs.

## Limits and Remaining Acceptance

- **No new real-provider five-hour wall-clock acceptance run was performed.**
  Offline, fake-clock, fixture and browser tests do not prove provider
  availability, hourly timing, or long-duration mission effectiveness.
- Default provider timeout is a **120-second total logical request budget**,
  including retries. Shorter caller deadlines and reserves still apply.
  Retries require at least **5 seconds** of useful transport budget. The
  connectivity probe uses one independent attempt, normally 20 seconds.
- Deadlines are cooperative synchronous-transport limits. An injected
  transport that ignores its timeout cannot be forcibly interrupted here;
  late results are rejected. `--wall-seconds` stops at a simulation boundary,
  not by forcibly cancelling an in-progress step. Exactly-on-the-hour command
  application is not guaranteed while a step is in progress.
- Idealized moving-contact evidence tests use real UAV dynamics but do not
  replace stochastic sensing, obstacle-rich mission or live-LLM acceptance.
- Tiny uncovered focus remnants below legal search geometry remain honestly
  unmet. Unsafe focus-route updates retain the previously validated route.
- The unchanged frontend lockfile has `nanoid@3.3.16`; npm reported existing
  advisory `GHSA-2v37-7h3g-55p8`. No out-of-scope dependency upgrade was made.

A future real acceptance run should use this worktree, configured credentials,
one episode, an ample step budget and a new report directory, for example:

```bash
python main.py --steps 100000 --wall-seconds 18000 --port 8765 \
  --hold-server --run-report-dir outputs/live5h_repair_acceptance
```

That command alone does **not** schedule UI interventions. At real elapsed
30-minute boundaries, draw and submit exactly 5x5 cells through the map UI;
at each full hour before the endpoint, adjust I/II counts and II AIS through
the vessel UI. Log intended time, enqueue, applied receipt, authoritative-frame
confirmation, model acknowledgement and reconnaissance outcome separately.
Do not substitute offline configuration edits or accelerated simulation.
Endpoint policy and permissible timing tolerance must be specified in that
acceptance record. `--hold-server` preserves the final view; it does not extend
an episode that ended early.

## Handoff

The repair branch/worktree are preserved for review. Nothing was merged or
pushed. Offline preview: `http://127.0.0.1:5197`, backend port 18767. It uses
the deterministic fixture, runs for a 15-minute diagnostic window, then keeps
the final view available; it is not a provider-backed acceptance session.
The preview driver is `.superpowers/sdd/offline_preview.py`. To reproduce the
UI smoke, set `PREVIEW_URL` and `PREVIEW_REQUEST_LOG` (the driver's printed
temporary directory plus `/requests.jsonl`) and run
`node .superpowers/sdd/offline_ui_smoke.cjs` from this worktree. Use a fresh
diagnostic episode so prior test vessels do not consume population capacity.

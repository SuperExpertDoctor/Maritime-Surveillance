# Six-Hour Live Interaction Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the actual `main.py` process for at least 21,600 wall-clock seconds with reproducible browser-driven blue-operator changes, and retain evidence that red reconnaissance responds to each map focus area.

**Architecture:** Add a small Node.js ESM harness that launches `main.py`, attaches Playwright to its live UI, and schedules UI actions against a monotonic clock. Keep deterministic selection and final evidence auditing as pure functions so their contracts can be unit-tested without simulating six hours.

**Tech Stack:** Python CLI (`main.py`), Node.js ESM, Playwright, Node `node:test`, JSONL runtime and operator evidence.

## Global Constraints

- Run the actual `main.py` process for at least 21,600 seconds of wall-clock time with the configured live LongCat provider and browser-based operator interactions.
- Start with `--steps 1000`, `--step-delay 60`, and `--wall-seconds 21720`; the two-minute margin covers initialization/report timing, and the 60-second delay paces each one-minute simulation step at real-time speed.
- At elapsed hours 1, 2, 3, 4, and 5, draw and submit one 5x5 cell `search_priority` area through the map UI; select only boxes whose 25 cells are in the live frame's searchable domain, prefer boxes overlapping an active search region when one is available, and keep each active for 360 simulation minutes.
- At elapsed hours 1.5, 3, and 4.5, delete and replace one randomly selected active Type I and one Type II vessel, and toggle AIS for a random half of the active Type II vessels, with at least one selected.
- Use the live browser controls, not configuration edits or direct state mutation. Use a recorded seed and a monotonic clock.
- Preserve existing output artifacts. Record planned and actual command times, receipts, authoritative-frame confirmations, model decisions, screenshots, rejected commands, and model-blocked states.
- Passing requires at least 21,600 seconds of runtime, all five focus operations and three population/AIS rounds confirmed in authoritative frames, and inspectable red task/decision evidence for each focus. Fixture or accelerated runs are development checks only.

---

## File Structure

- Create `scripts/live_interaction_scenario.mjs` for the fixed schedule, seeded PRNG, legal 5x5 selection, random vessel/AIS target selection, and final acceptance audit.
- Create `scripts/live_interaction_ui.mjs` for Playwright frame capture and map/sidebar actions. It must use visible UI controls and emit request, receipt, and frame-confirmation details to the caller.
- Create `scripts/run_live_interaction_acceptance.mjs` for output-directory creation, server startup, monotonic scheduling, JSONL/screenshot capture, process monitoring, and final report generation. The runner creates the outer run directory and passes a not-yet-existing `main-report/` child to `main.py`, whose CLI rejects an already-existing report directory.
- Create `tests/scripts/test_live_interaction_scenario.mjs` for schedule, deterministic selection, and audit unit tests.
- Create `src/vis/frontend/tests/live-interaction-operator.spec.js` for browser-level validation of the same UI helpers against the existing fixture WebSocket helper and intercepted command responses.
- Create `docs/validation/2026-09-29-live-six-hour-interaction-run.md` only after the live run finishes, recording actual evidence and any failed acceptance items.

### Task 1: Implement deterministic schedules and choices

**Files:**
- Create: `scripts/live_interaction_scenario.mjs`
- Test: `tests/scripts/test_live_interaction_scenario.mjs`

**Interfaces:**
- `operatorSchedule()` returns eight entries sorted by elapsed milliseconds: focus at 3,600,000; fleet/AIS at 5,400,000; focus at 7,200,000; focus then fleet/AIS at 10,800,000; focus at 14,400,000; fleet/AIS at 16,200,000; focus at 18,000,000.
- `seededRandom(seed)` returns a deterministic `[0, 1)` random generator.
- `chooseFocusBBox(frame, rng, previousBBoxes)` returns an integer `[x1, y1, x2, y2]` bbox exactly five cells wide/high, with all 25 cells present in `frame.search_domain.searchable_cells`; it excludes overlap with previous boxes, prefers a candidate intersecting an active search region when available, and throws when no legal candidate exists.
- `chooseFleetTargets(frame, rng)` returns `{ replaceTypeIId, replaceTypeIIId }` chosen from `frame.scenario_vessels` and throws when either class is absent.
- `chooseAisTargets(frame, rng)` returns a unique ID array containing `max(1, ceil(activeTypeIICount / 2))` current, controllable Type II vessels.
- `auditAcceptance({ report, operatorEvents, frames })` returns `{ passed, failures, metrics }`; it requires a 21,600-second `summary.wall_seconds`, all eight groups confirmed, exactly 25 eligible cells in each focus bbox, all vessel deletes/replacements and at least one AIS toggle per round confirmed, and at least one focus-owned task or matching successful decision for every focus.

- [x] **Step 1: Write failing unit tests**

```js
import test from "node:test";
import assert from "node:assert/strict";
import { operatorSchedule, seededRandom, chooseFocusBBox, chooseFleetTargets, chooseAisTargets } from "../../scripts/live_interaction_scenario.mjs";

test("operator schedule has five hourly focuses and three 90-minute fleet rounds", () => {
  const events = operatorSchedule();
  assert.deepEqual(events.map(({ kind, atMs }) => [kind, atMs]), [
    ["focus", 3600000], ["fleet", 5400000], ["focus", 7200000],
    ["focus", 10800000], ["fleet", 10800000], ["focus", 14400000],
    ["fleet", 16200000], ["focus", 18000000],
  ]);
});

test("focus boxes contain only 25 searchable cells and avoid previous boxes", () => {
  const cells = Array.from({ length: 30 }, (_, x) => Array.from({ length: 30 }, (_, y) => [x, y]))
    .flat().filter(([x, y]) => !(x >= 10 && x < 15 && y >= 10 && y < 15));
  const frame = { search_domain: { searchable_cells: cells } };
  const first = chooseFocusBBox(frame, seededRandom(8), []);
  const second = chooseFocusBBox(frame, seededRandom(8), [first]);
  const searchable = new Set(cells.map(([x, y]) => `${x},${y}`));
  for (let x = first[0]; x < first[2]; x += 1) {
    for (let y = first[1]; y < first[3]; y += 1) assert.ok(searchable.has(`${x},${y}`));
  }
  assert.equal(first[2] - first[0], 5);
  assert.equal(first[3] - first[1], 5);
  assert.notDeepEqual(second, first);
  assert.ok(second[2] <= first[0] || second[0] >= first[2]
    || second[3] <= first[1] || second[1] >= first[3]);
});

test("AIS targets are reproducible and include at least one active Type II", () => {
  const vessels = Array.from({ length: 3 }, (_, index) => ({
    scenario_entity_id: `ii-${index}`, vessel_class: "type_ii", ais_controllable: true,
  }));
  const frame = { scenario_vessels: vessels };
  const targets = chooseAisTargets(frame, seededRandom(12));
  assert.deepEqual(targets, chooseAisTargets(frame, seededRandom(12)));
  assert.equal(targets.length, 2);
  assert.equal(new Set(targets).size, 2);
});

test("fleet targets contain one active vessel of each class", () => {
  const frame = { scenario_vessels: [
    { scenario_entity_id: "i-1", vessel_class: "type_i" },
    { scenario_entity_id: "ii-1", vessel_class: "type_ii" },
  ] };
  assert.deepEqual(chooseFleetTargets(frame, seededRandom(3)), {
    replaceTypeIId: "i-1", replaceTypeIIId: "ii-1",
  });
});
```

- [x] **Step 2: Run tests and verify the intended import failure**

Run: `node --test tests/scripts/test_live_interaction_scenario.mjs`

Expected: FAIL because `scripts/live_interaction_scenario.mjs` does not exist.

- [x] **Step 3: Implement pure schedule, selection, and acceptance audit functions**

Use `seededRandom` with a recorded 32-bit seed. Build candidate boxes by enumerating top-left cells `(x, y)` from `search_domain.searchable_cells`, requiring every cell in `[x,x+5) × [y,y+5)` to be present. Shuffle legal non-overlapping candidates that intersect an active search region when any are available; otherwise shuffle all legal candidates. For AIS, shuffle active controllable Type II IDs and take `Math.max(1, Math.ceil(ids.length / 2))`. The audit reads only frame snapshots, operator ledger records, and `report.summary.wall_seconds`; it records every unsatisfied check as a failure and never infers application from enqueue.

```js
export function operatorSchedule() {
  return [
    ["focus", 3600000], ["fleet", 5400000], ["focus", 7200000],
    ["focus", 10800000], ["fleet", 10800000], ["focus", 14400000],
    ["fleet", 16200000], ["focus", 18000000],
  ].map(([kind, atMs], order) => ({ kind, atMs, order }));
}

export function seededRandom(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6D2B79F5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}
```

- [x] **Step 4: Run the unit tests and verify they pass**

Run: `node --test tests/scripts/test_live_interaction_scenario.mjs`

Expected: PASS for schedule, focus geometry, deterministic randomization, AIS count, and audit pass/fail evidence.

### Task 2: Drive real operator interactions through the browser UI

**Files:**
- Create: `scripts/live_interaction_ui.mjs`
- Create: `src/vis/frontend/tests/live-interaction-operator.spec.js`
- Modify: `src/vis/frontend/playwright.interactions.config.js` to include the new browser spec.
- Reuse: `src/vis/frontend/tests/helpers/frameSocket.js`
- Reuse: `src/vis/frontend/src/renderer/geometry.js`

**Interfaces:**
- `attachLiveFrames(page, onFrame)` listens to the page's `/ws/live` WebSocket and passes each parsed frame to `onFrame(frame)`; it returns a disposer.
- `drawFocusArea(page, { bbox, label, durationMin })` clicks `框选重点区`, drags the visible canvas for the exact bbox, fills the label and duration controls, clicks `提交重点区`, then resolves only after both an applied command receipt and an authoritative frame contain the submitted bbox.
- `selectVesselPlacement(page, vesselClass, cell)` uses the `I 类船舶` or `II 类船舶` palette and visible map click. It returns only after the created scenario vessel appears in a live frame at the requested cell.
- `deleteVessel(page, vesselId)` uses the `船舶状态` tab's labeled delete control and returns only after the applied receipt and authoritative absence.
- `setVesselAis(page, vesselId, enabled)` uses the row switch and returns only after applied receipt plus the requested value in an authoritative frame.
- Each helper returns `{ commandId, queuedAt, appliedAt, frameId, state }`; no helper calls command APIs directly.

- [x] **Step 1: Add a failing browser test for exact map selection**

Install the fixture frame socket, intercept only the intent POST/poll responses, load `/`, and call `drawFocusArea` with `[10, 10, 15, 15]`. Assert the request has that exact bbox, mode `search_priority`, and duration `360`; assert the helper waits until the fixture frame contains the same applied intent.

Run: `cd src/vis/frontend; npx playwright test --config playwright.interactions.config.js tests/live-interaction-operator.spec.js --grep "focus"`

Expected: FAIL because the helper module/test does not exist.

- [x] **Step 2: Implement frame capture and visible map/sidebar helpers**

Compute drag points from `.canvas-area canvas` bounds and `computeLayout`; for bbox `[x1,y1,x2,y2]`, drag from cell `(x1 + 0.25, y1 + 0.25)` to `(x2 - 0.25, y2 - 0.25)`. Read and operate on the `船舶状态` table by vessel ID, wait on command feedback, and separately require a confirming frame. Attach request/response listeners solely for correlation logging; use UI button/switch/canvas actions to submit every command.

```js
await page.getByRole("button", { name: "框选重点区", exact: true }).click();
const { rect, layout } = await page.evaluate(async () => {
  const { computeLayout } = await import("/src/renderer/geometry.js");
  const rect = document.querySelector(".canvas-area canvas").getBoundingClientRect();
  return { rect, layout: computeLayout(rect.width, rect.height) };
});
const [x1, y1, x2, y2] = bbox;
await page.mouse.move(rect.x + layout.offsetX + (x1 + 0.25) * layout.cellSize,
                      rect.y + layout.offsetY + (y1 + 0.25) * layout.cellSize);
await page.mouse.down();
await page.mouse.move(rect.x + layout.offsetX + (x2 - 0.25) * layout.cellSize,
                      rect.y + layout.offsetY + (y2 - 0.25) * layout.cellSize);
await page.mouse.up();
await page.getByLabel("有效期").fill(String(durationMin));
await page.getByRole("button", { name: "提交重点区" }).click();
```

- [x] **Step 3: Add browser tests for create, delete, and AIS frame confirmation**

For each vessel operation, return a queued response, publish the applied command and authoritative state change as separate fixture updates, and assert the helper remains pending until the matching entity/revision/state is visible. Assert Type I has no AIS mutation helper path.

Run: `cd src/vis/frontend; npx playwright test --config playwright.interactions.config.js tests/live-interaction-operator.spec.js`

Expected: PASS; browser console contains no errors.

### Task 3: Orchestrate the live six-hour process and retain evidence

**Files:**
- Create: `scripts/run_live_interaction_acceptance.mjs`
- Reuse: `scripts/live_interaction_scenario.mjs`
- Reuse: `scripts/live_interaction_ui.mjs`

**Interfaces:**
- CLI: `node scripts/run_live_interaction_acceptance.mjs --seed 20260929` starts the acceptance run. No duration override is accepted in acceptance mode. `--smoke --seed 20260929` starts a separate ten-minute, real-provider UI smoke with immediate focus and vessel/AIS operations; smoke output is marked non-acceptance.
- The runner creates a new `outputs/live-interaction-<timestamp>-<seed>/` report directory, then launches `python main.py --steps 1000 --step-delay 60 --wall-seconds 21720 --port <free-port> --memory-root outputs/live-interaction-<timestamp>-<seed>/strategy_memory --run-report-dir outputs/live-interaction-<timestamp>-<seed>/main-report`.
- `operator.jsonl` includes a run-start record, every intended/sent/queued/applied/confirmed/rejected transition, selected coordinates/IDs, retries, and completion/failure. `frames.jsonl` stores observed live frames and decisions. `screenshots/` stores one screenshot per scheduled event group and a final screenshot. `runtime-console.log` stores child stdout/stderr. The final main report is `main-report/report.json`; its `summary.jsonl_path` identifies the authoritative frame JSONL under `outputs/`.
- The runner stops only when `main.py` exits after its wall budget, closes Chromium, writes `audit.json`, and exits nonzero for process failure or audit failure.
- `waitUntil(targetNs)` compares `process.hrtime.bigint()` to an absolute nanosecond deadline and sleeps only for the remaining duration; it never accumulates handler time into later event offsets.
- `runUiIntervention(event, context)` accepts `{ page, getLatestFrame, rng, previousBBoxes, record }` and dispatches only `focus` or `fleet` UI workflows; it appends each returned receipt/frame confirmation through `record(event)`.
- The evidence poller requests `/api/runtime/logs?after=<cursor>` and `/api/runtime/decisions` while the process is active, appending sanitized records to `frames.jsonl` alongside captured WebSocket snapshots.

- [x] **Step 1: Test acceptance audit failures before adding the launcher**

Add tests for short runtime, missing focus confirmation, non-25-cell bbox, unconfirmed vessel/AIS command, and absent focus task evidence. Run `node --test tests/scripts/test_live_interaction_scenario.mjs` and verify each failure is detected.

- [x] **Step 2: Implement no-overwrite run directory and process launcher**

Choose an available loopback port by binding a temporary server to port `0`, release it, and pass the assigned port. Create only the outer run directory and `screenshots/`; refuse to start if the unique run directory exists or the loaded runtime config has `clear_outputs_before_run=true`. Leave `main-report/` absent so `main.py` can create it. Spawn Python with `--skip-llm-probe` omitted, tee stdout/stderr into the outer report directory, poll `/api/runtime/logs` until HTTP 200, then open Chromium at `http://127.0.0.1:<port>` and require a connected live frame before scheduling. Use `createRequire(resolve("src/vis/frontend/package.json"))("@playwright/test")` so Node resolves the repository's existing Playwright installation without adding a dependency.

```js
const child = spawn("python", [
  "main.py", "--steps", "1000", "--step-delay", "60",
  "--wall-seconds", "21600", "--port", String(port),
  "--memory-root", path.join(runDir, "strategy_memory"),
  "--run-report-dir", path.join(runDir, "main-report"),
], { cwd: repoRoot, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
```

- [x] **Step 3: Implement monotonic scheduling, live actions, and failure records**

Use `process.hrtime.bigint()` as the timer source. For each absolute offset, wait until `start + atMs` without adding prior action duration to later deadlines. At a shared 3-hour offset, process focus then fleet/AIS and retain their common intended timestamp. Before each choice, use the latest authoritative frame and seeded PRNG. On `paused_model`, record the state and perform at most one visible `重试` UI click for that pause; log its receipt and resulting frame. Do not send a direct runtime POST. If a command is rejected/unconfirmed, preserve the error and continue to the next scheduled event unless the main process exits.

```js
for (const event of operatorSchedule()) {
  await waitUntil(startedAt + BigInt(event.atMs) * 1_000_000n);
  if (child.exitCode !== null) throw new Error(`main.py exited early: ${child.exitCode}`);
  await appendLedger({ type: "scheduled", kind: event.kind, intendedAtMs: event.atMs });
  await runUiIntervention(event, latestFrame, seededRandom, appendLedger);
}
```

In `--smoke` mode only, use a separate unique directory, `--steps 1000`, `--step-delay 1`, and `--wall-seconds 600`; run one immediate focus submission and one each Type I/II delete-and-replace plus one Type II AIS toggle. Set a top-level `acceptance: false` and skip the six-hour audit. Never expose duration/step overrides in the normal acceptance CLI.

- [x] **Step 4: Capture all run evidence and execute the audit at exit**

Append operator and frame JSONL records as events arrive, take event screenshots after authoritative confirmation, and preserve partial artifacts in `finally` on interruption. After child exit, load its `report.json`, invoke `auditAcceptance`, write `audit.json`, and fail the command unless `summary.wall_seconds >= 21600`, all events confirm, and each focus has a task/decision link.

- [x] **Step 5: Run all automated checks**

Run: `node --test tests/scripts/test_live_interaction_scenario.mjs`

Run: `cd src/vis/frontend; npx playwright test --config playwright.interactions.config.js tests/live-interaction-operator.spec.js`

Expected: PASS for the new scheduler/audit and UI-driver regressions. Existing state-machine tests remain unchanged.

### Task 4: Run and audit the live acceptance scenario

**Files:**
- Create after run: `docs/validation/2026-09-29-live-six-hour-interaction-run.md`
- Evidence: `outputs/live-interaction-<timestamp>-<seed>/`

The first full attempt on 2026-09-29 ran for 21,785.937 seconds and confirmed all five operator focus commands and all three fleet rounds. It was not accepted because focus `I0005` had no overlapping active search task and the live frame reported `resource_blocked`; its artifacts are preserved. The follow-up attempt prefers random legal focus boxes intersecting active search-task footprints when possible, without directly assigning or mutating tasks.

- [x] **Step 1: Check acceptance prerequisites**

Require a configured `LONGCAT_API_KEY` without printing its value, an available Chromium binary, `clear_outputs_before_run=false`, a fresh report directory, and a cleanly starting main process with a successful live connectivity probe. A failed probe aborts before starting the six-hour timer. First run `node scripts/run_live_interaction_acceptance.mjs --smoke --seed 20260929` and verify every short live UI operation against actual model-backed frames; this smoke is a development gate only and is never included as acceptance evidence.

- [ ] **Step 2: Launch the actual six-hour run**

Run: `node scripts/run_live_interaction_acceptance.mjs --seed 20260929`

Expected: one `main.py` process remains active for at least 21,600 seconds; the runner performs five map draws at 1/2/3/4/5 hours and three vessel/AIS rounds at 1.5/3/4.5 hours through the browser UI.

- [ ] **Step 3: Audit all explicit requirements against retained evidence**

Check `main-report/report.json.summary.wall_seconds`, every ledger event's planned/actual timestamps and applied state, exactly 25 searchable cells in each focus bbox, same-class delete/replacement confirmations, Type II AIS changes, final simulation JSONL state/events, and a subsequent red decision/task assignment per focus area. Do not count queued receipts, screenshots alone, fixture tests, or frame absence as proof of application or task response.

- [ ] **Step 4: Write the validation report from measured results**

Record start/end wall timestamps, seed, model, command line, output paths, all eight scheduled groups and their actual confirmations, per-focus task/decision evidence, final audit status, and every failure/limitation. Do not claim success if any required evidence is missing.


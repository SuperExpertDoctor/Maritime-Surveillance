# Task 5: Command-Boundary Frames and UI Confirmation

Worktree: `/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/live5h-repair`
Branch: `fix/live5h-repair`; implementation started from `d71bb97`.
Scope: Task 5 only. Parent owns full-suite, independent whole-branch review, final validation and preview startup.

## Implementation and Contracts

- `SimulationEngine.step` accepts an optional command-boundary callback. Runtime/intent/population/vessel ordering remains unchanged. After drained commands, the engine refreshes the public runtime/inventory read model and captures a coherent snapshot before red/allocator model work. This callback uses the unchanged simulation time, neutral trigger/action, and `command_boundary: true`; it does not advance the clock, consume a step, or repeat an old heavy decision.
- `SimulationEngine.run` and the live main loop wire that callback to publication. Main skips its step-delay sleep and periodic step logging for command snapshots. Replay persistence uses the existing immutable FramePublisher queue; no delivery or recorder semantics were replaced.
- Paused command application now refreshes the public inventory before publication. Successful paused retries retain their newly generated decision payload. Unrelated paused edits use a neutral payload. A running abort returns a neutral result rather than republishing the preceding heavy decision.
- Vessel receipt confirmation is scoped by mode, episode, and reset generation. Create requires the receipt's matching vessel identity and minimum revision. AIS additionally requires the requested AIS state and requested vessel identity. Delete retains identity/selection until a complete authoritative inventory confirms absence. Missing receipt identity/revision never counts as confirmation. A confirmed result remains confirmed rather than re-locking when that entity later disappears.
- Context switches abort obsolete polling and clear command state. Existing disconnected/replay gating is preserved. Receipt-first and frame-first arrival orders are covered independently for create, delete and AIS.
- Existing focus rows now translate `awaiting_planning`, `resource_blocked`, `waiting_assignment`, `coverage_below_target`, and `freshness_below_target`, and show authoritative `assigned_task_ids` or an explicit no-owner state. Existing layout/components/React APIs are retained.
- Playwright accepts `PLAYWRIGHT_FRONTEND_PORT`, or derives the start port from `PLAYWRIGHT_BASE_URL`; Vite uses `--strictPort`. The unrelated 5180 service and occupied 5181 were not touched. 5195 was verified free and used for all browser runs; each owned test server exits with its runner.

## TDD Evidence

All Python commands below ran from the repair worktree. All npm/Playwright commands ran from its `src/vis/frontend` directory. No credentials, provider requests or historical replay edits were used.

### Python RED

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/test_live_runtime_loop.py -k command_boundary
```

Result: `2 failed, 33 deselected in 0.76s`. Both live and no-server cases reached the blocked model with applied vessel/intent receipts but an empty publication list: `AssertionError: applied commands must publish before the model returns`.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/test_live_runtime_loop.py -k command_boundary_does
```

Result: `1 failed, 35 deselected in 0.55s`. No command-boundary callback reached the step. The completed regression additionally asserts publication precedes model work with no step-delay sleep.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/test_live_runtime_loop.py -k paused_command_boundary
```

Result: `1 failed, 36 deselected in 0.61s`. Paused frame inventory reported `actual_vessel_count == 2` while the applied command had created vessel 3.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/test_live_runtime_loop.py -k running_abort
```

Result: `1 failed, 37 deselected in 0.61s`. The result list still contained the previous heavy decision after a running abort.

### Browser RED

```bash
PLAYWRIGHT_FRONTEND_PORT=5195 npx playwright test --config=playwright.interactions.config.js tests/vessel-interactions.spec.js --grep 'delete receipt|AIS confirmation|create receipt|reset generation'
```

Result: `3 failed, 1 passed (1.0m)`.

- Delete: selection was already cleared, expected `aria-pressed=true`, received `false`.
- AIS: same entity/revision with the wrong requested state prematurely enabled the button.
- Reset: an old same-episode generation poll remained active; expected zero status elements, received one.
- Create identity/revision behavior already passed and was retained as a regression.

```bash
PLAYWRIGHT_FRONTEND_PORT=5195 npx playwright test --config=playwright.interactions.config.js tests/vessel-interactions.spec.js --grep '5x5 canvas'
```

Result: `2 failed`. Both real canvas mouse-drag flows submitted `[10, 10, 15, 15]`, but the UI displayed raw `awaiting_planning` rather than the expected status and had no owner display. Desktop and mobile failed at the status assertion before owner assertions.

### GREEN and Verification

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/test_live_runtime_loop.py tests/env/test_frame_publisher.py
```

Latest result: `48 passed in 2.30s`. Includes actual SimulationEngine plus real FramePublisher/FrameLogger while a model boundary is blocked, coherent same-time intent/vessel snapshots, paused inventory, abort stale-payload prevention, delay, retry, shutdown, wall-budget, port and immutable recorder behavior.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/test_live_runtime_loop.py tests/env/test_frame_publisher.py tests/env/test_coverage_frame.py tests/test_validate_live_adversarial.py
```

Result before the final extra abort regression: `71 passed in 46.16s`. The abort change was subsequently covered by the latest 48-test runtime/frame run above.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_opponent_population.py tests/env/test_intent_api.py tests/mission/test_intent_store.py -k 'not runtime_vessel_motion_stays_in_bounds_across_seeds'
```

Result: `48 passed, 50 deselected in 181.29s (0:03:01)`. The initial unfiltered version was intentionally interrupted after `11 passed in 295.51s`, during unchanged expensive ship navigation. The parent already owns the broad 50-seed baseline; this filtered run does not claim to revalidate that entire group.

```bash
npm ci
npm run build
PLAYWRIGHT_FRONTEND_PORT=5195 npx playwright test --config=playwright.interactions.config.js --output=test-results/task5-interactions
```

Results: dependencies installed from the unchanged lockfile; production build passed (1512 modules, 825ms). Full focused interaction suite: `48 passed (1.3m)`. Includes existing coverage, drawer, telemetry, mixed-maritime, replay-mode gating, HTTP-loss recovery, episode-switch and late-frame tests plus nine new boundary/focus/race cases. Browser output contains the existing `NO_COLOR`/`FORCE_COLOR` warning, no failing browser cases.

The first full interaction attempt was intentionally stopped after `11 passed`, one interrupted test and 33 not run: two old mocked applied receipts omitted vessel identity/revision and could no longer bypass frame confirmation. Those fixture receipts and their follow-up authoritative frames were corrected to the real API shape, then the full 45-test and final 48-test suites passed. Production confirmation was not weakened.

```bash
PLAYWRIGHT_FRONTEND_PORT=5195 npx playwright test --config=playwright.interactions.config.js tests/vessel-interactions.spec.js --grep 'frame arriving' --output=test-results/task5-frame-first
```

Result: `3 passed (6.5s)` for create/delete/AIS frame-before-receipt ordering. Final self-review strengthened identity/revision assertions to wait for actual rendered frame changes before checking locks:

```bash
PLAYWRIGHT_FRONTEND_PORT=5195 npx playwright test --config=playwright.interactions.config.js tests/vessel-interactions.spec.js --grep 'delete receipt|AIS confirmation|create receipt|frame arriving' --output=test-results/task5-confirmation
```

Result: `6 passed (12.2s)`. No production change after the full 48-test browser run. `git diff --check` also passed. All owned test processes and test servers have exited.

## Browser Artifacts and Visual Checks

These are mocked HTTP/WebSocket offline frontend checks, not screenshots from a provider-backed live experiment. Canvas assets are the actual existing renderer assets.

- Preserved desktop viewport: `.superpowers/sdd/live5h-task-5-artifacts/offline-focus-1440-viewport.png` (1440 x 1000).
- Preserved mobile viewport: `.superpowers/sdd/live5h-task-5-artifacts/offline-focus-375-viewport.png` (375 x 812).
- Regenerable originals/full-page images: `src/vis/frontend/test-results/task5-interactions/tests-vessel-interactions--446a2--status-and-owner-at-1440px/` and `tests-vessel-interactions--bc0cb-e-status-and-owner-at-375px/`.
- Both viewport flows use the existing toolbar and actual canvas mouse down/move/up. The submitted bbox is exactly 5 x 5, then authoritative intent ID/status/owner frames update the existing row.
- Automated row checks assert no horizontal overflow, row within viewport, and no overlapping metadata text rectangles. Images were visually inspected: desktop map/sidebar are visible; mobile uses the existing sidebar overlay and scroll behavior. No redesign or global style changes were made.

The UI/UX skill informed stable disabled/async feedback and preserving the existing layout. The browser-testing skill informed real rendered interaction and screenshot inspection; the brief explicitly directed use of the existing JavaScript Playwright harness rather than introducing a new Python harness.

## Self-Review, Limits and Handoff

- No new command schema, external package, population policy or task allocation logic. Automatic command ordering is unchanged.
- The existing asynchronous live publisher may conflate frames under backpressure; the boundary snapshot is enqueued before model work, and every snapshot is durably recorded. This does not claim synchronous socket delivery before a model begins.
- No full Python-suite or provider-backed five-hour acceptance claim. Parent owns final whole-branch verification and independent review. No real model experiment was started.
- `npm ci` reported one existing transitive high advisory. `npm audit --json` identifies `nanoid <3.3.18`, `GHSA-2v37-7h3g-55p8`, custom generators looping indefinitely at zero size. Dependencies/lockfile were not upgraded within this repair.
- Existing sidebar typography/layout are unchanged. Visual checks are scoped to the touched focus row and existing interaction paths, not a redesign/accessibility audit of the whole application.
- Parent's unrelated tracked Task 4 report edits are excluded from this commit.

## Running the Repaired App

The parent will start a separate explicitly offline preview after branch verification. Do not reuse 5180 or assume 5181 is free. Check ports before running either command.

For the existing read-only offline fixture backend (generates fresh temporary fixture replays; does not modify historical outputs):

```bash
/home/shuixia/miniconda3/bin/python scripts/run_visual_fixture_server.py --config configs --port 18766
```

Frontend, from `src/vis/frontend`:

```bash
VITE_BACKEND_PORT=18766 /home/shuixia/.local/bin/npm run dev -- --host 127.0.0.1 --port 5195 --strictPort
```

URL: `http://127.0.0.1:5195`. This fixture is read-only and does not exercise a live advancing model loop. The task's interactive mutation confirmation checks are the mocked interaction suite plus real-engine offline boundary tests above. For the full existing Playwright fixture harness on Linux, set `PLAYWRIGHT_PYTHON=/home/shuixia/miniconda3/bin/python`, `PLAYWRIGHT_NPM=/home/shuixia/.local/bin/npm`, `PLAYWRIGHT_FRONTEND_PORT=5195`, and `PLAYWRIGHT_BACKEND_PORT=18766`; Windows executable defaults remain unchanged.

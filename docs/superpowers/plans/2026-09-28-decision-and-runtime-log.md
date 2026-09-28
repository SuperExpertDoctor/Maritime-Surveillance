# Decision Table and Runtime Log Implementation Plan

**Goal:** Show each LLM allocation decision in a five-column, incrementally updated table and expose live algorithm status in a separate Log tab.

**Architecture:** The model gateway retains the response's reason content and publishes sanitized attempt states. The scheduler and simulation emit one durable decision event after assignment application; recorded frames preserve it for replay. A bounded, cursor-based runtime journal serves live diagnostics independently of conflated frame delivery. React renders decisions and log records as separate views.

**Tech Stack:** Python, FastAPI, React, Vite, pytest, Playwright.

## Global Constraints

- Preserve the region, parameters, AIS, map, and playback controls.
- Do not synthesize missing model reasoning or conflate selected tasks with committed UAV assignments.
- Keep model prompts, credentials, and raw responses out of the runtime status feed.
- Preserve recorded frame compatibility and support old replay files without decision records.

---

### Task 1: Capture model reasoning and attempt status

**Files:** `src/mission/llm_gateway.py`, `src/mission/mission_scheduler.py`, `tests/mission/test_llm_gateway.py`.

- [ ] Add failing tests for response message reason content, sanitized retry/timeout callbacks, and missing reasoning.
- [ ] Verify tests fail for the intended missing behavior.
- [ ] Retain reason content and expose a callback for attempt start, retry, success, and failure; add it to selection interactions.
- [ ] Run the targeted tests.

### Task 2: Record actual decisions and trigger provenance

**Files:** `src/schedule/trigger_manager.py`, `src/schedule/task_allocator.py`, `src/env/simulation.py`, `tests/schedule/test_trigger_manager.py`, `tests/env/test_simulation_integration.py`.

- [ ] Add failing tests covering periodic/event/initial/retry provenance and successful/failed decision event contents.
- [ ] Verify tests fail for missing behavior.
- [ ] Append decision events containing model reasoning, selected task IDs, final committed pairs, status, and trigger source.
- [ ] Run the targeted tests and check recorded frames include decision events.

### Task 3: Offer a loss-aware live runtime journal

**Files:** `src/vis/backend/runtime_journal.py`, `src/vis/backend/server.py`, `main.py`, `tests/vis/test_runtime_journal.py`.

- [ ] Add failing tests for bounded cursor paging, sanitized records, and endpoint delivery.
- [ ] Verify tests fail for missing behavior.
- [ ] Connect the model callbacks and algorithm state changes to the journal; expose `GET /api/runtime/logs?after=...`.
- [ ] Run targeted API and journal tests.

### Task 4: Render decision and log views

**Files:** `src/vis/frontend/src/App.jsx`, `src/vis/frontend/src/components/BottomDrawer.jsx`, `src/vis/frontend/src/App.css`, `src/vis/frontend/src/hooks/useRuntimeLogs.js`, `src/vis/frontend/tests/decision-log.spec.js`.

- [ ] Add failing Playwright coverage for five-column decisions, replay seek filtering, and live log status/retry handling.
- [ ] Verify tests fail for missing behavior.
- [ ] Build responsive table and bounded log list; poll the journal with a cursor and keep replay data separate.
- [ ] Run Playwright tests, frontend build, relevant Python tests, and review diff.

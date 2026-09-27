# Final Fix Wave Report

## Fixed Contracts

- `GET /api/intents` now returns the detached, last simulation-thread-published
  intent snapshot. It no longer attaches owners, writes mission records, queues
  control events, advances the delivery cache, or republishes state on the API
  server thread.
- Focus delivery identity now includes task ID, intent ID, assigned UAV, and
  the controller lease generation. A retained task reassigned to another owner
  receives its focus event without resetting scheduler intent acknowledgement.
- Scheduler correction fixtures explicitly use a 10-second test-only planning
  budget. Production keeps its two-second default and the gateway keeps its
  five-second useful-retry guard.
- The intent owner row now wraps long, multiple task IDs at word boundaries;
  IDs remain inspectable without horizontal overflow on desktop or mobile.

## TDD Evidence

### RED

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_mission_scheduler.py::test_scheduler_retries_a_selection_with_a_malformed_information_version tests/mission/test_mission_scheduler.py::test_live_scheduler_corrects_infeasible_simultaneous_selection
```

Output: `2 failed in 0.07s`. Both fixtures used the production two-second
default, leaving less than the gateway's useful retry budget.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/env/test_intent_api.py::test_public_intent_read_does_not_attach_owners_or_queue_control_events
```

Output: `1 failed in 0.73s`; a GET mutated the retained task record.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/env/test_intent_api.py::test_reassigned_retained_focus_is_delivered_to_replacement_owner
```

Output: `1 failed in 0.74s`; reassignment did not advance delivery sequence.

```bash
env -u NO_COLOR PLAYWRIGHT_FRONTEND_PORT=5195 npx playwright test --config playwright.interactions.config.js tests/vessel-interactions.spec.js --grep '5x5 canvas focus'
```

Output: `2 failed`; desktop and mobile each reported `fits: false` for four
production-shaped owner task IDs.

### GREEN

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_mission_scheduler.py::test_scheduler_retries_a_selection_with_a_malformed_information_version tests/mission/test_mission_scheduler.py::test_live_scheduler_corrects_infeasible_simultaneous_selection
```

Output: `2 passed in 0.08s`.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/env/test_intent_api.py
```

Output: `11 passed in 7.42s`.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/control/heuristic/test_probe.py tests/mission/test_probe_navigation_integration.py
```

Output: `22 passed in 16.12s`.

```bash
env -u NO_COLOR PLAYWRIGHT_FRONTEND_PORT=5195 npx playwright test --config playwright.interactions.config.js tests/vessel-interactions.spec.js --grep '5x5 canvas focus' && npm run build
```

Output: `2 passed (7.6s)` and `vite build` completed in `760ms`.

## Scope and Limitations

- No provider calls, credentials, dependencies, or historical replay data were
  changed. Browser tests used fixture frames and the dedicated port 5195.
- The requested probe test suites pass. The additional parent dynamics
  diagnostic was not expanded into a new parameterized acceptance test in this
  wave; its strict existing evidence path is unchanged.
- The parent-owned Task 4 report was not staged or changed by this commit.

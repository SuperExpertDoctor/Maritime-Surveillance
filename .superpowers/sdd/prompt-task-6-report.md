# Task 6 Offline Measurement Report

## Commands And Results

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_mission_scheduler.py::test_selection_interaction_before_any_payload_has_empty_user_payload
```

Result: `1 passed in 0.08s`.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/time -f 'elapsed=%e' python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_coverage_decision_budget.py::test_seed_42_initial_scenario_wire_payload_is_lossless_and_under_30_kib
```

Result: `1 passed in 47.87s`; shell timing `elapsed=48.32`.

The focused regression uses an injected recording gateway and the requested
`ModelResult("offline", False, None, ("decision_deadline_exceeded",),
"timeout")`. It runs seed-42 engine steps three times, performs no network or
provider call, verifies each compact request has no `instructions`, decodes
losslessly to the preserved canonical snapshot, retains candidate ID/order, and
keeps each initial-scenario system-plus-user request below 30 KiB. A direct
test verifies `selection_interaction()` uses `{}` before any payload exists.

## Measurements

First request, UTF-8 bytes: canonical compact-comparison payload 34,476;
system 4,940; compact user wire 20,805; transmitted system + user 25,745. The
canonical comparison is not scheduler `prompt_bytes`, which uses default JSON
spacing. The largest snapshot value
contributions were feasible edges 14,814, candidates 5,727, coverage summary
2,096, resources 2,192, coverage constraint 1,934, contacts 1,138, and prompt
sources 452 bytes.

These are JSON byte counts, not provider token counts or latency evidence.
The full affected offline suite was intentionally not run here; root owns that
final verification after this commit. No live calls were made.

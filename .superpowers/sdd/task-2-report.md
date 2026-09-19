# Task 2 Implementation Report

Status: DONE

## Implementation

- Added explicit `required_cells` and SAR-projected `scan_footprints` to coverage plans.
- Kept planner footprint accounting aligned with the runtime cell-centre aperture test.
- Made coverage route snapshots fail closed when an unflown scan leg is blocked, including an explicit uncovered-cell ledger.
- Reused the shared `(cols, rows)` half-open bbox helper for rectangular track regions.
- Applied `search_min_cells` before candidate creation and retained fragment alerts in `CandidatePool`.
- Extended conflict detection to include current-pose overlap and a full horizon when one path ends earlier.
- Added regression coverage for fractional swaths, explicit direction feasibility, fail-closed obstacles, candidate fragments, rectangular bboxes, and conflict horizons.

## TDD Evidence

RED command:

```text
pytest tests/utils/test_coverage_planner.py tests/control/heuristic/test_coverage.py tests/utils/test_conflict_detector.py tests/schedule/test_candidate_extractor.py -q
```

Result: `6 failed, 45 passed`. The failures were the missing footprint contract, missing explicit-direction argument, fail-open obstacle behavior, current-pose conflict omission, short-path horizon omission, and the new fragment fixture setup.

GREEN command:

```text
pytest tests/utils/test_coverage_planner.py tests/control/heuristic/test_coverage.py tests/control/heuristic/test_coverage_closed_loop.py tests/control/heuristic/test_coverage_sensor_geometry.py tests/utils/test_conflict_detector.py tests/schedule/test_candidate_extractor.py tests/schedule/test_state_manager.py -q
```

Result: `93 passed in 5.94s`.

Additional scan integration check:

```text
pytest tests/mission/test_coverage_scan_integration.py -q
```

Result: `9 passed in 3.93s`.

`git diff --check` passed. The planned simulation integration command remains environment-blocked by the repository's mandatory `LONGCAT_API_KEY` assertion; supplying a dummy key starts the live decision path and was stopped after it entered a long external call. No Task 2 assertion failure was observed.

## Files Changed

- `src/utils/coverage_planner.py`
- `src/control/heuristic/coverage.py`
- `src/schedule/state_manager.py`
- `src/schedule/candidate_extractor.py`
- `src/mission/prompt_window.py`
- `src/utils/conflict_detector.py`
- `tests/utils/test_coverage_planner.py`
- `tests/control/heuristic/test_coverage.py`
- `tests/schedule/test_candidate_extractor.py`
- `tests/utils/test_conflict_detector.py`

## Concern

The fail-closed route state is now exposed by `route_snapshot()` and the uncovered-cell ledger. Releasing higher-level task bindings and emitting the corresponding audit event remain covered by the lifecycle/control work in Tasks 3 and 5.

# Branch1 Task 1 Execution

Date: 2026-09-19
Worktree: `/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/branch1-correctness-reliability-performance`
Starting SHA: `0731b71427945e5fc463babcaf717240465a8c39`

## RED

Command:

```bash
pytest tests/regressions/test_branch1_contracts.py -q
```

Output:

```text
ERROR: found no collectors for /home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/branch1-correctness-reliability-performance/tests/regressions/test_branch1_contracts.py

==================================== ERRORS ====================================
______________________ ERROR collecting tests/regressions ______________________
...
tests/regressions/test_branch1_contracts.py:1: in <module>
    from src.schedule.config_loader import ConfigLoader, search_min_cells
E   ImportError: cannot import name 'search_min_cells' from 'src.schedule.config_loader' (/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/branch1-correctness-reliability-performance/src/schedule/config_loader.py)
=========================== short test summary info ============================
ERROR tests/regressions - ImportError: cannot import name 'search_min_cells' ...
1 error in 0.66s
```

Failure reason: the new contract intentionally imported the not-yet-existing `search_min_cells` helper. The bbox helper was also absent at RED time.

## GREEN

Command:

```bash
pytest tests/regressions/test_branch1_contracts.py tests/regressions/test_branch1_contract_matrix.py -q
```

Output:

```text
....                                                                     [100%]
4 passed in 0.44s
```

The implementation exposes `grid_bbox_from_center` with `(cols, rows)` ordering and bounded half-open coordinates, and `search_min_cells` reads the configured grid value. The audit matrix maps `B1-001` and `B1-002` to their focused tests and source files.

## SHA and scope

The execution started from the required SHA above. The implementation SHA is recorded by the Task 1 commit. `git diff --check` passed during self-review.

Files changed for Task 1:

- `src/schedule/config_loader.py`
- `src/schedule/datatypes.py`
- `tests/regressions/test_branch1_contracts.py`
- `tests/regressions/test_branch1_contract_matrix.py`
- `docs/validation/2026-09-19-branch1-execution.md`

Concern: `src/schedule/config_loader.py` retains the pre-existing formal-acceptance UAV minimum literal `10` in `validate_uav_count`. It is unrelated to the new grid/search contract and was left unchanged because Task 1 permits only the two specified production modules and the brief does not authorize changing that validation policy.

## Branch1 Tasks 2-10 Validation Gate

Date: 2026-09-20
Worktree: `/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/branch1-correctness-reliability-performance`
Task 9 implementation SHA: `ae85237`

### Static audit

Command:

```bash
python scripts/validate_branch1_audit.py --json
```

Result: `passed`, 126 production/source files scanned, 0 findings. The audit covers generic exception accounting, truthy heading checks, exact base/position comparisons, mode defaulting, class-name introspection, queue polling, modulo sentinels, hard-coded bonuses and search-area literals. Test compatibility fixtures are outside the production gate; any future exception is required to carry a local `branch1-audit` annotation.

### Task 9 performance and correctness

Commands:

```bash
pytest tests/performance/test_branch1_hotspots.py tests/schedule tests/env/test_coverage_frame.py -q
python scripts/evaluate_goal.py --scenario branch1-hotspots --repeat 10
```

Results:

- `150 passed in 29.83s`; benchmark mean `164.2512 ms` on the fixed 375,154-cell rectangular fixture.
- Hotspot JSONL-compatible JSON output: seed `42`, HEAD `ae85237`, rank p50/p95 `179.598/429.097 ms`, light snapshot p50/p95 `0.019/0.047 ms`.
- Cache peak/limits: route `100/512`, metrics `100/512`, geometry `2048/2048`, task cells `2048/2048`.

### Scenario evidence matrix

| Scenario | State | Evidence | Result |
| --- | --- | --- | --- |
| Rectangular grid and bbox | fixture | `tests/regressions/test_branch1_contracts.py` | PASS |
| New obstacle fail-closed | fixture | coverage route/snapshot regression tests | PASS |
| Weather replan retains unaffected task | fixture | `tests/control/heuristic/test_coverage.py` | PASS |
| Target disappearance and control recovery | fixture | lifecycle/control regression tests | PASS |
| Recording logger failure and durable flush | fixture | `tests/env/test_frame_publisher.py` | PASS |
| WebSocket concurrency and replay partial line | replay | server/replay adapter tests | PASS |
| Passive positioning noise | fixture | `tests/sensor/test_passive.py` | PASS |
| 30/60/120-minute live simulation | live | requires configured `LONGCAT_API_KEY` | BLOCKED: live model credential unavailable |

Live, fixture, and replay paths remain explicitly labeled. No live result is represented as a fixture success; the only unresolved acceptance item is the credential-gated live run above.

### Task 10 full validation

Commands:

```bash
LONGCAT_API_KEY=offline-test pytest tests/regressions tests/control tests/env tests/mission tests/schedule tests/utils tests/vis -q
npx playwright test tests/coverage-metrics.spec.js tests/mixed-maritime.spec.js
REPLAY_ACCEPTANCE_DIR=/tmp/branch1-replay-acceptance-MwQbxZ npx playwright test --config playwright.replay.config.js
npm run build
npm run test:acceptance
```

Results:

- Python fixture suite: `1654 passed in 1240.75s`.
- Static audit: `126` production/source files, `0` findings.
- Frontend coverage and mixed-maritime suites: `15 passed`.
- Replay restoration suite with real v06/v07 JSONL artifacts: `7 passed in 2.1m`.
- Frontend build: passed; acceptance smoke suite: `2 passed`.
- The live 30/60/120-minute run remains intentionally blocked until `LONGCAT_API_KEY` is supplied; no live result is substituted with the offline fixture.

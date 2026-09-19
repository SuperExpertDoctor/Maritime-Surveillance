# Task 5 Implementation Report

Status: DONE (with environment-gated integration tests)

## Implementation

- Added a shared `ControlError` contract with stable error codes while
  preserving `ValueError`/`RuntimeError` compatibility for existing callers.
- Classified malformed probe evidence as `ProbeValidationError` and mapped it
  to an explicit simulation recovery reason.
- Reused `_poses_match` for recovery-plan endpoints, including 2D base poses
  and the existing position/heading tolerances.
- Kept collision-free candidate exhaustion as `UnsafeControlState` rather
  than an invalid command, and changed server conflict handling to use
  `isinstance` against the vessel command exception type.
- Added a uniform-grid RRT node index, geometric parent shortlists, bounded
  anchor evaluation, timing metadata, and an explicit `ObstaclePlanningTimeout`.
  Planning misses remain raised as route failures instead of being silently
  discarded.

## TDD Evidence

Focused command:

```text
pytest tests/control/heuristic/test_probe.py tests/control/test_safety.py tests/control/test_route_snapshot.py tests/utils/test_obstacle_avoider.py tests/env/test_server_runtime.py -q
61 passed in 12.14s
```

After the RRT/index changes:

```text
pytest tests/utils/test_obstacle_avoider.py -q
5 passed in 0.52s
pytest tests/utils/test_conflict_detector.py -q
6 passed in 0.42s
```

The broader control/server command produced `279 passed, 4 failed`; all four
failures stop in `SimulationEngine` construction because the environment does
not provide `LONGCAT_API_KEY`, before the tested behavior runs. The impacted
navigation/simulation command produced `20 passed, 29 failed` with the same
configuration blocker; no RRT traceback was reported.

## Files Changed

- `src/control/common/safety.py`
- `src/control/heuristic/safety.py`
- `src/control/heuristic/probe.py`
- `src/control/heuristic/return_to_base.py`
- `src/control/common/coordinator.py`
- `src/env/simulation.py`
- `src/utils/obstacle_avoider.py`
- `src/vis/backend/server.py`
- focused control, obstacle, and server tests

# Task 3 Implementation Report

Status: DONE

## Implementation

- Added explicit `ControlOutcome` values for clean, clipped, masked, invalid, and unsafe control paths.
- Successful safety adjustments no longer increment the invalid-command streak; rejected commands still do, while `UnsafeControlState` is classified and recovered separately.
- Added generation- and route-aware `save_coverage_task`, `restore_coverage_task`, and `clear_saved_coverage` APIs, including restoration after target loss and save hooks for return/holding/preemption.
- Wrote the first real tracking timestamp when a TRACK command is applied and preserved existing release cleanup.
- Made trigger deduplication independent of the consumed pending queue, with explicit five-minute expiry and light-trigger throttling.
- Removed AIS history keys when a deleted vessel no longer has a live MMSI owner.
- Preserved the fail-closed unavailable-route contract in the watchdog regression.

## TDD Evidence

RED command:

```text
pytest tests/control/test_coordinator.py::test_clipping_does_not_increment_invalid_command_counter tests/control/test_coordinator.py::test_unsafe_control_state_is_classified_and_recovered_separately tests/control/heuristic/test_task_flow.py::test_saved_coverage_round_trip_restores_generation_and_route tests/mission/test_information_loop.py::test_light_trigger_respects_five_minute_dedup_window tests/env/test_contact_lifecycle.py::test_sar_detection_and_contact_loss_publish_type_ii_stage_changes tests/mission/test_feature_sensing_integration.py::test_deleted_vessel_removes_ais_history_key -q
```

Result: `5 failed, 1 passed`. The failures exposed each missing contract: clipped commands counted as invalid, unsafe had no classification, TaskFlow lacked save/restore, light dedup was queue-local, and vessel deletion left AIS history.

GREEN checks:

```text
pytest tests/control --ignore=tests/control/test_simulation_ownership.py -q
```

Result: `258 passed in 5.51s`.

```text
pytest tests/env/test_contact_lifecycle.py tests/mission/test_information_loop.py tests/mission/test_contact_release.py tests/mission/test_feature_sensing_integration.py tests/control/heuristic/test_coverage_progress.py -q
```

Result: `47 passed in 33.47s`.

Additional checks: `python -m compileall` and `git diff --check` passed. The four tests in `tests/control/test_simulation_ownership.py` remain blocked at `SimulationEngine` construction by the repository's mandatory missing `LONGCAT_API_KEY`; this is the same baseline configuration failure, not a Task 3 assertion failure.

## Files Changed

- `src/control/common/coordinator.py`
- `src/control/common/safety.py`
- `src/control/heuristic/task_flow.py`
- `src/env/simulation.py`
- `src/schedule/trigger_manager.py`
- `tests/control/common and heuristic lifecycle regressions`
- `tests/env/test_contact_lifecycle.py`
- `tests/mission/test_information_loop.py`
- `tests/mission/test_feature_sensing_integration.py`

## Concern

Saved coverage routes are retained as immutable metadata and task restoration is now explicit. A restored controller still replans from the current observation on its next tick, so stale physical waypoints are never executed after a return or target-loss transition.

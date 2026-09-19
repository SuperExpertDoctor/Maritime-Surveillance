# Task 6 Implementation Report

Status: DONE

## Implementation

- Extended contract snapshots to detach NumPy arrays, dataclasses, sets,
  nested containers, and public attributes of custom mutable payloads. Frozen
  mappings remain immutable, copyable, and JSON-compatible.
- Normalized nested coverage progress recursively in frame output and preserved
  an explicit zero SAR heading.
- Added monotonic event IDs and a bounded `(last, now]` event window with
  detached event payloads, preventing boundary duplication between frames.
- Forced normalized replay frames to `mode=replay` and rejected malformed
  non-list `scenario_vessels` input at the adapter boundary.
- Changed passive position release to intersect measured noisy bearing rays;
  the legacy truth argument is ignored for compatibility and simulation no
  longer passes it into the resolver.

## TDD Evidence

Task command:

```text
pytest tests/control/test_contracts.py tests/env/test_coverage_frame.py tests/mission/test_maritime_acceptance.py tests/sensor/test_passive.py tests/vis/test_replay_adapter.py -q
51 passed in 17.46s
```

Related sensor, information, SAR, and replay acceptance regressions:

```text
pytest tests/mission/test_feature_sensing_integration.py tests/mission/test_information_update.py tests/mission/test_coverage_scan_integration.py tests/env/test_replay_acceptance_server.py -q
28 passed in 15.18s
```

## Files Changed

- `src/control/common/contracts.py`
- `src/schedule/state_manager.py`
- `src/vis/backend/frame_builder.py`
- `src/sensor/passive.py`
- `src/env/simulation.py`
- `src/vis/backend/replay_adapter.py`
- focused contract, frame, sensor, acceptance, and replay tests

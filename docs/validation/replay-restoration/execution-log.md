# Replay Restoration Execution Log

This log records evidence for the replay-restoration implementation plan. A
line is added for each task only after its task gate has been checked. Paths
under `outputs/validation/replay-restoration/` are ignored runtime artifacts;
the source logs listed below are read-only inputs.

## T00 Baseline

- Implementation branch: `feature/replay-visual-restoration`
- Starting `branch1` commit: `5cca8b8`
- Design evidence baseline commit: `bc8d76f`
- Python baseline command:
  `LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/mission/test_simulation_flow.py tests/mission/test_mission_scheduler.py tests/control/test_simulation_ownership.py tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py -q`
- Python baseline result: `32 passed in 11.66s`
- Historical input directory: `/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/outputs`
- Historical input SHA-256 and size/frame counts:
  - `simulation_20260812_200432.jsonl`: `8759a25cd8e14f17a63b26df74d0359b0a1ca90de62f0efc015c73453abc32aa`, 190302543 bytes, 471 frames.
  - `simulation_20260916_204010.jsonl`: `ff07850296485e2a2596a26639b936eff0f8bfae93743aa4908809c50367957e`, 1281703 bytes, 20 frames.
  - `simulation_20260916_210604.jsonl`: `6a9800c21139039d9c0907e795e55da692cec90f21c432cfbc4465b6849b8ebe`, 1302263 bytes, 20 frames.
- Historical statistics agree with `baseline-manifest.json`: old final coverage is
  `56.35528330781011`; both new logs end at `0.0`. The old final frame has 9
  non-empty planned paths and 10 non-empty mission routes; each new final frame
  has zero of both.
- Existing frontend build: passed (`vite v8.2.0`, 1508 modules).
- Existing frontend acceptance: `1 passed, 1 failed`; the pre-existing failure is
  `tests/acceptance.spec.js:143`, where the I-class vessel button remained
  disabled after connection. The sensor-beam test passed. This remains an open
  baseline item until the responsible task supplies a regression result.
- Baseline screenshots: `t00-baseline/legacy-t20.png`,
  `t00-baseline/current-t20.png`, and `t00-baseline/sensor-beams-live.png`.
- Browser render timing baseline: `t00-baseline/performance.json` contains 500
  samples from Chromium `151.0.7922.34` at a 900x700 canvas. P50 is
  `0.6000003814697266 ms` and P95 is `17.5 ms`; the same frame set and browser
  command will be reused at T15.

## Status Vocabulary

`PASS` means the task gate has fresh evidence. `OPEN` means a baseline or
environment issue is recorded but not silently counted as a pass. `N/A` is used
only when the plan explicitly defines a capability as compatibility or an
extension interface.

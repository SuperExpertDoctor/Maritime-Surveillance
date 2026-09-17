# Persistent Coverage Execution Record

This record separates implementation evidence from the design, plan, and validation matrix. `NOT_RUN` is not a pass result.

## Baseline

Task / Gate: Repository baseline before T01
Command and working directory: `python -m pytest tests -q` from repository root
Exit code: 0
Transport: none
Config hash / seeds / actual simulation end: not applicable
Raw artifacts: none
Metric measurement: native | legacy_reconstructed: not applicable
Result: PASS
Evidence and remaining issue: Python 3.13.11, NumPy 1.26.4, pytest 9.0.3; `1385 passed in 545.60s (0:09:05)`. Frontend dependencies were present; frontend build and browser checks remain NOT_RUN.

## T01: Independent oracle and real-motion rig

Command and working directory: `python -m pytest tests/mission/test_coverage_oracle.py -q` from repository root
Red command: same command after the behavior tests were written and before the implementation; the placeholder helper produced `4 failed` with `NotImplementedError` in the first three oracle tests and `FileNotFoundError` for the not-yet-created baseline fixture.
Green command: same command after the T01 helper and fixture implementation
Green result: `4 passed in 0.33s`
Focused rig smoke command: a two-tick `make_coverage_rig` run
Focused result: `records=2 moved=True sar=True fuel=0.966666667`
Files: `tests/mission/coverage_helpers.py`, `tests/mission/test_coverage_oracle.py`, `tests/fixtures/coverage_audit_baseline.json`
Metric measurement: independent Python-set oracle; no production `CoverageMetrics` import
Result: PASS for the T01 oracle contract
Evidence and remaining issue: the helper routes commands through `UAVEntity`, `ObservationProvider.build`, `SafetyEnvelope.apply`, `UAVDynamicsExecutor.execute`, and `SARSensor.compute_swath_footprint`. It does not use the old V07 validation hook or teleport pose after initialization. Rig imaging-success conditions remain intentionally unasserted for T04/T05.

## Task Status

T01: DONE_WITH_CONCERNS
T02-T16: NOT_RUN

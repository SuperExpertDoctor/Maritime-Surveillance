# T01 Implementation Report

Status: DONE_WITH_CONCERNS

## Commit

Initial commit SHA before this report-only amendment: `ca49f89`.

## Files changed

- `tests/mission/coverage_helpers.py`
- `tests/mission/test_coverage_oracle.py`
- `tests/fixtures/coverage_audit_baseline.json`
- `docs/validation/persistent-coverage-execution.md`
- `.superpowers/sdd/task-1-report.md`

No production algorithm files were modified. User-provided untracked input documents and `.superpowers/sdd/progress.md` were preserved.

## Test evidence

Red command:

```text
python -m pytest tests/mission/test_coverage_oracle.py -q
```

Initial collection output was an expected missing-helper error. After adding a deliberate placeholder, the same command reached behavioral red:

```text
4 failed in 0.37s
NotImplementedError
FileNotFoundError: tests/fixtures/coverage_audit_baseline.json
```

Green command:

```text
python -m pytest tests/mission/test_coverage_oracle.py -q
```

Actual output:

```text
....                                                                     [100%]
4 passed in 0.33s
```

Focused rig smoke output:

```text
records=2 moved=True sar=True fuel=0.966666667
```

`python -m compileall -q tests/mission/coverage_helpers.py tests/mission/test_coverage_oracle.py` also exited 0.

## Implementation summary

`oracle_coverage` uses only the event shape specified by T01, filters to SAR events, applies the left-open/right-closed time window, intersects a Python-set domain, deduplicates repeated cells, and returns null percentage for an empty domain.

`CoverageRig` owns the real observation, safety, execution, UAV, state, and SAR sensor objects. Each tick builds an observation, obtains a deterministic coverage command, applies safety, executes real dynamics, computes the actual SAR footprint, advances simulation time, and exposes the contracted diagnostics. The rig uses an empty obstacle mask and does not import or invoke the old V07 validation hook.

The audit JSON records both original log paths, seed 42, common end time 177, the requested historical percentages and counts, dynamic/fixed denominators, frame count, and unique simulation times. Percentage tests derive their expected values from counts and denominator rather than asserting approximate historical decimals exactly.

## Self-review

- Oracle behavior is independent of production coverage metrics.
- Duplicate and out-of-order events are handled as set membership.
- EO events cannot contribute to SAR coverage.
- Pose is initialized once in the rig factory; subsequent movement is through the executor and `UAVEntity.apply_motion`.
- The specified `dt_min` values 1.0 and 0.25 are accepted.
- Only T01 files are intended for staging.

## Concerns

- The rig controller is intentionally a deterministic straight-line command source. It is a real-motion integration fixture, not the future closed-loop scan planner; successful imaging and full-bbox coverage remain deferred to T04/T05.
- The historical source JSONL files are not present in this checkout, so the baseline JSON preserves the required paths and recorded audit values without reconstructing or rerunning those logs.
- The full repository regression was not rerun for this test-only task; the contracted T01 command and focused smoke/compile checks were run.

## T01 Follow-up Fix Evidence

Reviewer issue 1 is fixed: the repeated-event test now compares the duplicated event input and its reversed order directly to the exact expected result (`2` cells, `200` km2, `50.0` percent), and also compares a deduplicated input to that same result.

Reviewer issue 2 is fixed: a committed parametrized test exercises `CoverageRig` at both `dt_min=1.0` and `dt_min=0.25`. It verifies executor-driven pose movement, positive distance, simulation/state time advancement, fuel consumption, SAR imaging, and the complete contracted tick-key set. It intentionally does not assert full-bbox imaging success; that remains deferred to T04/T05.

Red command:

```text
python -m pytest tests/mission/test_coverage_oracle.py -q
```

Actual focused red output after adding the new test with an intentionally incorrect distance expectation:

```text
..FF..                                                                   [100%]
2 failed, 4 passed in 0.39s
```

Green command:

```text
python -m pytest tests/mission/test_coverage_oracle.py -q
```

Actual focused green output:

```text
......                                                                   [100%]
6 passed in 0.36s
```

Self-review: the follow-up does not modify production algorithms, user-provided input documents, or `.superpowers/sdd/progress.md`; the unused `BaseObservation` import is removed. The red failure was an intentionally wrong test expectation used to document the TDD cycle, and the final assertion derives distance from the required `160 / 10 / 60` cells/min rate.

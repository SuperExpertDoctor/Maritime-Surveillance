# Task 1: Versioned Lossless Payload

Status: DONE

## Implementation

- Added `encode_selection_payload` and `decode_selection_payload` for `mission-prompt/v2`.
- Compact UAV options into ordered rows while preserving full-precision numeric values.
- Drop instructions from the wire payload and omit candidate eligibility only when it exactly matches the edge table.
- Preserve unknown snapshot fields and detach inputs/outputs with `deepcopy`; support unversioned legacy payloads.
- Added focused regression coverage for round trips, empty options, shared UAVs, eligibility mismatches, malformed rows, unknown versions, legacy detachment, and unsupported option fields.

## TDD Evidence

RED command:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/mission/test_prompt_payload.py
```

Result: collection failed with `ModuleNotFoundError: No module named 'src.mission.prompt_payload'`, as expected before module creation.

GREEN command:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/mission/test_prompt_payload.py
```

Result: `8 passed in 0.02s`.

Additional check: `git diff --check` passed. A broader repository run was stopped after unrelated tests failed due to missing `LONGCAT_API_KEY` configuration (26 failed, 433 passed at interruption); focused payload tests pass independently.

## Files Changed

- `src/mission/prompt_payload.py`
- `tests/mission/test_prompt_payload.py`
- `.superpowers/sdd/prompt-task-1-report.md`

## Concern

No payload-specific concerns. The repository-wide run is not a valid clean signal here because unrelated integration tests require external LLM configuration.

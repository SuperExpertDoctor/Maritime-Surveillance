# Prompt Task 4 Report

Status: DONE

## Implementation

- Decision-maker `max_tokens` now accepts only integer values from 1 through
  16384. Its YAML default remains 4096; all other role budgets retain their
  exact-value validation.
- The mission scheduler no longer supplies a decision-maker output override,
  allowing the gateway to resolve the configured role budget.
- YAML documents the configurable decision-maker range and default without
  changing thinking mode, temperature, or timeouts.

## TDD Evidence

RED:

```text
pytest -q tests/mission/test_llm_gateway.py -k 'decision_maker_accepts_configurable_output_budget or decision_maker_rejects_invalid_output_budget or other_required_role_token_budget_is_validated'
9 failed, 4 passed, 106 deselected

pytest -q tests/mission/test_coverage_decision_budget.py -k 'scheduler_uses_configured_decision_maker_output_budget or scheduler_default_decision_maker_output_budget_reaches_transport'
1 failed, 1 passed, 2 deselected
```

The failures were the existing exact-4096 decision-maker validation.

GREEN:

```text
pytest -q tests/mission/test_llm_gateway.py tests/mission/test_coverage_decision_budget.py tests/schedule/test_llm_client.py tests/test_runtime_configuration.py
174 passed in 16.79s
```

## Coverage

- Boundary configuration accepts 1, 4096, 8192, and 16384; rejects 0, -1,
  16385, bool, float, and string values.
- Scheduler integration changes only decision-maker configuration to 8192 and
  observes 8192, enabled thinking, and the shared 60-second deadline minus the
  1-second reserve (59-second transport allowance).
- Default scheduler integration still observes 4096 at the transport.
- Output-length retries cover 8192, 16384, 16384, preserving the existing
  16384 cap. Existing role-client coverage verifies retries share one deadline
  rather than receiving a fresh provider allowance each attempt.

## Self Review

```text
git diff --check
```

Passed with no whitespace errors. No live provider calls were made.

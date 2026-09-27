# Prompt Task 5 Report

Status: DONE

## Implementation

- Gateway call records distinguish UTF-8 system/user/wire input byte counts,
  prompt format version, configured token budget, and each attempt's current
  message byte count.
- `initial_failure_category` captures the first typed transport, truncation,
  validation, configuration, or budget failure without changing terminal
  `failure_category`. Validation is recorded before a later deadline check can
  turn the final result into `timeout`.
- Scheduler preserves canonical `prompt_bytes` and forwards explicitly named
  wire metrics through allocator timing and interaction data.
- Public details and final reports export bounded attempt records only. Their
  usage projection allows only integral non-bool `prompt_tokens`,
  `completion_tokens`, and `total_tokens`; request bodies, raw output,
  provider attempt channels, and arbitrary usage fields remain absent.

## TDD Evidence

RED:

```text
pytest -q tests/mission/test_llm_gateway.py -k 'validation_cause_survives_deadline_expiry'
1 failed, 124 deselected

pytest -q tests/vis/test_decision_details.py -k 'numeric_diagnostics'
1 failed, 7 deselected
```

The first failure showed `initial_failure_category == timeout` when invalid
validation consumed the deadline. The second showed an arbitrary
`usage.provider_secret` crossing the public boundary.

GREEN:

```text
pytest -q tests/mission/test_llm_gateway.py tests/vis/test_decision_details.py tests/test_live_runtime_loop.py tests/schedule/test_llm_client.py tests/mission/test_coverage_decision_budget.py
218 passed in 18.63s

git diff --check
```

No whitespace errors and no live provider calls were made.

## Baseline Note

`tests/vis/test_decision_details.py::test_real_sdk_explicit_provider_channels_and_redaction`
previously expected `thinking.disabled`. Both base `a565392` and the unchanged
configuration specify `decision_maker.thinking: enabled`; root reproduced the
same baseline failure on unchanged main. The assertion now verifies `enabled`.

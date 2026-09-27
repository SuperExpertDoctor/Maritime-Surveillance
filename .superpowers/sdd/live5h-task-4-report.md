# Task 4 Report: Uniform LLM Deadlines and Useful Retry Budgets

## Implemented

- Made `providers.longcat.timeout_seconds` a 120-second total budget for one
  logical model call, including all validation/correction retries. The timer
  starts before request setup, so setup time is included.
- The effective total deadline is the earlier of that provider budget and a
  caller-provided deadline. A caller deadline shorter than the configured
  budget therefore wins; a longer one cannot extend the configured total.
  An optional transport deadline is also capped at this effective total.
- Passed the shrinking remaining timeout to every synchronous transport call
  and continued rejecting any response received after the effective deadline.
  This bounds cooperative transports (including the production SDK). An
  injected transport that ignores its timeout cannot be interrupted by the
  gateway; its late result is rejected rather than accepted.
- Retained the existing retry cap, strict JSON/text validation, role/model
  bindings, and `decision_deadline_exceeded` timeout result contract.
- Added a 5.0-second useful-retry threshold. The first request remains allowed
  under a shorter caller deadline; subsequent retries start only when at least
  5.0 seconds remain. Timeout backoff also preserves that response budget.
- Logged a deliberate skipped retry on the logical call without fabricating a
  transport attempt: `retry_skipped_reason=insufficient_retry_budget`,
  `retry_remaining_seconds`, and `retry_minimum_seconds`.
- Kept connectivity probes independent: `request_probe` still makes exactly
  one request, with 32 output tokens and disabled thinking; its supplied probe
  timeout is its own total budget.

## TDD Evidence

### RED

Command:

```bash
pytest -q tests/mission/test_llm_gateway.py -k 'role_requests_without_caller_deadline or shorter_caller_deadline or truncation_stops_before_retry or truncation_retries_when_useful'
```

Output: `5 failed, 1 passed, 99 deselected in 0.63s`.

The omitted-deadline red commander, contact assessor, and reviewer requests
each retained a `validation` result because every retry received a fresh 120
seconds. A truncation with only four seconds left sent a second request and
returned success. The meaningful-retry case also gave its second request a
fresh 120 seconds instead of the six seconds remaining.

Audit-field RED command:

```bash
pytest -q tests/mission/test_llm_gateway.py -k 'truncation_stops_before_retry_with_less_than_useful_budget or role_request_without_caller_deadline_rejects_late_response'
```

Output: `1 failed, 3 passed, 104 deselected in 0.51s`.

The skip behavior existed, but the call log had no
`retry_skipped_reason` field. The late-response role tests were already green
from the total-deadline implementation.

### GREEN

Focused regressions after implementation:

```bash
pytest -q tests/mission/test_llm_gateway.py -k 'truncation_stops_before_retry_with_less_than_useful_budget or role_request_without_caller_deadline_rejects_late_response or role_requests_without_caller_deadline or shorter_caller_deadline or truncation_retries_when_useful'
```

Output: `9 passed, 99 deselected in 0.39s`.

Gateway suite:

```bash
pytest -q -o 'markers=timeout: legacy timeout marker' tests/mission/test_llm_gateway.py
```

Output: `108 passed in 13.58s`.

Final green task-scoped suite:

```bash
pytest -q -o 'markers=timeout: legacy timeout marker' tests/mission/test_llm_gateway.py tests/mission/test_coverage_decision_budget.py tests/mission/test_contact_assessor.py tests/schedule/test_llm_client.py
```

Output: `216 passed in 20.56s`.

All tests used injected scripted/fake-clock transports or monkeypatched SDK
clients. No external provider request was made.

## Independently Existing Failure

Command:

```bash
pytest -q -o 'markers=timeout: legacy timeout marker' tests/mission/test_red_commander.py
```

Output: `6 failed, 163 passed in 31.20s`.

The failures are the established stale red-commander motion/prompt fixtures,
not deadline behavior. No production motion or prompt rules were changed:

- `test_each_active_ship_must_receive_a_minimum_maneuver` (three parameters)
- `test_valid_maneuvers_and_inclusive_bounds_are_accepted_unchanged` (two parameters)
- `test_prompt_states_schema_authority_motion_semantics_and_config_limits`

## Files Changed

- `configs/llm_params.yaml`
- `src/mission/llm_gateway.py`
- `tests/mission/test_llm_gateway.py`
- `tests/schedule/test_llm_client.py`

## Backward Compatibility

The public `request_json`, `request_text`, and `request_probe` signatures are
unchanged. Existing callers with explicit shorter deadlines retain their
deadline, and the scheduler's transport reserve remains a stricter cap. The
behavioral change is intentional for callers without a deadline: their
configured provider timeout is now total across retries rather than a fresh
timeout per retry. Role identities remain `LongCat-2.0`; no fallback decision
or non-model decision path was introduced.

## Parent Independent Verification

Fresh execution on d71bb97, using the complete mandated invocation:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_llm_gateway.py tests/mission/test_coverage_decision_budget.py tests/mission/test_contact_assessor.py tests/schedule/test_llm_client.py
```

Result: `216 passed in 20.28s`, exit0, no warnings. Parent session97061 finished. No credentials or provider calls used.

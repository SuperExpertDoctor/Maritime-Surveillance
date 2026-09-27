# Task 2 Wire Integration Report

Status: complete for owned implementation and targeted verification.

## Implementation

- `MissionScheduler.decide()` preserves its canonical payload for validation and
  diagnostics, while sending `encode_selection_payload(payload)` only through
  the decision-maker gateway boundary.
- `selection_interaction()` uses compact v2 JSON for its fallback user prompt;
  audited gateway messages still take precedence.
- Decision-maker payload serialization is compact; other roles retain the
  default spaced JSON serialization.
- `_FixtureGateway` decodes only decision-maker input before its selection
  builder. `CoverageFixtureGateway` inherits this entry point unchanged.
- Added transmitted-wire, role-serialization, legacy/v2 standard-fixture, and
  legacy/v2 coverage-fixture tests. V2 fixture rows include all three option
  columns.

## TDD Evidence

RED commands:

```text
pytest -q tests/mission/test_mission_scheduler.py::test_scheduler_sends_compact_wire_payload_once
pytest -q tests/mission/test_llm_gateway.py::test_request_json_serializes_only_decision_maker_payload_compactly
pytest -q tests/mission/test_evaluation_cli.py::test_fixture_gateway_expands_selection_until_validator_accepts tests/mission/test_coverage_scan_integration.py::test_coverage_fixture_prefers_shortest_search_transit_at_positive_floor
```

Result: expected failures for absent v2 wire version, non-compact
decision-maker JSON, fixture row decoding, and initially incomplete test rows.

GREEN commands:

```text
pytest -q tests/mission/test_mission_scheduler.py::test_scheduler_sends_compact_wire_payload_once
pytest -q tests/mission/test_llm_gateway.py::test_request_json_serializes_only_decision_maker_payload_compactly
pytest -q tests/mission/test_evaluation_cli.py::test_fixture_gateway_expands_selection_until_validator_accepts tests/mission/test_coverage_scan_integration.py::test_coverage_fixture_prefers_shortest_search_transit_at_positive_floor
```

Result: `7 passed`.

Additional affected gateway/fixture suite:

```text
pytest -q tests/mission/test_llm_gateway.py tests/mission/test_evaluation_cli.py tests/mission/test_coverage_scan_integration.py
```

Result: `130 passed in 22.04s`.

Final targeted verification:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_mission_scheduler.py::test_scheduler_sends_compact_wire_payload_once tests/mission/test_llm_gateway.py::test_request_json_serializes_only_decision_maker_payload_compactly tests/mission/test_llm_gateway.py::test_role_requests_do_not_share_conversation_messages tests/mission/test_evaluation_cli.py::test_fixture_gateway_expands_selection_until_validator_accepts tests/mission/test_coverage_scan_integration.py::test_coverage_fixture_prefers_shortest_search_transit_at_positive_floor
```

Result: `8 passed in 0.09s`.

## Files Changed

- `src/mission/mission_scheduler.py`
- `src/mission/llm_gateway.py`
- `scripts/evaluate_mixed_maritime.py`
- `tests/mission/test_mission_scheduler.py`
- `tests/mission/test_llm_gateway.py`
- `tests/mission/test_evaluation_cli.py`
- `tests/mission/test_coverage_scan_integration.py`

## Concerns

The combined scheduler/payload/decision-budget/legacy broad suite was started
once but was CPU-bound beyond the imposed 90-second wrapper; it was stopped at
root direction and was not rerun. Root owns the final broad affected-suite
run. No provider calls were made. The root-owned plan-document modification is
intentionally unstaged.

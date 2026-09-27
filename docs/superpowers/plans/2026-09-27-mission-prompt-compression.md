# Mission Prompt Compression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Obtain workspace consent before implementation; no live API calls are authorized by this plan alone.

**Goal:** Reduce decision-maker input redundancy without dropping mission facts or weakening validation, make its output budget configurable, and preserve the initiating cause of truncation-related timeouts.

**Architecture:** Keep `MissionSnapshot`, deterministic matching, and the canonical diagnostic payload unchanged. Build a detached, versioned model-facing payload immediately before the gateway request, with a compact relation table and no repeated system instructions. Decode this representation only at the offline fixture boundary; retain both initiating and terminal failure diagnostics.

**Tech Stack:** Existing Python, pytest, JSON serialization, YAML configuration, and OpenAI-compatible transport. No new dependency or provider.

## Status And Authorization

Execution update: the user authorized the isolated worktree and merging the
verified feature branch into `main`. The user subsequently requested real LLM
validation. The initial live batch is bounded to 10 logical task calls, with
physical retries separately counted. The larger paired experiment and five-hour
rerun below remain future acceptance steps, not completed work.

- Design approved by the user on 2026-09-27.
- Implementation evidence is recorded in `docs/validation/2026-09-27-mission-prompt-compression.md`; this plan alone is not evidence of mission success.
- Tasks 1-6 have been implemented and independently reviewed; the complete affected suite passed 370 tests. Final whole-branch review and local merge remain pending.
- The authorized isolated worktree is `.worktrees/mission-prompt-compression`, branch `fix/mission-prompt-compression`, based on `4be5e9a`.
- The bounded real-provider batch used 7 logical calls and 10 physical requests. Single-UAV dispatch succeeded; ten-UAV dispatch still failed. No five-hour rerun or push was performed.
- Live experiments beyond the initial bounded batch and a five-hour rerun require a separately confirmed call budget.

## Global Constraints

- Keep `LongCat-2.0`, thinking enabled, default decision-maker `max_tokens: 4096`, provider total timeout 120 seconds, planning deadline 60 seconds, and postprocessing reserve 1 second unchanged initially.
- Keep model output schema `mission-selection/v1` unchanged.
- Do not alter candidate-window selection, candidate ordering, task IDs, contact evidence, active ownership, UAV generations, coverage quotas, safety rules, or identity/classification thresholds.
- Do not round floats, merge UAVs, clip feasible relations, truncate observations, or introduce heuristic model replacements.
- Never mutate the canonical payload or `MissionSnapshot` while building the wire payload.
- Do not add prompt bodies, credentials, or private reasoning text to public HTTP/WebSocket telemetry or final reports.
- Keep all retries within the existing shared absolute deadline. Do not introduce background retries, automatic thinking-mode changes, or an unbounded timeout.
- Byte counts are UTF-8 text measurements, not provider token estimates. Provider `usage` is authoritative for actual token counts.
- Preserve unrelated user changes. Do not merge or commit without the agreed repository workflow.

## Evidence Baseline

The historical run is `outputs/simulation_20260927_160814.jsonl`. Its first three user-payload sizes are 39066, 39386, and 39678 bytes. Offline reconstruction with seed 42 reproduced those sizes. The system prompt is separately 7287 bytes, so the first request contains 46353 bytes of message content before chat-envelope overhead.

The first payload contains 15 visible candidates, 10 resources, one contact, and empty strategy memories/reviewer summary. It does not transmit all 7528 internal candidates. Major first-round fields are:

| Field | UTF-8 JSON bytes |
| --- | ---: |
| `snapshot.feasible_edges` | 15758 |
| `snapshot.candidates` | 6263 |
| `snapshot.resources` | 2381 |
| `snapshot.coverage_summary` | 2317 |
| `snapshot.coverage_constraint` | 2044 |
| `snapshot.contacts` | 1216 |

One full copy of the system rules is redundantly encoded in `instructions`. Removing that copy and compacting JSON produces approximately 36707 total message-text bytes for the first reconstructed request. This is a measured serialization calculation, not a provider latency or token-count result.

All four historical mission requests first reached `finish_reason=length` at 4096 completion tokens. Two then had only 9.61/7.47 seconds for a larger retry; two skipped retry with 1.45/3.95 seconds left. Preserve this distinction in new diagnostics.

## File Map

| File | Responsibility |
| --- | --- |
| New `src/mission/prompt_payload.py` | Pure model-payload encoder and legacy-compatible fixture decoder |
| `src/mission/mission_scheduler.py` | Keep canonical payload; pass encoded copy to gateway; serialize the actual sent payload in traces |
| `src/mission/llm_gateway.py` | Compact serialization, byte metrics, configurable decision-maker limit, initial-failure diagnostics |
| `src/mission/prompts/mission_scheduler.txt` | Single, reorganized rule set and relation-column explanation |
| `scripts/evaluate_mixed_maritime.py` | Decode wire payload at decision fixture entry, including inherited coverage fixture behavior |
| `scripts/persistent_coverage_scenarios.py` | Verify compatibility through its inherited gateway; avoid changing its selection algorithm |
| `main.py` | Include bounded numeric/summary diagnostic fields in the final report |
| `configs/llm_params.yaml` | Document configuration behavior; preserve defaults |
| New `tests/mission/test_prompt_payload.py` | Encoder/decoder fidelity and mutation isolation |
| Existing mission/gateway/evaluation/visibility tests | Boundary integration and behavioral regression |
| New `docs/validation/2026-09-27-mission-prompt-compression.md` | Actual measurements, tests, limitations, and subsequent live experiment results |

## Task 1: Versioned, Lossless Model Payload

**Interfaces:** `encode_selection_payload(payload: dict) -> dict` accepts the canonical scheduler payload. `decode_selection_payload(payload: dict) -> dict` returns a detached semantic payload for offline fixtures, accepting historical unversioned input. New format is `mission-prompt/v2`; response schema remains unchanged.

- [x] Add `tests/mission/test_prompt_payload.py` with the following primary regression. Add independent cases for empty options, two tasks sharing a UAV, exact float preservation, mismatched candidate/edge eligibility, malformed row width, unsupported format version, and untouched legacy payloads.

```python
from copy import deepcopy

from src.mission.prompt_payload import (
    decode_selection_payload,
    encode_selection_payload,
)


def test_wire_payload_preserves_facts_without_mutating_source():
    canonical = {
        "schema_version": "mission-selection/v1",
        "instructions": "rules",
        "snapshot": {
            "snapshot_id": "S1",
            "candidates": [{"task_id": "Q1", "feasible_uav_ids": ["U1"]}],
            "feasible_edges": [{"task_id": "Q1", "uav_options": [{
                "uav_id": "U1",
                "transit_time_min": 61.17751767083647,
                "total_range_cells": 39.02193997489758,
            }]}],
            "contacts": [{"contact_id": "C1", "samples": []}],
            "coverage_constraint": {"must_service_task_ids": ["Q1"]},
        },
    }
    before = deepcopy(canonical)
    wire = encode_selection_payload(canonical)
    assert wire["prompt_format_version"] == "mission-prompt/v2"
    assert "instructions" not in wire
    assert "feasible_uav_ids" not in wire["snapshot"]["candidates"][0]
    assert wire["snapshot"]["feasible_edges"][0]["uav_options"] == [
        ["U1", 61.17751767083647, 39.02193997489758]
    ]
    decoded = decode_selection_payload(wire)
    assert decoded["snapshot"] == canonical["snapshot"]
    wire["snapshot"]["contacts"][0]["samples"].append("changed")
    assert canonical == before
```

- [x] Run the new file and observe an import failure for the not-yet-created module. After module creation, rerun every behavior-specific regression rather than treating import failure as proof of all cases.
- [x] Implement `src/mission/prompt_payload.py` with these exact public interfaces and encoding rules. Use `deepcopy`; preserve all unknown snapshot fields. Only candidate eligibility duplicated exactly by the edge table may be omitted.

```python
from copy import deepcopy

PROMPT_FORMAT_VERSION = "mission-prompt/v2"
UAV_OPTION_COLUMNS = ("uav_id", "transit_time_min", "total_range_cells")


def encode_selection_payload(payload: dict) -> dict:
    wire = deepcopy(payload)
    wire.pop("instructions", None)
    wire["prompt_format_version"] = PROMPT_FORMAT_VERSION
    snapshot = wire["snapshot"]
    snapshot["uav_option_columns"] = list(UAV_OPTION_COLUMNS)
    eligibility = {}
    for edge in snapshot.get("feasible_edges", []):
        options = edge["uav_options"]
        if any(set(option) != set(UAV_OPTION_COLUMNS) for option in options):
            raise ValueError("unsupported UAV option fields")
        eligibility[edge["task_id"]] = sorted(option["uav_id"] for option in options)
        edge["uav_options"] = [
            [option[column] for column in UAV_OPTION_COLUMNS]
            for option in options
        ]
    for candidate in snapshot.get("candidates", []):
        ids = candidate.get("feasible_uav_ids")
        if ids is not None and ids == eligibility.get(candidate["task_id"]):
            del candidate["feasible_uav_ids"]
    return wire


def decode_selection_payload(payload: dict) -> dict:
    decoded = deepcopy(payload)
    version = decoded.pop("prompt_format_version", None)
    if version is None:
        return decoded
    if version != PROMPT_FORMAT_VERSION:
        raise ValueError("unsupported mission prompt format")
    snapshot = decoded["snapshot"]
    columns = snapshot.pop("uav_option_columns")
    if columns != list(UAV_OPTION_COLUMNS):
        raise ValueError("unsupported UAV option columns")
    eligibility = {}
    for edge in snapshot.get("feasible_edges", []):
        edge["uav_options"] = [
            dict(zip(columns, row, strict=True)) for row in edge["uav_options"]
        ]
        eligibility[edge["task_id"]] = sorted(
            option["uav_id"] for option in edge["uav_options"]
        )
    for candidate in snapshot.get("candidates", []):
        if candidate["task_id"] in eligibility:
            candidate.setdefault("feasible_uav_ids", eligibility[candidate["task_id"]])
    return decoded
```

The encoder accepts the canonical scheduler contract, where all visible candidates have eligibility lists and edges have exactly the three named option fields. A mismatching eligibility list is retained rather than silently discarded. The decoder does not restore removed prose, so equality assertions compare semantic snapshots, not `instructions`.

- [x] Run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider tests/mission/test_prompt_payload.py`. Expected: all new tests pass; malformed/unknown formats raise explicit errors.

## Task 2: Integrate The Wire Boundary And Preserve Fixtures

**Files:** `mission_scheduler.py`, `llm_gateway.py`, `scripts/evaluate_mixed_maritime.py`, `tests/mission/test_mission_scheduler.py`, `test_coverage_decision_budget.py`, `test_evaluation_cli.py`, `test_coverage_scan_integration.py`, and `test_llm_gateway.py`.

**Consumes:** Task 1 encoder/decoder. **Produces:** Actual transport messages contain the v2 payload; canonical diagnostic APIs and offline selectors retain their existing semantic structures.

- [x] Add an integration test using existing `_task`, `_resource`, `_edge`, `_snapshot`, and `_selection` helpers and `ScriptedTransport`. Exercise the real `LLMGateway` and scheduler rather than replacing the encoder with a mock.

```python
def test_scheduler_sends_compact_wire_payload_once():
    snapshot = _snapshot([_task("Q1")], [_resource("U1")], [_edge("Q1", "U1", 1.0)])
    transport = ScriptedTransport({
        "decision_maker": [json.dumps(_selection(snapshot, ["Q1"]))],
    })
    gateway = LLMGateway(transport=transport)
    scheduler = MissionScheduler(gateway=gateway)
    assert scheduler.decide(snapshot) is not None
    messages = transport.calls[0]["messages"]
    wire = json.loads(messages[1]["content"])
    assert wire["prompt_format_version"] == "mission-prompt/v2"
    assert "instructions" not in wire
    assert messages[0]["content"] == scheduler.system_prompt
    assert messages[1]["content"] == json.dumps(
        wire, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    )
    assert scheduler.last_selection_payload["instructions"] == scheduler.system_prompt
    assert scheduler.last_selection_payload["snapshot"]["feasible_edges"][0]["uav_options"][0]["uav_id"] == "U1"
    assert scheduler.selection_interaction()["user_prompt"] == messages[1]["content"]
```

- [x] Parameterize existing fixture selection tests with legacy and v2 inputs. In the v2 case, provide complete three-column options, call the encoder, and assert the same selected task IDs and validation outcome. Repeat for `CoverageFixtureGateway`'s near-versus-far task-order test.
- [x] Run those tests before integration; expect missing wire version or row-reading failures.
- [x] In `MissionScheduler.decide`, retain `payload` for visible task IDs and validation, and replace only `user_payload=payload` with `user_payload=encode_selection_payload(payload)` at the gateway call.
- [x] In `selection_interaction`, make the fallback user prompt represent the encoded payload. Actual audited gateway messages still take precedence. Guard the no-payload case with `{}` rather than invoking the encoder without a snapshot.

```python
wire_payload = (
    encode_selection_payload(self.last_selection_payload)
    if self.last_selection_payload is not None else {}
)
user_prompt = json.dumps(
    wire_payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
)
```

- [x] In the gateway's JSON user-content serialization, use `separators=(",", ":")` for `role == "decision_maker"` and retain default `(", ", ": ")` for other roles. Keep strict JSON handling intact.
- [x] In `_FixtureGateway.request_json`, decode only the decision-maker payload before calling `_build_decision_selection`. This inherited entry point also covers `CoverageFixtureGateway`, so its task-ranking algorithm need not change.

```python
if role == "decision_maker":
    snapshot = decode_selection_payload(user_payload).get("snapshot", {})
```

- [x] Run scheduler, payload, gateway, evaluation CLI, legacy scheduling, and coverage scan tests. Canonical `_prompt_payload()` tests should continue to see the old structure; update only tests explicitly examining transmitted messages.

## Task 3: Shorten Rules Without Changing Policy

**Files:** `src/mission/prompts/mission_scheduler.txt`, `tests/mission/test_legacy_search_scheduling.py`, `tests/mission/test_llm_gateway.py`, and the validation document.

**Produces:** One system rule set explaining v2 rows, while retaining the same hard constraints and response schema.

- [x] Add a prompt-format regression to the payload integration test: the system prompt must explain `uav_option_columns`, `simultaneous`, `required_new_search_count`, `pending_intent_reviews`, and `ordinary_search_admission_limit`. Run it and observe the missing column explanation before editing.
- [x] Replace the prompt with the following text. Keep the listed legacy phrases to avoid turning tests that check retained-work rules into weaker assertions.

```text
You select simultaneous tasks for mixed maritime surveillance. The server
validates every selection and deterministically assigns distinct UAVs.

OUTPUT
Return exactly one JSON object:
{"schema_version":"mission-selection/v1","snapshot_id":"supplied snapshot id",
"selected_task_ids":["supplied task IDs in priority order"],
"preempt_uav_ids":[],"defer_reason":null,"notes":""}
Prefer empty notes; if necessary use at most 80 Unicode characters. The hard
validator limit is 160 Unicode characters, including spaces and punctuation.
Do not include commentary, invented IDs, geometry, or sequential future work.

INPUT
The snapshot is authoritative for resources, generations, map version, active
tasks, contacts, intents, costs, and reviewer summary. Each feasible_edges entry
belongs to one task. Its uav_options rows follow uav_option_columns, in order:
uav_id, transit_time_min, total_range_cells. A candidate's eligible UAVs are those
rows unless an explicit feasible_uav_ids list is present. Do not invent edges.
Search candidates already contain legal rectangles; probe/track candidates
reference contacts. The server retains the complete legal graph and exact costs.

HARD CONSTRAINTS
Select only visible candidate IDs. Each selected task requires a distinct
eligible UAV; selected_task_ids are simultaneous, not a sequential queue.
Respect remaining range, generations, current task ownership, contact state,
intent status, feasibility, and explicit preemption policy.
pending_search_task_ids are retained work, not new model candidates;
do not select or recreate pending search regions. Deterministic reassignment
handles them. selected ordinary searches are additions only and must not
overlap retained/executing search regions or each other.
Preserve approved/executing tasks. Busy search UAVs require explicit legal
preemption. Never preempt return, refuel, safety, active probe, or valid track.
Ordinary search/investigation cannot preempt unless explicitly authorized.

COVERAGE AND INTENT RULES
When coverage_constraint exists, select at least required_new_search_count new
ordinary searches and all global must_service_task_ids, unless global
infeasible_reason is present. Investigation/direction_search are not ordinary
search. required_new_search_count is the residual addition count after active
searches and matchable pending work, not the fleet's total search count.
For every feasible zone_requirements entry, meet its required_search_count from
representative_task_ids and include its must_service_task_ids, even if the global
budget is infeasible. zone_infeasible records exclusions; do not invent work.
After preemption, active_search_count + matchable_pending_count + new ordinary
searches - preempted searches must be at least min(desired_search_count,
active_search_count + matchable_pending_count), including global infeasibility.
A zero addition requirement does not authorize removing existing coverage.
ordinary_search_admission_limit, when non-null, caps new ordinary-search
admissions while executable probe demand exists. Without that demand, full
ordinary-search utilization is legal. Preserve validated executing tasks.
Review each pending_intent_reviews revision against active_tasks owners and
remaining candidates. Preserve ownership and prioritize remaining focus scans.
Never select an executing owner again or duplicate a focus reservation. If no
new assignment is legal, acknowledge review with an empty selection and concise
notes or defer_reason. A deferral never overrides required feasible idle work.

PRIORITIES
1. Preserve safe executing assignments and boundaries; repartition only
   unreserved remaining domain when resources change.
2. Spread legal work across the domain using coverage_summary's per-zone unseen,
   overdue, and in-flight gaps. In-flight work is not a sensor observation and
   never makes cells fresh. Prefer supplied partition:* candidates; small
   search:* candidates are fallbacks for geometry, weather, or energy limits.
3. Fill otherwise idle feasible capacity, even with a zero coverage addition
   requirement, without unnecessary preemption. Coverage floors are not ceilings;
   all admission, zone, priority, and safety limits still apply.
4. Reduce switching, boundary churn, uncovered gaps, and redundant scans; then
   minimize transit + search + recovery cost using supplied edges. Do not claim
   global optimality. The server performs final distinct-UAV matching.

Strategy memories are advisory prioritization evidence only. They cannot change
sensors, identity thresholds, safety, resources, or feasibility; do not present
past observations as current evidence. Empty memories mean baseline scheduling.

REASONING
Soft thinking budget: aim for approximately 1024 tokens. This is a target, not a
hard cutoff. Finish necessary checks and emit one complete valid JSON object.
Keep any provider reasoning outside the final JSON; never emit think markup.
```

- [x] Audit the following rule mapping against existing behavioral tests: retained work, non-overlap, distinct UAVs, protected operations, residual/global coverage floor, zone obligations under global infeasibility, preemption floor, probe admission ceiling, revision-specific intent acknowledgement, idle-capacity utilization, partition preference, advisory memories, and notes limit.
- [x] Run legacy scheduling, mission scheduler, prompt-window, coverage policy, fleet partition, and intent candidate tests. Text-presence tests supplement behavioral tests; they do not prove semantic equivalence of model behavior.
- [x] Record before/after system bytes. Do not claim model quality equivalence until the separately authorized paired live experiment.

## Task 4: Configurable Decision-Maker Output Budget

**Files:** `src/mission/llm_gateway.py`, `src/mission/mission_scheduler.py`, `configs/llm_params.yaml`, `tests/mission/test_llm_gateway.py`, and `test_coverage_decision_budget.py`.

**Produces:** Decision-maker configuration accepts integer values 1 through 16384; default remains 4096. Other role limits remain unchanged. The existing output-retry ceiling remains 16384.

- [x] Use `_write_llm_config` and `ScriptedTransport` to add a test that changes only decision-maker `max_tokens` to 8192 and executes a scheduler decision. Assert the transport receives 8192, the same thinking mode, and the existing transport deadline/reserve.
- [x] Add boundary configuration cases: accept 1/4096/8192/16384; reject 0/-1/16385/True/1.5/"8192". Keep rejection tests for changing other role limits.
- [x] Run the tests before modification; expect the current exact-4096 validation to reject 8192.
- [x] Replace only the decision-maker branch of `_validate_required_bindings` with the following rule; retain the current exact-value rule for all other roles.

```python
tokens = binding["max_tokens"]
if role == "decision_maker":
    if type(tokens) is not int or not 1 <= tokens <= 16384:
        raise LLMConfigurationError(
            "decision_maker max_tokens must be an integer from 1 to 16384"
        )
elif tokens != expected_tokens:
    raise LLMConfigurationError(f"{role} max_tokens must be {expected_tokens}")
```

- [x] Remove `max_tokens=4096` from the mission scheduler gateway call and its obsolete hardcoded-allowance comment. The gateway already resolves an omitted override from the role binding. Preserve the probe's independent 32-token override.
- [x] Replace `test_decision_maker_override_is_explicitly_limited` with a test asserting the scheduler does not override role configuration, and retain an integration assertion that the default transport still receives 4096.
- [x] Document the decision-maker range and default in YAML comments without changing its value, thinking mode, temperature, or any timeout.
- [x] Run gateway, scheduler-budget, role-client, and runtime-configuration tests. Verify 8192 retries cap at 16384 and deadlines remain shared, not multiplied by retry count.

## Task 5: Diagnostic Evidence And Report Visibility

**Files:** `src/mission/llm_gateway.py`, `src/mission/mission_scheduler.py`, `src/schedule/task_allocator.py`, `main.py`, gateway tests, `tests/vis/test_decision_details.py`, and report/runtime-loop tests.

**Produces:** Canonical versus transmitted size is explicit; initiating truncation is not hidden by terminal timeout. Historical report fields retain their meaning.

- [x] Add tests using the existing fake-clock truncation fixture, asserting final `timeout` alongside `initial_failure_category == "output_truncated"`. Also cover truncation followed by success, initial transport timeout, and an initial validation error.
- [x] Add a real-gateway/scripted-transport assertion for exact byte counts:

```python
messages = transport.calls[0]["messages"]
call = gateway.call_log[-1]
assert call["system_prompt_bytes"] == len(messages[0]["content"].encode("utf-8"))
assert call["user_prompt_bytes"] == len(messages[1]["content"].encode("utf-8"))
assert call["input_text_bytes"] == sum(
    len(message["content"].encode("utf-8")) for message in messages
)
assert call["attempts"][0]["input_text_bytes"] == call["input_text_bytes"]
```

- [x] Add report/public-detail tests asserting these numeric fields survive export, while `messages`, `system_prompt`, `user_prompt`, credentials, and partial truncated output remain absent.
- [x] Run the new tests and observe absent numeric/root-cause fields.
- [x] At gateway call construction record `system_prompt_bytes`, `user_prompt_bytes`, their sum as `input_text_bytes`, `prompt_format_version`, and `configured_max_tokens`. At each attempt record the sum of current message-content bytes, including any compact correction hint, plus the existing `max_tokens` and timeout fields.

```python
call.update({
    "system_prompt_bytes": len(system_prompt.encode("utf-8")),
    "user_prompt_bytes": len(user_content.encode("utf-8")),
    "input_text_bytes": sum(len(m["content"].encode("utf-8")) for m in messages),
    "prompt_format_version": (user_payload or {}).get("prompt_format_version"),
    "configured_max_tokens": binding["max_tokens"],
})
```

- [x] In every failure path, record the first failure with `call.setdefault("initial_failure_category", category)` before later budget checks can replace the terminal category. In particular, do this immediately inside `except LLMOutputTruncated` with `"output_truncated"`; use the existing typed/status classification for transport errors and `"validation"` for invalid responses. Never derive transport type from arbitrary error-message text.
- [x] Keep `failure_category` unchanged as the final outcome category. Successful correction may therefore have `success=True`, terminal category null, and a non-null initial failure category.
- [x] Preserve existing scheduler `prompt_bytes` as canonical user-payload bytes for compatibility. Add explicitly named wire/system byte fields; forward these through the allocator timing/interaction dictionary instead of silently redefining historical metrics.
- [x] Extend `main.py`'s report allowlist with the new scalar diagnostic fields and existing bounded attempt records. Before export, remove each attempt's `messages`, `raw_output`, `response`, and `provider_channels`; keep only attempt number, token limit, byte count, timeout, elapsed time, finish reason, numeric usage, and redacted errors. Keep call-level private prompt/reasoning content excluded.
- [x] Run gateway, public-detail/visibility, runtime-loop, and report tests. Confirm a truncation-then-budget-exhaustion report clearly contains both causes and does not leak request bodies.

## Task 6: Offline Measurement And Regression Gate

**Files:** New validation document; new or existing tests in `test_coverage_decision_budget.py` and `test_prompt_payload.py`.

- [x] Add an offline initial-episode size test. Inject a gateway that records requests and returns `ModelResult("offline", False, None, ("decision_deadline_exceeded",), "timeout")`; use `SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=gateway)` and step three times. Do not construct a real transport.
- [x] For each request assert `instructions` is absent, every encoded edge decodes to the canonical values, candidate IDs/order are unchanged, and the original snapshot/constraints remain unchanged. Measure compact user text plus the actual system text. Assert the historical initial scenario fits `30 * 1024` bytes; do not impose this threshold on every valid future large mission.
- [x] Measure first-round canonical bytes, system bytes, transmitted bytes, and field contributions. Record that local byte measurements do not reproduce the provider tokenizer or prove latency improvement.
- [x] Run the following affected offline suite without credentials or network calls:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider -o 'markers=timeout: legacy timeout marker' tests/mission/test_prompt_payload.py tests/mission/test_llm_gateway.py tests/mission/test_mission_scheduler.py tests/mission/test_coverage_decision_budget.py tests/mission/test_coverage_model_failure.py tests/mission/test_coverage_prompt_window.py tests/mission/test_coverage_policy.py tests/mission/test_fleet_partition.py tests/mission/test_intent_candidates.py tests/mission/test_legacy_search_scheduling.py tests/mission/test_coverage_scan_integration.py tests/mission/test_evaluation_cli.py tests/mission/test_visibility.py tests/vis/test_decision_details.py tests/schedule/test_llm_client.py tests/test_live_runtime_loop.py tests/test_runtime_configuration.py
```

- [x] Review the diff for accidental changes to deterministic matching, safety, candidate generation, classification criteria, role defaults, or timeout configuration. Review tests for weakened assertions rather than legitimate boundary-format updates.
- [x] Record actual counts, elapsed time, failures, byte measurements, and residual limitations in `docs/validation/2026-09-27-mission-prompt-compression.md`. No claim of effective real-model reconnaissance is permitted from offline results alone.

## Task 7: Separately Authorized Live Acceptance

The full paired and five-hour acceptance gates below remain future work. The
subsequently authorized bounded live batch is recorded in the validation
document; it does not satisfy the full acceptance criteria below.

- [ ] Obtain a budget for 30 logical decisions: five representative snapshots, two prompt variants, three repetitions each. Count and report physical retry requests separately; configured retries can make physical request count larger than 30.
- [ ] Use initial dispatch, newly observed contact, edited focus intent, constrained resources, and retained/executing work snapshots. Use the exact same snapshot within each pair. Start with the original and compressed prompts at 4096 tokens, thinking enabled, and 60 seconds; alternate order to reduce time-of-day confounding.
- [ ] Collect actual provider prompt/completion tokens, initial and final failure categories, per-attempt limits and duration, valid assignment outcome, and coverage/intent/legal matching checks. Do not demand identical model-selected IDs across stochastic runs; demand legality and compare mission-quality measures.
- [ ] If compressed 4096 still truncates, obtain an additional budget for testing 8192 with the same deadline. If that becomes timeout-limited, evaluate a longer deadline separately, including UI-command responsiveness. Do not silently change defaults based on one successful response.
- [ ] Run a fresh short episode through dispatch, physical flight, detection, evidence acquisition, and assessment. Check real emitted events, sensor evidence, applied assignments, and evaluator results, not fabricated reports or fixture responses.
- [ ] Only after the short episode passes, schedule a new five-hour acceptance with explicit API budget and a separate stall-monitoring policy. Existing `paused_model` is not active reconnaissance time. A safe initial monitoring policy is to stop unattended acceptance on that state and report the reason, rather than auto-resume endlessly.

## Delivery And Rollback

- Implement tasks 1-6 in individually reviewable stages: encoding, integration, prompt rules, output configuration, diagnostics, and evidence.
- The user authorized this worktree and local merge into `main`; merge only after the affected offline regression and final code-review gates pass. Real ten-UAV acceptance remains a separately reported limitation.
- Leave defaults at 4096/60 seconds until live evidence supports a separately approved change.
- If the compact format causes a regression, revert the encoding integration and prompt-format explanation together; do not run row data against a legacy-only system prompt. Keep the independent diagnostic improvements if their tests pass.
- Do not rewrite old logs or mutate the original five-hour episode to make its outcome look successful.

## Plan Self-Review

- [x] Initial and final failures are distinguished without exposing reasoning or credentials.
- [x] Both hardcoded-token restrictions are addressed; other roles and probe overrides remain unchanged.
- [x] Model-facing schema is separate from canonical data and response schema.
- [x] Offline fixture compatibility includes the inherited persistent-coverage selector.
- [x] Byte measurements include both messages and explicitly avoid claims about token equivalence.
- [x] No lossy rounding, candidate clipping, altered safety thresholds, or unauthorized live calls are included.
- [x] Workspace consent and live-budget consent are explicit implementation gates.

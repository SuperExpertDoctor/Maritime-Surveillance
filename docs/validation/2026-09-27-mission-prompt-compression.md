# Mission Prompt Compression Validation

## Scope

The user authorized implementation, local merge into `main`, and real LLM
validation. The initial live batch is capped at 10 logical calls; physical
retries are counted separately. No new five-hour run or statistically powered
paired evaluation is included. Offline fixtures are not live-model evidence.

The default remains LongCat-2.0 with thinking enabled, 4096 output tokens,
a 60-second planning deadline, and a 1-second postprocessing reserve.

## Live Evidence

The runs below use the real provider and a seed-42 `SimulationEngine` first
step, without a successful-response fixture or heuristic model replacement.

| Run | Input text bytes | First output limit | Outcome |
| --- | ---: | ---: | --- |
| Original `4be5e9a` | 46,353 | 4,096 | First request timed out after 59.95s; no usage returned |
| Compressed prompt | 25,745 | 4,096 | First response truncated after 44.93s; 8,192-token retry timed out after 14.33s |
| Compressed, temporary 8,192 config | 25,745 | 8,192 | First request timed out at approximately 60s; no usage returned |
| Compressed, isolated 8,192 / 120s | 25,745 | 8,192 | First response truncated after 80.27s; 16,384-token retry timed out after 39.00s |
| Final code `bdf26f4`, default | 25,745 | 4,096 | Truncated after 45.08s; retry had 13.91s and timed out after 14.19s |

The compressed first request returned authoritative provider usage of 12,900
prompt tokens and 4,096 completion tokens, with `finish_reason=length`.
The retry had only 14.06s of transport budget. These runs left all ten UAVs idle
and invalidated the episode with `decision_maker_failed`. Raising the first
attempt to 8,192 in an isolated configuration also left all ten UAVs idle under
the same deadline. This experiment did not modify the repository default.
The separate 120s diagnostic returned 12,900 prompt tokens and 8,192 completion
tokens on its first truncated response. Only 38.72s remained for retry. It also
produced no assignment. This was not a UI responsiveness acceptance test, and
does not justify changing production defaults. No partial response or private
reasoning was retained, so the evidence does not determine which output channel
consumed the completion allowance.

The final-code run records `initial_failure_category=output_truncated` together
with `failure_category=timeout`, `configured_max_tokens=4096`, and exact initial
and retry input bytes (25,745 / 25,927). It confirms the new diagnostic chain
on a real provider response, not only a fixture. The first response again used
12,900 prompt tokens and 4,096 completion tokens.

The compressed run began at `3bb8ad3` with the subsequently committed Task 3
prompt in the working tree. The artifact records its exact prompt SHA-256 and
modified-file list. This avoids attributing an uncommitted prompt to the parent
commit. Later diagnostic fields were not yet implemented, so those keys are
null in this artifact; per-attempt provider metadata remains available.

Input text decreased by 44.46%, but this single observation does not establish
latency improvement or model-quality equivalence. Byte counts are not token
estimates. The original live baseline timed out without usage, so it does not
support a paired provider-token reduction claim.

Local artifacts in the primary checkout:

- `outputs/prompt_compression_20260927/baseline.json`
- `outputs/prompt_compression_20260927/compressed_default_step1.json`
- `outputs/prompt_compression_20260927/compressed_8192_step1.json`
- `outputs/prompt_compression_20260927/compressed_8192_120s_step1.json`
- `outputs/prompt_compression_20260927/compressed_single_uav_smoke.json`
- `outputs/prompt_compression_20260927/compressed_final_default_step1.json`
- `outputs/prompt_compression_20260927/live_check.py`

These reports contain no credentials, request bodies, or provider reasoning.
The completed batch consumed 7 logical calls and 10 physical requests: five
failed ten-UAV decisions, one successful single-UAV decision, and one successful
reviewer call. No additional calls are planned in this batch.

### Constrained Single-UAV Smoke Test

A separate seed-42 scenario changed only the UAV count to one, retaining the
normal 4096-token / 60s decision configuration and enabled thinking. Its first
real decision completed in 10.42s, with 3,036 prompt tokens and 756 completion
tokens. The selection passed validation and was applied; status progressed
from idle to transit to tracking. The real reviewer call at simulation minute
15 also succeeded using that role's unchanged thinking-disabled configuration.
The run consumed two logical calls and two physical requests in 30.77s wall
time. This is accelerated simulation, not a 15-minute wall-clock test.

The evaluator remained valid, but detected ships, observed vessels, terminal
classifications, and SAR coverage all remained zero. A tracking status is not
proof of sensor evidence or successful classification. This smaller scenario
does not establish ten-UAV mission success.

## Regression Status

### Offline Measurement And Regression Gate

An injected recording gateway ran `SimulationEngine(ConfigLoader.load(),
seed=42, llm_gateway=gateway)` for three steps. It returned only
`ModelResult("offline", False, None, ("decision_deadline_exceeded",),
"timeout")`; no transport, provider client, or credentials were used by this
test. All three initial-scenario decision requests decoded from the compact wire
format to the exact canonical snapshot, retained candidate IDs and order, and
omitted `instructions` from the user payload. A spy around the actual encoder
also verified each encoder input was unchanged, so the test observes canonical
payload preservation at the real encoding boundary rather than inferring it
after decoding.

First-request UTF-8 measurements use compact JSON serialization
(`ensure_ascii=False`, `allow_nan=False`, separators `(',', ':')`):

| Measurement | Bytes |
| --- | ---: |
| Canonical payload (compact comparison only) | 34,476 |
| System text | 4,940 |
| Compact user-wire text | 20,805 |
| Actual transmitted system + user text | 25,745 |
| Historical initial-scenario limit | 30,720 |

The three recorded initial-scenario requests each remained below the 30 KiB
limit. This is deliberately a historical seed-42 regression threshold, not a
general limit on future large missions.

The canonical comparison row is not the scheduler's historical `prompt_bytes`
metric, which serializes canonical payloads with default JSON spacing. It is
included only to compare the canonical representation with the compact wire
representation on the same UTF-8 serialization basis.

The canonical top-level value contributions were 5,040 bytes for
`instructions`, 22 for `schema_version`, and 29,367 for `snapshot` (the latter
includes JSON structure). The largest first-round snapshot value contributions
were `feasible_edges` (14,814), `candidates` (5,727), `coverage_summary`
(2,096), `resources` (2,192), `coverage_constraint` (1,934), `contacts`
(1,138), `prompt_sources` (452), `uav_generations` (122), and
`strategy_memory_context` (110) bytes. The remaining snapshot values together
are small metadata and empty collections.

These local byte counts neither reproduce the provider tokenizer nor establish
latency improvement or real-model reconnaissance quality. Provider `usage`
remains authoritative for token counts. Credential-origin inspection confirmed
that the inherited environment had no API key while the configured local key
matched the supplied credential fingerprint; no key, fingerprint, prompt body,
or provider reasoning is recorded here.

Final affected-suite verification passed: **370 passed in 332.67s**. Root ran
the Task 6 suite listed in the implementation plan, plus
`tests/mission/test_zone_rolling_acceptance.py::test_prompt_preserves_geometry_safety_and_coverage_floor_policy`,
with bytecode, external pytest plugin auto-loading, and pytest cache disabled.
No source or test files changed during the run. The production/test revision was
`fd90eda`; subsequent documentation-only updates do not change tested code.
The JUnit artifact is `outputs/prompt_compression_20260927/offline-tests.xml`
in the primary checkout. This is an affected-suite result, not a claim that
every test in the repository was run successfully.
The pre-change focused baseline was 155 passed in 32.08s.

One existing fleet test also failed on unmodified `main`: it expected ten
ordinary searches while its fixture introduced executable probe demand. The
test now explicitly configures zero ships and disables opponent spawning,
asserts no initial contacts, and retains all original ten-UAV/ten-region and
large-area assertions. Shared fixture defaults and production policy are
unchanged. Updated prompt assertions check equivalent rules in the shortened
text and the actual provider-facing system message.
An existing visibility test also expected thinking disabled despite the
unchanged decision-maker configuration being enabled. Root reproduced that
failure on unmodified main; its assertion now matches the actual preserved
configuration.

Final read-only whole-branch review of `4be5e9a..490ffa4` approved the scoped
implementation with no Critical, Important, or Minor findings. The reviewer
independently checked the JUnit result and the remaining live-acceptance limits.
Root also verified that all saved live summaries total 7 logical calls / 10
physical requests, contain no private report fields, and that neither changed
tracked files nor live summaries contain the configured credential.

The local post-merge check covers payload, gateway, scheduler-budget, public
detail, and runtime/report regressions. Its evidence artifact target is
`outputs/prompt_compression_20260927/merged-tests.xml` in the primary checkout.

## Limits

Prompt compression demonstrated valid real-model dispatch only in the separate
single-UAV scenario. Ten-UAV dispatch, useful physical reconnaissance, detection,
and identity assessment remain unverified. A new five-hour acceptance run is not
justified by the evidence above.

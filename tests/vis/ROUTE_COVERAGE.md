# Visualization integration audit (2026-09-22)

All tests below use offline/labeled fixtures. They do not establish real-provider mission success or replace the parent's production 20-minute run. No simulation.py/main.py changes were made by this task; other agents own those files. No commits.

## Route coverage matrix

| Route | Consumer / contract | Executed coverage |
|---|---|---|
| GET `/` and `/assets/*` | Built React application | `drawer-integration.spec.js`: real FastAPI static assets, no route mocks |
| WS `/ws/live` | Current frame, runtime status, regions, contacts, config snapshot, last 10 model calls | `test_server_runtime.py`: initial frame, broadcast, disconnect/timeout; `test_decision_details.py`: provider channel and vessel state; browser integration renders all five tabs |
| GET `/api/replay/list` | Replay selector | `test_decision_details.py`: empty list; drawer integration: actual fixture files |
| GET `/api/replay` | Paged historical frames | `test_server_runtime.py`: growing/indexed/truncated/canonical frames; decision tests: path/query validation and safe historical decision output; drawer integration: historical parameters and absence of future events |
| GET `/api/config` | Live fallback; newly recorded frames embed same config | runtime config contract test; initial WS/config comparison; browser Params rendering; historical missing config never replaced by current config |
| GET `/api/model-calls` | Drawer polling while simulator waits; current episode only; last 100 calls maximum | decision tests: live HTTP/WS equivalence, stale episode 409, invalid limits 422, SDK external-channel provenance/redaction, prompt-size exclusion; browser selects and renders calls |
| GET `/api/intents` | Published intent read model | `test_intent_api.py`: create/apply/read; decision CRUD integration |
| POST `/api/intents` | Queued creation | intent API suite: queued→applied, idempotence, conflict, malformed/old episode, queue full, replay/finished rejection; browser fixture create/status flow |
| PATCH `/api/intents/{intent_id}` | Revision-checked update | intent API suite: applied update and competing revision rejected |
| DELETE `/api/intents/{intent_id}` | Revision-checked cancellation | decision CRUD integration: accepted→applied cancellation |
| GET `/api/intent-commands/{command_id}` | Intent and runtime command polling | intent API suite: applied/rejected/reset; decision CRUD: 404; mixed browser runtime queue→applied; transient polling failure now retries |
| POST `/api/runtime/retry` | Retry paused model | intent API suite: queued/applied and not-paused rejection; mixed browser runtime status |
| POST `/api/runtime/abort` | Finish episode | intent API suite: queued/applied and subsequent writes blocked |
| POST `/api/vessels` | Queued vessel placement | decision CRUD integration: create/apply/inventory; runtime tests conflict/validation; browser click/drop placement |
| DELETE `/api/vessels/{vessel_id}` | Revision-checked deletion | decision CRUD integration: apply/delete/inventory; browser placement→selection→delete |
| PATCH `/api/vessels/{vessel_id}/ais` | Strict boolean + revision | decision CRUD integration: apply + initial WS authoritative state; runtime API strict bool, replay/finished rejection; browser revision acknowledgement and conflict |
| GET `/api/vessel-commands/{command_id}` | Authoritative queued/applied/rejected status | decision CRUD integration + 404; vessel browser suite: lost POST, retry after network error, delayed completion, episode/replay changes |
| GET `/api/scenario/vessels` | Authoritative operator inventory | decision CRUD integration: before/create/delete counts |
| GET `/api/export/capabilities` | Export availability | runtime API encoder available/unavailable tests; browser capability fixture |
| POST `/api/export/mp4` | Transcode/download | runtime API tests: encoder subprocess fixture + download, streaming size rejection; file-switch guard prevents old export from restoring another replay's position |

## Detail semantics and payload size

- Model telemetry excludes system/user prompts, attempt messages, and duplicate raw-attempt arrays. It retains decision output, notes, IDs, model/provider, time, thinking mode, success/failure, validation errors, attempt budgets and explicitly returned external-provider channels. Frame history is bounded to 10 calls; on-demand live HTTP to 100.
- Explicit SDK `choices[0].message.reasoning_content` / `thinking` becomes `provider_channels` with `external_api_response` provenance and source path. An explicit `reasoning_summary` is a separate public-summary channel. Secrets are redacted in both answer content and channels by the gateway. Prompt instructions and arbitrary answer fields are not treated as provider reasoning channels. Thinking configuration remains unchanged (disabled).
- Notes, external-provider reasoning and public provider summaries have separate UI sections. Absent data is `not provided`. Historical call selection is labeled separately from the latest active call.
- Timeline expands actual event data; replay hides future loaded events. WS bursts preserve unpublished events and reset matrix caches across episodes. UI context changes clear old decisions, events and command state.
- Regions show published status and completion basis; absent values are not invented zeroes. AIS retains latest report/time and full sample count even when live sample arrays are truncated. Empty current contacts do not fall back to hidden ship truth.
- New frames record the public config endpoint projection. Old replays without config explicitly say historical parameters were not provided.

## Executed checks

- Broad Python integration/regression run: **181 passed** (before final lean-payload test and CRUD test were added).
- After lean-payload changes: **102 passed** (`tests/vis/test_decision_details.py`, gateway, frame-publisher suites).
- Final decision/API suite: **6 tests** (result recorded in task final response).
- `npm run build`: passed on current frontend source.
- `npx playwright test --config playwright.drawer.config.js`: **1 passed**, real built frontend + FastAPI/WS/HTTP with labeled fixture provider.
- `npx playwright test --config playwright.interactions.config.js`: **22 passed**, explicitly mocked browser edge cases including reset/burst/stale command/replay failures.

## Parent production run

From `src/vis/frontend`:

```bash
PLAYWRIGHT_BASE_URL=http://127.0.0.1:8766 npx playwright test --config playwright.production.config.js
```

This test starts no server, makes no mutations/provider calls, and mocks no routes. It subscribes to actual WS telemetry, reads config and model-call HTTP, visits five tabs and captures screenshots plus `production-observations` JSON. It permits legitimately absent model calls/regions and reports counts rather than fabricating success. The parent owns the 20-minute real `main.py` run and live CRUD checks.

Selectors: drawer toggle button `切换任务详情面板`; region `任务详情`; tabs `时间线`, `区域`, `模型日志`, `参数`, `AIS`; call select label `Model call`; context `data-testid="drawer-context"`.

Provider schema references: https://longcat.ai/platform/docs/zh/api/chat and https://github.com/openai/openai-python/blob/main/README.md (extra SDK response properties). Verified installed SDK retains `reasoning_content` in `model_extra`.

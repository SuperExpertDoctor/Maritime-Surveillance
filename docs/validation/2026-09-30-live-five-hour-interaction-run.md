# Five-Hour Live Interaction Acceptance Run

Date: 2026-09-30. Branch: `main`. Seed: `20260930`.
Commits: `cea83e2` (parameterize acceptance window via `--hours`),
`2df1d0c` (placement legality fix).
Requested window: five real wall-clock hours of `main.py` with hourly
5x5 operator focus areas and three blue-fleet intervention rounds at
1.5/3/4.5 hours.

## Verdict

**PASS.** `outputs/live-interaction-20260929T180124999Z-20260930-a0368e33/audit.json`
reports `passed: true` with zero failures. The audit was produced by
`auditAcceptance` over the identical evidence inputs the runner uses
(operator ledger + authoritative frames + main report); the runner
process itself was reaped by the host tool shell after `main.py`
completed but before its audit block, so the audit was executed offline
and the terminal `run_finished` record notes that.

## Run Identity

- Command: `node scripts/run_live_interaction_acceptance.mjs --seed 20260930 --hours 5`
- Child: `python main.py --steps 1000 --step-delay 60 --wall-seconds 18720 --llm-probe-timeout 120 --port 38737 --memory-root .../strategy_memory --run-report-dir .../main-report`
- Episode: `episode-e949d3867d8049f0aff791db9ccc6061`
- Wall runtime: **18,765.38 s** (target 18,720 s = 5 h window + 12 min observation tail; required >= 18,000 s)
- Simulation steps: 262 (mean ~71.6 s/step; synchronous model work extends steps beyond the 60 s floor)
- Server-ready anchor: 2026-09-29T18:01:25Z (02:01:25 +0800); all eight scheduled events dispatched with **<= 1 ms** drift.

## Scheduled Events (all confirmed in authoritative frames)

| Event | Scheduled | Result |
| --- | --- | --- |
| focus I0001 | +1 h | intent applied, bbox `[7,0,12,5]` (25 cells) |
| fleet round 1 | +1.5 h | del `scenario-vessel-1` (I) / `Ship-1` (II); create `scenario-vessel-4` @ `[6.5,11.5]`, `scenario-vessel-5` @ `[7.5,21.5]`; AIS off on `scenario-vessel-2`, `scenario-vessel-3` |
| focus I0002 | +2 h | intent applied, bbox `[13,25,18,30]` |
| focus I0003 | +3 h | intent applied, bbox `[5,12,10,17]` |
| fleet round 2 | +3 h | del+create I @ `[5.5,27.5]` (`scenario-vessel-6`), del+create II @ `[15.5,8.5]` (`scenario-vessel-7`); AIS `scenario-vessel-7` off, `scenario-vessel-3` on |
| focus I0004 | +4 h | intent applied, bbox `[3,7,8,12]` |
| fleet round 3 | +4.5 h | del `scenario-vessel-6`/`scenario-vessel-5`; create `scenario-vessel-8` @ `[15.5,8.5]`, `scenario-vessel-9` @ `[17.5,11.5]`; AIS `scenario-vessel-7` on, `scenario-vessel-9` off |
| focus I0005 | +5 h | intent applied, bbox `[12,14,17,19]` |

Every operation carries queued->applied receipts plus frame-capture
confirmation (capture id/order recorded per event in `operator.jsonl`).
Six replacements and six AIS state changes confirmed; all creates passed
`invalid_position`/`vessel_spacing_conflict` validation on first attempt.

## Red-Side Response Evidence (final authoritative frame)

| Intent | Assigned task | Final coverage |
| --- | --- | --- |
| I0001 | `search:2:0:11:5` | 0.00 |
| I0002 | `search:12:21:18:29` | 0.80 |
| I0003 | `search:2:5:6:13` | 0.12 |
| I0004 | `search:2:5:6:13` | 0.48 |
| I0005 | `search:14:10:20:18` | 0.44 |

`audit.metrics`: `focusAreasConfirmed 5`, `focusAreasWithRedTasks 5`,
`eligibleFocusCells 125`, `confirmedFleetRounds 3`, `confirmedAisChanges 6`,
`maxDispatchDriftMs 1`. I0003/I0004 share a task because their boxes
overlap the same search footprint; that is the scheduler's own
deconfliction, not duplicated operator input.

## Main-Process Summary (`main-report/report.json`)

`heavy_triggers 221`, `llm_success_rate 0.217`, `coverage_pct 41.1`,
`region_changes 6`, `ship_count 5`, `detected_ships 0`,
`opponent_population_paused_by_manual_edit true` (expected: manual
fleet edits pause automatic releases by design). `runtime_status`
remained `running` for all 289 captured frames; no model-pause operator
intervention was required. The 10 `model_calls` retained in the final
frame all finished `stop`; the low aggregate success rate reflects
attempts that exhausted the configured provider budget earlier in the
episode, after which deterministic planning continued to assign tasks
(the red-side task evidence above is unaffected).

## Mid-Run Defect and Repair

The first five-hour attempt (`...160115445Z-...-5d7de212`, same seed)
exposed a harness defect: `choosePlacementCell` drew candidates from
`search_domain.searchable_cells`, which covers the whole 30x30 grid,
so replacements could be placed on the mainland (`mainland_width_cells
5`), islands, or storm cells and were rejected `invalid_position`.
That run was terminated at ~1.6 h with fleet round 1 partial; its
artifacts are preserved.

The repair mirrors `_create_scenario_vessel` legality in the picker:
exclude mainland columns, island footprints, storm cells (safety margin
plus drift buffer), and positions within 1.1 cells of live vessels;
committed as `2df1d0c` with unit coverage. The subsequent run above
placed all six replacements legally on first attempt.

## Limitations

- I0004's box `[3,7,8,12]` overlaps the mainland columns; intent
  validation accepts any in-bounds box (the intent mask spans the
  mission area) and a task was still assigned.
- `detected_ships 0`: no blue vessel was classified within the window.
- The runner tool shell disappeared before writing `run_finished`;
  the audit block was re-executed offline against the persisted ledger
  and frames, and this substitution is recorded in the ledger.

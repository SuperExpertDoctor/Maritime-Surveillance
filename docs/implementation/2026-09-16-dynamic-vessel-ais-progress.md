# Dynamic Vessel AIS Implementation Progress

## Baseline

- Implementation branch: `feature/dynamic-vessel-ais-information`
- Base commit: `7f87a62cadbf84c2552ae28076efcdfbed531f35`
- Original worktree user changes preserved outside this linked worktree:
  - `tests/mission/test_mission_scheduler.py`
  - `tests/mission/test_vessel_commands.py`
  - `docs/2026-09-15-maritime-requirements.md`
  - `docs/superpowers/plans/2026-09-14-mixed-maritime-llm-implementation-plan.md`
  - `docs/superpowers/plans/2026-09-15-maritime-requirements-alignment-implementation-plan.md`
  - `docs/superpowers/plans/2026-09-16-dynamic-vessel-ais-information-implementation-plan.md`
  - `docs/superpowers/specs/2026-09-14-mixed-maritime-llm-design.md`
  - `docs/superpowers/specs/2026-09-15-maritime-requirements-alignment-design.md`
- `python -m pytest --collect-only -q`: 1207 tests collected.
- Initial frontend build: blocked because linked worktree dependencies were absent (`vite: not found`).
- `npm --prefix src/vis/frontend install`: completed; npm reported one high-severity audit finding, not changed by this implementation.

## Task Ledger

| Task | Commit | Tests | Status |
| --- | --- | --- | --- |
| T1 | `9aa425f` | `python -m pytest tests/mission/test_vessel_compat.py tests/mission/test_config.py tests/mission/test_contracts.py tests/test_runtime_configuration.py -q` -> 54 passed, 0 failed, 0 skipped | complete |
| T2 | `05cfb07` | `python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py tests/env/test_vessel_activity.py tests/env/test_emitter.py tests/mission/test_visibility.py -q` -> 38 passed, 0 failed, 0 skipped | complete |
| T3 | `59a6913` | `python -m pytest tests/mission/test_vessel_commands.py tests/env/test_server_runtime.py -q` -> 15 passed, 0 failed, 0 skipped | complete; `src/env/simulation.py` included because the specified engine property is required |
| T4 | `ba82540` | `LONGCAT_API_KEY=t04-offline python -m pytest tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_handoff.py tests/env/test_vessel_boundary.py -q` -> 36 passed, 0 failed, 0 skipped; boundary command -> 50 passed, 0 failed, 0 skipped | complete; exact path differs from the plan because `test_simulation_integration.py` is the existing integration file and the lifecycle test was added under `tests/mission/` |
| T5 | `b4f1e9a` | `LONGCAT_API_KEY=t05-offline python -m pytest tests/mission/test_surveillance_stage.py tests/mission/test_probe_session.py tests/env/test_contact_lifecycle.py -q` -> 115 passed, 0 failed, 0 skipped | complete; exact path differs from the plan because the contact lifecycle test lives under `tests/env/` and the probe policy test remains under `tests/mission/` |
| T6 | `c26b9f4` | `python -m pytest tests/mission/test_red_dynamic_population.py -q` -> 3 passed; `python -m pytest tests/mission/test_red_commander.py -q` -> 167 passed; `python -m pytest tests/mission/test_ship_navigation.py -q` -> 69 passed; `LONGCAT_API_KEY=t06-offline python -m pytest tests/mission/test_dynamic_vessel_lifecycle.py -q -k 'not runtime_vessel_motion_stays_in_bounds_across_seeds'` -> 5 passed, 50 deselected; boundary test -> 50 passed | complete; dynamic Red plan reuse is keyed by the active type-II surveillance signature |
| T7 | `0262407` | `python -m pytest tests/mission/test_information_update.py tests/mission/test_evidence_store.py tests/schedule/test_state_manager.py -q --maxfail=12` -> 29 passed; `LONGCAT_API_KEY=t07-offline python -m pytest tests/schedule/test_candidate_extractor.py tests/env/test_simulation_integration.py -q --maxfail=12` -> 42 passed | complete; InformationUpdatePolicy is the only information state source |
| T8 | `f2aa5f0` | `python -m pytest tests/mission/test_information_loop.py tests/schedule/test_trigger_manager.py tests/mission/test_maritime_acceptance.py tests/mission/test_simulation_flow.py -q --maxfail=12` -> 27 passed; cross-module regression -> 139 passed; `python -m compileall -q src tests` passed | complete; scan, time-decay, AIS and surveillance-stage deltas reach the trigger loop |
| T9 | `020c134` | `python -m pytest tests/env/test_frame_publisher.py tests/vis/test_replay_adapter.py tests/env/test_mixed_frame.py tests/env/test_server_runtime.py -q --maxfail=12` -> 20 passed; `python -m compileall -q src/vis/backend/replay_adapter.py src/schedule/state_manager.py src/vis/backend/frame_builder.py src/vis/backend/server.py src/env/simulation.py` passed | complete; live scenario inventory is deep-copied through StateManager and legacy replay fields normalize only at the replay boundary |
| T10 | `831800c` | `npm --prefix src/vis/frontend run build` -> PASS; `npm --prefix src/vis/frontend run test:acceptance -- tests/mixed-maritime.spec.js` -> 13 passed, 0 failed | complete; frontend consumes canonical vessel frame fields, keeps runtime create/delete available, adds I/II palette and II-class AIS control with authoritative-frame refresh, and covers replay/read-only plus revision conflict |
| T11 | `52c51c5` | `python -m pytest tests/mission/test_contact_store.py tests/mission/test_contact_assessor.py tests/mission/test_outcome_evaluator.py tests/mission/test_episode_logger.py tests/mission/test_strategy_validation.py -q` -> 117 passed; `LONGCAT_API_KEY=t11-offline-fixture python -m pytest -q` -> 1301 passed; `python scripts/evaluate_mixed_maritime.py --scenario mixed-ais --fixture --steps 1 --output-dir <temporary>` -> finished, canonical report emitted | complete; assessment events, outcome metrics, strategy reports, logs and deterministic evaluation fixture use `vessel_class` and type I/type II metric names, while legacy outcome/config inputs remain isolated in adapters |
| T12 | `e6c04bc` | `git diff --cached --check` -> PASS; documentation terminology scan excluding the current design/plan and the two compatibility boundaries -> 0 prohibited classification hits | complete; README, implementation docs, historical specs/plans, and control interface examples use I 类船舶/II 类船舶 and `type_i`/`type_ii`; the current design and plan remain the migration source documents |
| T13 | `54ae4b4` | RED contract test -> 1 expected failure; focused contract/lifecycle regressions -> 176 passed and T13 scenario suite -> 69 passed; `LONGCAT_API_KEY=t13-offline-fixture python -m pytest -q` -> 1302 passed, 0 failed, 0 skipped; `npm --prefix src/vis/frontend run build` -> PASS; `PLAYWRIGHT_BACKEND_PORT=18767 npm --prefix src/vis/frontend run test:acceptance` -> 2 passed, 0 failed; `PLAYWRIGHT_BACKEND_PORT=18768 npx playwright test tests/mixed-maritime.spec.js` -> 11 passed, 0 failed; final forbidden-term scan -> 0 matches; fixture evaluation -> finished_count 1, operational_failure_count 0, `live_verified: false` | complete; final runtime contracts now expose only canonical class/AIS fields, frontend and replay-facing frames use `vessel_class`, and old runtime properties were removed; real online model accuracy was not verified |

## Verification Notes

This file is updated after each task with the exact commands, pass/fail/skip counts, and any deviation from the implementation plan. Scripted gateways are not evidence of real online-model accuracy.

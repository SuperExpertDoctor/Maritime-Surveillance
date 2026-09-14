# Task 2 Implementation Report

## Status and scope

DONE, with the pre-existing API-config regression described below.

- Branch: `feature/mixed-maritime-llm`
- Parent: `61323580e2aba9450e682334a7257cfaa9893d18`
- Commit: the commit containing this report, with message `feat: generate configurable mixed vessels with indistinguishable civil AIS`.
- Requirements: `.superpowers/sdd/task-2-brief.md`.

The worktree already contained uncommitted Task 2 implementation and tests when this run began. Those changes were preserved, reviewed, completed, and included in this commit. Their original write/test ordering cannot be independently attested. The baseline replay and new RED/GREEN cycles below were actually executed during this run; they are not a claim that inherited production code was first written during this run.

## Delivered behavior

- `create_ship_population(config, seed, land_mask, navigator)` returns exactly N ships, with exactly the requested target count, generation-order `Ship-N` IDs, independently sampled positions and routes, and a common generic cargo label and normal speed.
- Target slots are selected by shuffling all N slots. Identity and independent target AIS Bernoulli draws use Task 1's `ship_rng_manifest` streams; placement and heading use separate deterministic streams. Changing identity counts does not perturb normal positions, routes, speed, or public vessel type.
- Civilians always receive civilian/on AIS at initialization; targets receive civilian/on or silent AIS according to the configured probability.
- Placement uses the supplied map and existing `AStarNavigator.plan_grid`. The engine supplies its actual mainland-plus-island mask and navigator configured from the existing navigation settings. Every successful initial route reaches a water boundary cell; initialization raises `PopulationPlacementError` after 200 unsuccessful placement attempts rather than returning fewer ships or using an unsafe fallback.
- AIS-on ships share naming, MMSI, bounded position error, heading, and speed reporting logic. Reports do not read hidden identity or serialize truth. Generated population ordinals provide distinct stable MMSIs, fixing collisions in the previous weighted-character checksum.
- Ships move individually along their normal routes; tracking state does not change normal motion. Formation offsets, follower movement, carrier/destroyer selection, and old escape behavior are removed.
- The ship `group_id` property is a read-only contact-ID compatibility view. Simulation lookup, departure accounting, detection, and tracking refer to individual contact IDs. Existing scheduler/UAV signatures retaining the name `group_id` carry a singleton contact key.
- Removed the engine's obsolete AIS-based military/civilian classification and automatic civilian release. Existing EO measurement, observation recording, ownership/coordinator, frame, and tracking interfaces remain usable. The legacy standalone discriminator utility is untouched and no longer used by the engine.

No ContactStore, red commander, Hybrid A* implementation changes, or new mission decision logic were added.

## TDD evidence

### Baseline RED replay

A disposable checkout was created with `git archive` of the exact Task 1 parent at `/tmp/maritime-t02-red.eOONf5`. Only the focused tests were copied into it; the active worktree was not reset or overwritten.

Initial inherited focused tests against Task 1:

```text
python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py -q
11 failed, 1 passed, 2 errors in 0.67s
```

The missing population factory caused nine population failures, two AIS failures, and two fixture setup errors. The already-existing AIS schema check passed. This was followed by a clean focused RED run without fixture errors, using a direct AIS report test that checks identity permutation, generic naming, and configured noise:

```text
python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py -q -k 'exact_configured or identity_flip' --tb=short
7 failed, 10 deselected in 0.53s
```

Six failures demonstrate missing exact-N generation for `(N,target) = (0,0), (1,0), (1,1), (8,0), (8,8), (8,3)`. The AIS failure demonstrates the old `MV-CIV` visible name. A preliminary version checking only truth permutation passed against the old generator, so the test was strengthened before accepting this as AIS RED evidence.

### Completion cycle: identifier uniqueness and compatibility view

The inherited working-tree focused suite initially passed: `14 passed in 2.68s`.

Added checks for 100 unique stable population MMSIs, a read-only legacy group alias, actual engine map integration, independent target draws, and direct truth permutation. Before fixes:

```text
python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py -q
3 failed, 16 passed in 3.22s
```

Two intended failures: only 92 unique MMSIs for 100 generation IDs, and assigning `ship.group_id` did not raise. The third was test setup: engine initialization requires `LONGCAT_API_KEY`. The engine-map test now installs a dummy credential using `monkeypatch`; it only initializes the engine and does not call the model.

After the MMSI and compatibility/contact migration fixes:

```text
python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py -q
19 passed in 3.79s
```

### Completion cycle: recorded seed replay

Review found that the inherited population used independently named seeds rather than Task 1's public run-manifest seeds. Added a test replaying shuffled identities and target AIS draws from that manifest:

```text
python -m pytest tests/mission/test_ship_population.py -q -k recorded_rng --tb=short
1 failed, 11 deselected in 0.65s
```

The recorded seed predicted a target at index 1 while the population generated a civilian. The factory now consumes `ship_identity` and `ship_ais_mode` from `ship_rng_manifest`. The final focused run below confirms GREEN.

## Final verification

Exact required T02 command, without shell credential setup:

```text
python -m pytest tests/mission/test_ship_population.py tests/mission/test_ais_generation.py -q
20 passed in 4.06s
```

Relevant environment, simulation ownership, and Task 1 regression tests:

```text
LONGCAT_API_KEY=t02-offline-test python -m pytest tests/env tests/control/test_simulation_ownership.py tests/mission/test_contracts.py tests/mission/test_config.py tests/test_runtime_configuration.py -q --tb=short -k 'not test_api_config_exposes_control_strategy_contract'
207 passed, 1 deselected in 24.68s
```

This includes the migrated formation/normal-motion tests, EO-without-AIS-classification behavior, simulation integration, frame publication, sensors, obstacles, navigation consumers, coordinator ownership, and Task 1 contract/configuration tests. No live model validation was performed.

Additional verification: `git diff --check` passed. Source review found no hidden-identity reads or ship group-field reads in `simulation.py`, and no hidden-identity reads in AIS generation.

### Existing environment/regression failures

An initial regression run without credentials produced `54 failed, 116 passed in 3.59s`; engine initialization generally stopped at the missing credential guard. With a dummy credential, the same environment/ownership suite produced `169 passed, 1 failed in 24.96s`.

The remaining failure is `tests/env/test_server_runtime.py::test_api_config_exposes_control_strategy_contract`: `src/vis/backend/server.py:282` still reads removed `ShipConfig.count_min`. Running that exact test on the untouched Task 1 archive reproduced the same failure (`1 failed in 0.60s`). It is explicitly deselected in the final broader check, not silently counted as passing. API configuration migration is outside this T02 ship/AIS/simulation change.

One preliminary test-discovery command referenced nonexistent `tests/vis`; pytest exited 4 without running tests. The corrected command uses `tests/env`, which contains the frame/server tests.

## Changed files

- `src/env/ship.py`: independent population, truth/public views, normal movement, reachable placement, recorded RNG streams.
- `src/env/ais_signal.py`: identity-blind generic AIS and stable unique generation-ordinal MMSIs.
- `src/env/simulation.py`: actual ship map/navigator integration, individual contact migration, removal of formation movement and obsolete AIS classification.
- `tests/mission/test_ship_population.py`: exact counts, deterministic legal independent routes, hidden-identity invariance, explicit placement failure, compatibility view, recorded-seed replay, actual engine-map integration.
- `tests/mission/test_ais_generation.py`: civilian broadcast, target probability extremes/mixed draws, truth permutation, public schema/noise/naming, identifier uniqueness.
- `tests/env/test_goal2_foundation.py`: replace formation/evasion expectations with independent generic vessels and tracking-independent motion; keep handoff test geometry explicit.
- `tests/env/test_ais_discrimination.py`: preserve legacy utility tests while migrating engine expectations to EO observation without AIS identity shortcuts.
- `tests/env/test_simulation_integration.py`: use a generated contact ID instead of hard-coded `G1` for shared UAV orbit tests.
- `.superpowers/sdd/task-2-report.md`: this report.

The pre-existing untracked design and implementation-plan documents are left untouched and excluded from the commit.

## Concerns and deferred boundaries

1. The baseline API-config failure above remains; this is not an all-repository-green claim.
2. T02 verifies initial route reachability using the existing navigator. Full dynamics rollout safety, replanning, and final ship navigation integration remain T05 work, as specified by the brief.
3. Singleton contact keys retain existing scheduler/UAV method names. Observation association and independently managed contact identity remain later ContactStore work.
4. Legacy frame fields such as `is_military`, `discrimination`, and `is_evading` remain neutral compatibility outputs. They do not classify ships or trigger escape behavior.
5. The initial implementation was inherited uncommitted; this report distinguishes baseline replay from the new test-first fixes rather than claiming unverifiable original TDD ordering.

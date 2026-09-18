# Persistent Coverage Execution Record

This record separates implementation evidence from the design, plan, and validation matrix. `NOT_RUN` is not a pass result.

## Baseline

Task / Gate: Repository baseline before T01
Command and working directory: `python -m pytest tests -q` from repository root
Exit code: 0
Transport: none
Config hash / seeds / actual simulation end: not applicable
Raw artifacts: none
Metric measurement: native | legacy_reconstructed: not applicable
Result: PASS
Evidence and remaining issue: Python 3.13.11, NumPy 1.26.4, pytest 9.0.3; `1385 passed in 545.60s (0:09:05)`. Frontend dependencies were present; frontend build and browser checks remain NOT_RUN.

## T01: Independent oracle and real-motion rig

Command and working directory: `python -m pytest tests/mission/test_coverage_oracle.py -q` from repository root
Red command: same command after the behavior tests were written and before the implementation; the placeholder helper produced `4 failed` with `NotImplementedError` in the first three oracle tests and `FileNotFoundError` for the not-yet-created baseline fixture.
Green command: same command after the T01 helper and fixture implementation
Green result: `4 passed in 0.33s`
Focused rig smoke command: a two-tick `make_coverage_rig` run
Focused result: `records=2 moved=True sar=True fuel=0.966666667`
Files: `tests/mission/coverage_helpers.py`, `tests/mission/test_coverage_oracle.py`, `tests/fixtures/coverage_audit_baseline.json`
Metric measurement: independent Python-set oracle; no production `CoverageMetrics` import
Result: PASS for the T01 oracle contract
Evidence and remaining issue: the helper routes commands through `UAVEntity`, `ObservationProvider.build`, `SafetyEnvelope.apply`, `UAVDynamicsExecutor.execute`, and `SARSensor.compute_swath_footprint`. It does not use the old V07 validation hook or teleport pose after initialization. Rig imaging-success conditions remain intentionally unasserted for T04/T05.

## T02: Configuration and authoritative rolling metrics

Command and working directory: `python -m pytest tests/mission/test_coverage_metrics.py tests/mission/test_config.py tests/test_runtime_configuration.py -q` from repository root
Exit code: 0
Transport: none
Config hash / seeds / actual simulation end: not applicable
Raw artifacts: `configs/mission.yaml`, `src/mission/coverage_metrics.py`
Metric measurement: native unit contract
Result: PASS
Evidence and remaining issue: Main-agent rerun produced `78 passed in 1.20s`. T02 review passed after enforcing direct-constructor windows `(30, 60, 120)` and rejecting area-overflow inputs. Runtime wiring remains for T03 and later.

## T03: Actual SAR scan, frame/replay wiring, and controlled baseline

Focused command and working directory: `python -m pytest tests/mission/test_coverage_scan_integration.py tests/env/test_coverage_frame.py tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py tests/env/test_frame_publisher.py -q` from repository root
Exit code: 0
Transport: fixture for baseline capture; real simulation engine and production frame builder
Config hash / seeds / actual simulation end: open-water config `7eaefd56d0fa7c71df75a039ac16bcd9191646a7f0b0ee9ac049f503c10a87f6`, mixed-weather config `95cdcf68e322dbac5cd548638ffb928d7a7e959dfe24d83825b5209b993ed4f9`; seeds `42 43 44 45 46`; every seed ended at simulation minute `720.0`
Raw artifacts: `outputs/evaluations/persistent/baseline-open-water/`, `outputs/evaluations/persistent/baseline-mixed/`, plus archived failed attempts under `outputs/evaluations/persistent/`
Metric measurement: native `coverage_metrics` and raw `sar_scan` telemetry; baseline capture is telemetry-only and does not evaluate endurance gates
Result: PASS for T03 implementation and required baseline capture; long-term coverage gates remain NOT_EVALUATED
Evidence and remaining issue: Main-agent rerun produced `30 passed in 9.79s`. Both baseline commands completed naturally with root `status=completed`; all ten seed manifests report `baseline_only=true`, `coverage_gate_status=not_evaluated`, `completed_steps=720`, and `frame_count=721`. Final capture SHA is `e2444e73e419c2bb8c593ddbbe2777ec9c6a02c5`; dirty patch SHA is `0bd37a6036848a767350098f0dc94651e84bd3de9015c4555a9b66390e51ea9b`; fixture source SHA is `931b89a0a5e67e01d02b8bb6dee3f237a0be8dee1f928776899a69735c0af9b1`. Independent T03 review passed. The baseline directories are controlled raw evidence only; T15 must run the independent oracle before any coverage-performance verdict, and live-model validation remains separate.

## T04: Scan geometry and control execution contract

Command and working directory: `python -m pytest tests/control/test_contracts.py tests/control/test_factory.py tests/control/test_safety.py tests/control/test_executor.py tests/control/heuristic/test_coverage_sensor_geometry.py tests/env/test_sensors_and_obstacles.py -q` from repository root
Exit code: 0
Transport: none
Config hash / seeds / actual simulation end: not applicable
Raw artifacts: `src/control/common/contracts.py`, `src/control/common/factory.py`, `src/control/common/safety.py`, `src/control/common/executor.py`, `src/control/heuristic/coverage.py`, `src/env/uav_entity.py`
Metric measurement: native control and sensor geometry contract
Result: PASS
Evidence and remaining issue: Main-agent rerun produced `77 passed in 0.75s`. T04 review passed after preserving route status across harmless map refreshes, rejecting custom planner near-range mismatches, gating imaging on actual heading/cross-track error, and clearing all acquisition state on OFF/EO/completion/refuel. T05 closed-loop motion and T06 watchdog behavior remain NOT_RUN.

## T05: Physical coverage route following and SAR closed loop

Focused command and working directory: `python -m pytest tests/control/heuristic/test_coverage_closed_loop.py tests/control/heuristic/test_coverage.py tests/utils/test_coverage_planner.py tests/control/test_route_snapshot.py -q` from repository root
Exit code: 0
Related command: `python -m pytest tests/control/heuristic/test_navigation.py tests/mission/test_coverage_oracle.py -q`
Transport: none; real `UAVEntity.apply_motion`, `SafetyEnvelope`, `UAVDynamicsExecutor`, and `SARSensor` fixture rig
Config hash / seeds / actual simulation end: representative deterministic rig scenarios; see the CSV rows for bbox, dt, actual cell count, and completion
Raw artifacts: `docs/validation/persistent-coverage-t05-motion.csv`
Metric measurement: independent real-motion footprint collection; no planned `CoveragePath.covered_cells` used as proof
Result: PASS
Evidence and remaining issue: The exact T05 command produced `43 passed`; related navigation/oracle review produced `55 passed`. Independent review passed after fixing physically continuous scan-entry headings, preserving custom navigator compatibility, retaining the exact completion tolerance, removing next-scan projection shortcuts, and retaining a real safety intervention/recovery path. All five audit rows have `missing_cells` empty and `complete=True`. T06 watchdog behavior remains NOT_RUN.

## T06: Progress watchdog, replanning, and visible diagnostics

Focused command and working directory: `python -m pytest tests/control/heuristic/test_coverage_progress.py tests/control/heuristic/test_coverage.py tests/control/test_route_snapshot.py tests/env/test_route_visual_frame.py -q` from repository root
Related command: `python -m pytest tests/control/test_factory.py tests/control/heuristic/test_coverage_sensor_geometry.py tests/control/test_simulation_ownership.py -q`
Exit code: 0 for the focused command; the related command has one known T05 integration failure
Transport: none
Config hash / seeds / actual simulation end: deterministic controller observations; no live simulation gate
Raw artifacts: `src/control/heuristic/coverage.py`, `tests/control/heuristic/test_coverage_progress.py`
Metric measurement: native controller route-progress diagnostics
Result: PASS for the T06 focused contract
Evidence and remaining issue: The exact T06 command produced `32 passed in 1.13s`. It covers two replans followed by one `task_failed`, map-version timer behavior, connector non-triggering, alignment timeout, same-timestamp idempotence, null diagnostics, route visibility after failure, generation propagation, and factory wiring. The related command produced `20 passed, 1 failed`; the failure is the pre-existing T05 integration mismatch where the production scheduler can assign a region accepted by the legacy coarse geometry check while the new physical coverage controller rejects its extended turn route. T10/T11 must make candidate edges use the same physical geometry before assignment. The `CoveragePlanner.is_region_feasible` default was restored to the legacy coarse mode so candidate-count behavior does not regress; explicit controller geometry remains enabled.

T01: PASS
T02: PASS
T03: PASS (implementation/baseline capture; endurance gate not evaluated)
T04: PASS
T05: PASS
T06: PASS (focused contract; one T05 integration regression tracked above)

## T07: Task-level SAR completion acceptance

Focused command and working directory: `python -m pytest tests/mission/test_coverage_service.py tests/mission/test_mission_task_lifecycle.py tests/control/heuristic/test_coverage.py tests/env/test_route_visual_frame.py -q` from repository root
Related command: `python -m pytest tests/env/test_route_visual_frame.py tests/schedule/test_state_manager.py tests/mission/test_simulation_flow.py tests/control/heuristic/test_task_flow.py -q`
Exit code: 0 for both commands
Transport: none; task-level service uses actual SAR cell inputs, and lifecycle tests use the real simulation coordinator
Config hash / seeds / actual simulation end: deterministic fixtures; no live-model gate
Raw artifacts: `src/mission/coverage_service.py`, `tests/mission/test_coverage_service.py`
Metric measurement: frozen bbox/fixed-domain task ledger; `completion_basis=task_sar` for verified regions and `legacy_observation` for old regions
Result: PASS
Evidence and remaining issue: Service and lifecycle tests produced `48 passed`; the broader related command produced `57 passed`. Route completion now emits `coverage_route_finished`, is held until the same tick's SAR footprint has been recorded, and only then either closes as complete or blocks with an auditable missing-cell list. Completion is queued as `search_complete` for the next control tick, so a completed coverage controller enters holding without a same-tick lease transition. Start validation is performed before assignment commit, and service identity carries `(task_id, generation, uav_id)` so local lease generations can safely be reused by a different UAV. The prior T06 scheduler/physical-route mismatch remains tracked for T10/T11.

T07: PASS

## T08: Failure release and UAV quarantine

Command and working directory: `python -m pytest tests/mission/test_coverage_failure_cleanup.py tests/control/test_coordinator.py tests/mission/test_mission_task_lifecycle.py tests/mission/test_contact_release.py tests/control/test_ownership.py -q` from repository root
Exit code: 0
Transport: none; failure fixture uses the real SimulationEngine, coordinator lease, CoverageService, task records, and BaseStation queue
Config hash / seeds / actual simulation end: deterministic fixture; no live-model gate
Raw artifacts: `src/control/common/coordinator.py`, `src/control/common/operation_registry.py`, `src/env/simulation.py`, `src/schedule/{datatypes,state_manager,task_allocator}.py`, `src/mission/task_catalog.py`, frame and display state changes
Metric measurement: native task/resource lifecycle contract
Result: PASS
Evidence and remaining issue: The required cross-module command produced `37 passed in 9.24s`; the focused cleanup/resource/candidate command produced `39 passed in 5.99s`. Quarantine increments the lease generation, stops and removes the controller, clears pending tasks/events/saved coverage/last command, and remains idempotent without installing holding. Emergency failure closes mission records and task SAR service state, releases contacts/probes/tracks/search regions and base reservations, removes refuelling queue entries without counting a refuel, marks the UAV failed at its last position, and emits one failure event. Failed UAVs are excluded from available resources, task-catalog prompt resources, mission resources, assignment preflight, and invalid active-search occupancy. Frontend frames expose `operational_status`/`failure_reason` and display `故障停用`.

T08: PASS

## T09: Whole-domain responsibility, stable geometry IDs, and fragments

Command: `python -m pytest tests/mission/test_coverage_policy.py tests/mission/test_coverage_prompt_window.py tests/mission/test_task_catalog.py tests/schedule/test_candidate_extractor.py -q`

Exit code: 0; result: `43 passed` in the focused rerun before T10/T11 additions. The final combined policy/window/candidate/catalog run produced `47 passed in 7.04s`.

Evidence: `CoveragePolicy` classifies the fixed SAR domain from actual SAR timestamps, candidate IDs use stable half-open bboxes, stale/failed/unknown UAV reservations are excluded, and residual due cells can produce bounded 1--19 cell fragment candidates or explicit `deferred_geometry` alerts. Complete candidate enumeration is separated from the prompt cap.

Result: PASS for the focused T09 contracts. Long-duration coverage and independent replay checks remain T15/T16 work.

## T10: One bounded candidate window and physical edge reuse

Command: `python -m pytest tests/mission/test_coverage_policy.py tests/mission/test_coverage_prompt_window.py tests/mission/test_prompt_window.py tests/schedule/test_candidate_extractor.py -q`

Exit code: 0; result: `47 passed in 7.04s` (including the bounded-window and legacy prompt regressions). The allocator freezes one window before constructing feasible edges and carries its IDs and source metadata into `MissionSnapshot`.

Evidence: ordinary search representatives are selected before the remaining urgent/fair slots, feasible edges are built from the frozen IDs, and the scheduler no longer applies a second independent candidate truncation for production snapshots.

Result: PASS for T10 focused contracts. The prior T06 physical-route mismatch is covered by the T10/T11 integration reruns below.

## T11: Feasible search-resource floor

Command: `python -m pytest tests/mission/test_coverage_prompt_window.py tests/mission/test_mission_scheduler.py tests/mission/test_mission_task_lifecycle.py -q`

Exit code: 0; result: the policy/scheduler/lifecycle subset passed (`26` scheduler tests in the current scheduler run, plus the coverage contracts and lifecycle regression suite). Maximum matching, oldest representative protection, generation-safe active task filtering, and explicit infeasible reasons are exercised.

Evidence: `CoverageConstraint` is part of the immutable snapshot; selection validation rejects a floor shortfall or omission of `must_service_task_ids`, while no-feasible-edge cases remain explicitly explainable instead of fabricating an assignment.

Result: PASS for focused T11 contracts. Live model behavior is not implied.

## T12: Prompt compression and stage timings

Command: `python -m pytest tests/mission/test_coverage_decision_budget.py tests/mission/test_llm_gateway.py tests/mission/test_mission_scheduler.py tests/mission/test_strategy_memory.py -q`

Exit code: 0; result: `3 + 71 + 26 + 11` focused tests passed across the pressure, gateway, scheduler, and memory runs. A separate scheduler/memory rerun produced `37 passed in 24.95s`; the prompt pressure contract produced `3 passed in 2.63s`.

Evidence: prompt serialization is an explicit allowlist, limits candidates to 40, contacts to 20, and selected contact samples to 12; it preserves generations, active records, edges, coverage constraints, and source metadata. The 1000-contact/600-sample pressure fixture stayed below 100 KiB without mutating the full snapshot. Decision-maker calls pass `max_tokens=1536`; gateway callback validation time is separated from transport time; interaction timing records preparation/prompt/model/validation/matching/total and prompt bytes. Over-budget input fails as `prompt_budget_exceeded:<field>`.

Result: PASS for offline T12 contracts. Real online model latency remains NOT_RUN.

## T13: Consecutive decision failures, pause, and role retry

Command: `python -m pytest tests/mission/test_coverage_model_failure.py tests/mission/test_failure_paths.py tests/env/test_server_runtime.py -q`

Exit code: 0; result: T13 unit contracts `5 passed in 3.33s`; the existing failure/runtime integration selection produced `27 passed in 655.94s`.

Evidence: heavyweight decision failures count once per decision; the configured third failure pauses with `blocked_role=decision_maker`, 401/402/403/configuration categories pause immediately, paused steps do not tick the clock, and operator retry uses `force_heavy=True` without ship/sensor/control stepping. Successful retry clears the streak and failed retry remains paused. Red commander retry behavior remains on its existing path.

Result: PASS for offline failure-control contracts. External model availability is not evaluated.

T14-T16: NOT_RUN

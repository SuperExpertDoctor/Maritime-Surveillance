# Real-Map Coverage Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dispatch every legally available UAV to approved work, restore executable broad-area search candidates on the default map, and improve real SAR rolling coverage without changing sensor physics or metric definitions.

**Architecture:** Geometry generation produces bounded, obstacle-clear search rectangles and retains excluded cells as ordinary residual work. The allocator exposes executable rectangles and matches all legal capacity; the LLM only selects validated IDs. C60 remains based on actual SAR observations, not assigned area.

**Tech Stack:** Python, NumPy, pytest, existing mission scheduler and simulation fixtures.

## Global Constraints

- Keep the 10-UAV fleet, 160 km/h speed, 15 km SAR swath, 30x30 fixed domain, and 60-minute actual-SAR metric unchanged.
- Preserve collision, flight-range, generation, preemption, and previously approved work protections.
- Do not authorize new tasks on an LLM failure without an explicit policy change.
- Treat C60 >= 50% as an evaluated objective, not an assertion that code changes can guarantee physically.

---

### Task 1: Executable Real-Map Partitions

**Files:** `src/mission/coverage_partition.py`, `src/schedule/candidate_extractor.py`, `tests/mission/test_fleet_partition.py`.

**Interfaces:** Extend `partition_search_mask(mask, slots, *, min_area=1, max_area=None)` while retaining existing two-argument behavior. In `CandidateExtractor.extract_pool`, pass a two-cell obstacle-clear search mask, configured minimum area, and an area cap derived from the searchable fleet capacity and sortie range. Preserve ordinary candidates for uncovered residual cells.

- [x] Add a default-map seed-42 regression proving that legal broad candidates reach the prompt and have feasible UAV edges; add a small-mask regression proving no blocked or too-small rectangle is returned and residual cells are not falsely claimed. Name the offending first-cycle behavior explicitly.
- [x] Run `python -m pytest tests/mission/test_fleet_partition.py -q` and confirm the new assertions fail for the expected reason.
- [x] Replace oversized greedy chunks with bounded splits before slot selection. Filter only after reserving space for valid large rectangles; keep all existing geometry/range checks.
- [x] Run the focused tests and the fast `tests/mission/test_zone_rolling_acceptance.py` cases, correcting only regressions caused by this behavior.

### Task 2: Fill Legal UAV Capacity

**Files:** `src/mission/mission_scheduler.py`, `src/schedule/task_allocator.py`, `src/mission/prompts/mission_scheduler.txt`, `tests/mission/test_idle_fleet_dispatch.py`, `tests/mission/test_mission_scheduler.py`.

**Interfaces:** Remove the probe-triggered fixed 80% search limit. Preserve actual probe/track assignments and legal preemption; new searches can occupy other available UAVs. Existing `CoverageConstraint` and `underutilized_feasible_work` remain the authorities for minimum/fill validation.

- [x] Add a failing test: an executable probe with ten idle UAVs and ten disjoint searches cannot prohibit otherwise legal search work. Include a mixed probe/search selection and a true no-feasible-edge case.
- [x] Run focused tests to observe rejection from the current 80% rule.
- [x] Remove the automatic 80% cap from edges, snapshot budget, validator, and prompt text while retaining probe priority through candidate selection.
- [x] Run focused scheduler, allocator, and fast zone acceptance tests.

### Task 3: Bound Real LLM Decision Time

**Files:** `configs/llm_params.yaml`, `src/mission/prompts/mission_scheduler.txt`, `tests/mission/test_config.py`, `tests/mission/test_coverage_prompt_window.py`.

**Interfaces:** Preserve strict JSON schema, visible IDs, feasible edges, and validation. Test the configured decision-maker output budget and thinking mode with existing config loader; no fabricated fallback selection.

- [x] Write a failing config/prompt test requiring a bounded structured selection output and concise prompt without an instruction to consume a long reasoning budget.
- [x] Run the test to confirm the old thinking setting or prompt causes failure.
- [x] Set decision-maker reasoning behavior and token budget suitable for the existing deadline, preserving provider compatibility and retry validation.
- [x] Run configuration, gateway, and prompt tests.

### Task 4: Coverage Proof and Limits

**Files:** `tests/mission/test_fleet_partition.py`, `tests/mission/test_coverage_scan_integration.py` (only if necessary), and existing evaluation scripts without metric changes.

**Interfaces:** The observed 60-minute `coverage_metrics.windows` percentage remains the only C60 acceptance source; logged assignment area and cumulative percentage must not substitute for it.

- [x] Run default-map seed-42 snapshot and first-step integration with deterministic fixture; inspect partition ID, edge count, ten UAV outcomes, and residual gaps.
- [ ] Finish long-running mission regression and C60 >= 60-minute simulation. The 65-step run was stopped after 15 minutes without reaching a complete C60 sample; do not interpret launch or cumulative coverage as C60.
- [x] Run `git diff --check` and audit that no SAR, speed, domain, or metric parameters were changed.

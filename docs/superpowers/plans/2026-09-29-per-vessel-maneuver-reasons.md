# Per-Vessel Maneuver Reasons Implementation Plan

> **For agentic workers:** Implement task-by-task using test-driven development. This plan is executed locally without subagents.

**Goal:** Every model-generated II-class vessel maneuver includes its own nonempty display reason alongside the maneuver parameters, with no versioned red-plan contract.

**Architecture:** `RedMotionParameters` carries `reason_content` for the specific ship. `RedCommander` requires it in each command and rejects missing or blank reasons. `SimulationEngine` publishes each installed ship's own reason instead of looking up shared provider reasoning. The public frame and existing expandable frontend view retain the complete sanitized per-ship text.

**Tech Stack:** Python dataclasses, pytest, React, Playwright.

## Global Constraints

- Remove `schema_version` only from the red commander plan; do not change unrelated schemas.
- Preserve existing maneuver safety and whole-batch validation.
- Do not alter or commit unrelated local changes.

### Task 1: Contract and validation

**Files:** `src/mission/contracts.py`, `src/mission/red_commander.py`, `src/mission/prompts/red_commander.txt`, `tests/mission/test_red_commander.py`, `tests/mission/test_contracts.py`.

- [x] Add failing tests: two commands have distinct `reason_content`; missing, whitespace, non-string, and extra version fields reject a batch; prompt requires per-ship reasons.
- [x] Run targeted tests and confirm they fail for the missing feature.
- [x] Add required `reason_content` to each command, remove the red-plan version from the plan and prompt, and keep numeric validation unchanged.
- [x] Run the red-commander and contract tests.

### Task 2: Ship state and fixtures

**Files:** `src/env/simulation.py`, `tests/mission/test_vessel_status.py`, and red-plan fixture users under `tests/` and `scripts/evaluate_mixed_maritime.py`.

- [x] Add a failing test proving different ships expose different reasons and that removing the command removes its reason.
- [x] Run the failing test.
- [x] Read the reason from the installed command; keep `motion_parameters` numeric-only and update deterministic fixture payloads and manual plan constructors.
- [x] Run related mission, navigation, visualization, and frontend tests; verify build and `git diff --check`.

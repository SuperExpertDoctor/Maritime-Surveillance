# Admin Vessel Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move vessel state and controls from the right sidebar into a live administrator table while retaining vessel drag/drop and accurate maneuver provenance.

**Architecture:** Continue publishing immutable `scenario_vessels` in every frame. Enrich each record on the simulator thread from installed ship navigation and the matching `red_commander` gateway call. Retain existing revision-checked vessel commands and move only their React controls and per-row status feedback.

**Tech Stack:** Python simulator/FastAPI, React/Vite, pytest, Playwright.

## Global Constraints

- Preserve non-vessel right-sidebar sections and existing AIS evidence expiry and forced-refresh behavior.
- Truth-only administrator fields must not enter mission decisions or contact observations.
- Type I AIS stays on; replay and disconnected views are read-only.

---

### Task 1: Simulator truth and provenance

**Files:** `src/env/simulation.py`, `src/vis/backend/frame_builder.py`, `tests/mission/test_feature_commands_integration.py`, `tests/vis/test_decision_details.py`.

**Interfaces:** `scenario_vessels[]` gains `motion_parameters`, `motion_reason_content`, `motion_plan_id`, `motion_decision_time_min`, `speed_kn`, and `heading_deg` as JSON-ready fields.

- [x] Write a test that installs a blue-side II-class plan and checks the matching ship's parameters and sanitized `reason_content`; ensure other ships cannot inherit the reason.
- [x] Run this test and confirm the new fields are absent.
- [x] Record the gateway call matching `snapshot_id`, attach provenance only on successful installation, and publish through the existing immutable inventory; expose actual speed and heading for both types.
- [x] Run the tests for plan retirement, no-reason fallback, and existing AIS off/on refresh with a writable pytest basetemp; plan reuse is covered by existing red-commander tests.

### Task 2: Per-row vessel commands

**Files:** `src/vis/frontend/src/App.jsx`, `src/vis/frontend/src/components/RightSidebar.jsx`, `src/vis/frontend/src/components/BottomDrawer.jsx`, `src/vis/frontend/src/App.css`, `src/vis/frontend/tests/decision-log.spec.js`.

**Interfaces:** Bottom drawer accepts `onSetVesselAis(vesselId, enabled)`, `onDeleteVessel(vesselId)`, `vesselCommandStatus`, `vesselCommandBusy`, and `editingAllowed`; App resolves each command against the identified row's revision.

- [x] Write a Playwright test for vessel rows ensuring a row click sends that row's ID/revision and waits for an authoritative frame before updating.
- [x] Verify it fails against the existing right-sidebar-only controls.
- [x] Move inventory/details/AIS/delete UI from the vessel editor into a compact `船舶状态` table; leave the palette, placement feedback, and all unrelated sidebar sections intact.
- [x] Run the test; extend it for Type I locked AIS, rejection/queued feedback, replay read-only, and mobile contained scrolling.

### Task 3: Regression and browser acceptance

**Files:** existing backend integration and frontend Playwright tests whose selectors rely on the old sidebar/AIS tab.

- [x] Run affected Python tests, the production frontend build, and targeted Playwright suites; resolve failures caused by changed controls without weakening unrelated assertions. Two existing seeded AIS integration tests fail during fixture selection.
- [x] Inspect desktop/mobile screenshots and exercise palette drop, row AIS/delete, and replay; run `git diff --check` and leave unrelated untracked files untouched.

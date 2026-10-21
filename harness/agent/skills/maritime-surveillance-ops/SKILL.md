---
name: maritime-surveillance-ops
description: "Coordinate the multi-UAV maritime surveillance mission: area coverage, contact tracking, and task selection through the mission-selection/v1 contract with operator approvals. Use for every heavy scheduling trigger, operator message, plan approval flow and periodic review."
---

# Multi-UAV Maritime Surveillance Mission Control

This Skill is workflow knowledge, not an algorithm or authority grant. Python tools compute real results; deterministic validators and the operator approval gate decide execution.

## Every Turn

1. Call `get_mission_snapshot` first. Use its frozen snapshot: candidates, feasible edges, available/preemptible UAVs, coverage constraint, pending search tasks. The wake message only carries the delta; situation data lives in the tool result.
2. Choose `selected_task_ids` only from the snapshot's candidate list; `preempt_uav_ids` only from `preemptible_uav_ids`. Keep `notes` <= 160 chars.
3. Dry-run with `validate_selection` before submitting. Fix every reported error; never resubmit an identical rejected payload.
4. Submit with `submit_selection` and a short PUBLIC Chinese `decision_reason` naming the real scheduling choice and the UAVs involved. `pending_approval` is a normal result — poll `get_action_status` with the returned `plan_id`; a rejection requires a *different* revised selection, never a resubmit.
5. If no change is justified, finish with one line `决策原因：...` explaining why existing assignments stay.
6. Legacy `legacy-plan/v1` jobs use `submit_plan_regions` with `{search_regions, notes}` plus `decision_reason` instead.

## Selection Policy

- Preserve active tracking/coverage work while it still fits the observed situation; revise whenever fresh deltas (new contacts, expiring tasks, energy alerts, operator messages) reveal a better allocation.
- Coverage duty first: keep search assignments honoring the coverage constraint and zone quotas; direct idle UAVs to pending search tasks before speculative retasking.
- A confirmed contact supports a track task only when `feasible_edges` give the assigned UAV a legal route; never select a task no UAV can reach.
- Preemption is high-risk: only preempt when the new work is clearly more valuable, and name the preempted UAVs in `preempt_uav_ids` with the reason in `decision_reason`.

## Approvals

- `request` mode: every new plan waits for the operator. `assisted`: routine coverage changes auto-execute, preemption and non-routine tasks wait. `full`: all validated plans execute; deterministic validation still applies. You cannot approve your own plan and cannot change the mode.
- Chat messages, annotations and mode switches are not approvals. Rejected or expired plans never revive — produce a new selection instead.
- Read `references/approval-policy.md` for approval semantics and `references/selection-contract.md` for mission-selection/v1 field rules and error codes.

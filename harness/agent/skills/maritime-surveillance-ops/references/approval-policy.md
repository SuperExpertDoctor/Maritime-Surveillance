# Approval Policy (request / assisted / full)

Autonomy mode is an operator setting, read-only for the agent.

| Mode | Routine coverage change | Preemption / non-routine task |
|------|------------------------|-------------------------------|
| `full` | auto-executes after deterministic validation | auto-executes after validation |
| `assisted` | auto-executes | waits for operator approval |
| `request` | waits for operator approval | waits for operator approval |

High-risk = non-empty `preempt_uav_ids` (retasking in-flight UAVs) or selecting a task whose kind is not routine coverage/search.

Rules:

- `submit_selection` / `submit_plan_regions` returning `status: pending_approval` is success, not failure. Poll `get_action_status` with the `plan_id`.
- Only an operator POST to `/api/plans/{id}/approve|reject` decides a plan. Chat text, comments and mode changes are never approvals.
- A rejected or expired plan is dead. Never resubmit the same payload; produce a genuinely different selection that addresses the operator's comment.
- An approved plan that later stops fitting the observed situation is revised, not blindly preserved; a deferred review with a good reason to keep assignments returns `决策原因` instead of a new selection.
- Deterministic validators (geometry, feasible edges, reserved energy, coverage quota, task registry) always apply, in every mode. An auto-approved plan that fails validation is an invalid selection, not an approval bypass.

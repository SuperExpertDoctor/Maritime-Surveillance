# mission-selection/v1 Contract

Fields in `get_mission_snapshot` → `selection_payload.snapshot`:

- `snapshot_id`: copy verbatim into the selection.
- `candidates`: task candidates with `task_id`, `kind`, `bbox`, `center`, `contact_id`, `label`. Select only listed `task_id`s.
- `feasible_edges`: directed task→UAV reachability; a selection is only executable through listed edges.
- `available_uav_ids` / `preemptible_uav_ids`: legal UAV pools. `preempt_uav_ids` may only contain preemptible ids.
- `coverage_constraint` + `pending_search_task_ids`: required coverage quota and overdue search tasks.
- `assigned_task_ids` / `existing_assignments`: work already committed.
- `information_version`: copy into `information_version` when present.

Selection object (strict, `additionalProperties: false`):

```
{ "schema_version": "mission-selection/v1",
  "snapshot_id": "<snapshot_id>",
  "selected_task_ids": ["task-..."],
  "preempt_uav_ids": ["UAV-..."],
  "defer_reason": null | "<why no change>",
  "notes": "<=160 chars operator-facing summary" }
```

`submit_selection` also requires `decision_reason` (<=500 chars, public Chinese, no internal reasoning chain — it is shown verbatim to the operator).

Common `validate_selection` errors: `invalid_snapshot_id` (stale snapshot), `missing_required_field`, `invalid_selected_task_ids` (unknown/duplicate ids), `invalid_preempt_uav_ids` (not preemptible), `no_feasible_edge` (chosen task unreachable for its UAV), `coverage_quota_unmet`, `invalid_schema_version`.

`legacy-plan/v1` jobs instead submit `{search_regions: [...region objects], notes}` via `submit_plan_regions`; regions must be valid rectangles per the plan schema in the job's `user_prompt`.

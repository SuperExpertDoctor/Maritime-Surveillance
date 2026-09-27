# Task 3 Prompt Rewrite Report

## Prompt size

Measured as UTF-8 file bytes (the prompt is ASCII):

- Before (`3bb8ad3`): 7,287 bytes.
- After: 4,940 bytes.
- Reduction: 2,347 bytes (32.2%).

## Rule audit

| Rule | Prompt rule retained | Behavioral test evidence |
| --- | --- | --- |
| Retained work | Pending searches remain retained work, not candidates; deterministic reassignment handles them. | `test_pending_search_is_a_reserved_audit_region_not_a_new_llm_candidate`, `test_pending_search_is_rejected_if_the_model_selects_it_directly`, pending reassignment tests in `test_legacy_search_scheduling.py` |
| Non-overlap | New ordinary searches cannot overlap retained/executing regions or one another. | `test_selection_rejects_unknown_ids_duplicate_contacts_and_overlapping_searches`, `test_scheduler_prompt_filters_overlapping_search_candidates` |
| Distinct UAVs / simultaneous selections | Each selected task needs a distinct eligible UAV; selections are simultaneous. | `test_matching_detects_shared_only_uav`, `test_live_scheduler_corrects_infeasible_simultaneous_selection` |
| Protected operations / preemption | Busy search requires explicit legal preemption; protected return/refuel/safety/probe/valid-track work cannot be preempted. | `test_full_capacity_probe_preempts_only_ordinary_search_after_cooldown`, `test_protected_returning_resource_and_cooldown_cannot_be_preempted`, `test_probe_preemption_preserves_existing_sar_coverage_floor` |
| Residual/global coverage floor | `required_new_search_count` is residual after active and matchable pending work; meet feasible global service requirements. | `test_residual_new_search_budget_accounts_for_pending_capacity`, `test_validator_allows_partial_floor_when_snapshot_reports_infeasible_resources` |
| Zone obligations under global infeasibility | Feasible zone representatives and must-service tasks remain binding; infeasible zones are exclusions. | Zone requirement construction/visibility tests in `test_zone_rolling_acceptance.py`; source-level prompt wording covers the model instruction. |
| Preemption floor | Coverage after preemption must not fall below the preserved SAR floor. | `test_probe_preemption_preserves_existing_sar_coverage_floor` |
| Probe admission ceiling | `ordinary_search_admission_limit` caps new ordinary admissions only while executable probe demand exists. | `test_executable_probe_reserves_real_search_admission`, `test_unexecutable_probe_does_not_limit_search_admission`, `test_search_edges_at_probe_cap_are_not_actionable_model_work` |
| Revision-specific intent acknowledgement | Review each pending revision against owners/candidates; preserve ownership and focus reservations, acknowledge when no legal addition remains. | `test_pending_focus_review_requires_meaningful_acknowledgement`, `test_owned_focus_is_reviewed_once_and_failed_review_stays_pending`, `test_focus_attaches_existing_owner_without_duplicate_reservations` |
| Idle-capacity utilization | Do not defer required feasible idle work; fill legal capacity without unnecessary preemption. | `test_empty_selection_requires_defer_reason_when_legal_work_exists`, `test_reason_does_not_allow_empty_selection_with_idle_feasible_work`, `test_underutilization_allows_rematching_selected_work_to_use_idle_uav` |
| Partition preference | Prefer large supplied `partition:*` candidates, with `search:*` as constrained fallback. | `test_ten_aircraft_partition_whole_domain_into_ten_large_regions`, `test_production_candidate_window_keeps_large_partition_boundaries` |
| Advisory memories | Memories influence prioritization only, not sensors, thresholds, safety, resources, or feasibility. | Policy is stated in the prompt; no dedicated behavioral assertion found for every advisory boundary. |
| Notes limit | Prefer empty notes; suggested 80 Unicode characters, hard validator limit 160. | `test_coverage_scan_integration.py` response assertions and mission-selection contract validation; no dedicated 80-character scheduler assertion found. |

## Verification

- RED: the new prompt payload assertion failed before the rewrite because `uav_option_columns` was absent.
- GREEN-oriented batch: prompt regression, `test_prompt_payload.py`, `test_coverage_prompt_window.py`, `test_prompt_window.py`, `test_intent_candidates.py`, and `test_fleet_partition.py`: 37 passed, 1 failed. The failure was `test_real_engine_launches_every_available_aircraft_into_larger_regions`, which expected all ten aircraft to launch; the current candidate selection has an idle UAV under executable probe demand and the new prompt retains the specified ordinary-search admission ceiling. This needs root suite reconciliation; the rule was not weakened.
- `test_coverage_policy.py` + full `test_legacy_search_scheduling.py`: 25 passed.

Prompt assertions supplement behavioral tests and do not prove semantic equivalence of model behavior. No live API experiment was run; no claim of model-quality equivalence is made.

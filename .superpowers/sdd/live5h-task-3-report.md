# Task 3: Focus Ownership, Admission and Useful Model Decisions

## Implementation

- Production `build_mission_snapshot` now annotates retained approved/executing search records with active overlapping focus IDs. The live intent/status boundary also updates the authoritative records. No extra reservation, assignment, task ID, lease, or artificial coverage evidence is created for an overlapping focus.
- A new focus revision reaches the decision maker through `pending_intent_reviews`, even when all useful work already belongs to an existing owner. The unchanged `mission-selection/v1` response can acknowledge retention using an empty selection and explanatory `notes` or `defer_reason`. Blank empty responses fail validation. Revisions are acknowledged only after successful selection validation and matching; transport, parsing, validation, and deadline failures leave them pending. Engine reset constructs a fresh scheduler and clears acknowledgement state.
- Existing coverage controllers receive `intent_focus` events. They prioritize focus-intersecting remaining swaths, reconnect from the current aircraft pose, validate the entire replacement route, and publish the normal route revision. They preserve the task/lease identity, progress offset and remaining swaths; an unsafe replacement leaves the previously validated route intact.
- Focus utility/priority precedes automatic broad partitions and ordinary fallback rectangles. Zone-containment preferences no longer displace operator focus. Candidate snapshots and actual SAR coverage measurements remain intact.
- Executable probe demand is determined using range, availability, ownership, preemption policy and cooldown checks. With such demand, the search floor is capped at `floor(0.8 * healthy_count)`, and independent selection admission validation forbids NEW searches above that ceiling. Existing validated searches are not aborted: legal probe preemption is offered, and model failure preserves their records and reservations. The 1/2/3/10 aircraft cases reserve at least one probe resource. Blocked demand does not reduce search utilization.
- `actionable_edges` is shared by prompt serialization and allocator skip decisions. It excludes unavailable/illegal assignments, retained pending-search audit edges, non-visible prompt IDs and exhausted search admissions. Empty urgent-looking candidates no longer trigger recurrent model calls. New intent revisions remain the explicit bounded review exception.
- Intent statuses use ownership plus candidate/resource evidence, with `awaiting_planning`, `no_legal_candidate`, `resource_blocked` and `waiting_assignment` separated. Actual owner-backed unmet coverage retains `coverage_below_target` / `freshness_below_target`.

## TDD Evidence

All test commands used `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q -p no:cacheprovider` and offline gateways/providers only.

1. New `tests/mission/test_live5h_scheduling.py`: initial RED reproduced unusable probe skip returning `None`; all 1/2/3/10-aircraft full-search selections admitted despite probe demand; focus losing to `partition:broad`. The first busy-fleet fixture had a misspelled resource field, corrected before production work; rerun `-k production` then produced **1 failed, 1 passed**, with desired search count **10 instead of 8** for executable probe demand and the blocked-demand control passing.
2. First GREEN command: `tests/mission/test_live5h_scheduling.py tests/mission/test_feature_control_integration.py::test_v07_real_eo_samples_classify_then_handoff_and_eo_lock tests/mission/test_mission_scheduler.py`: **57 passed** in 18.88s.
3. Focus RED command: `tests/mission/test_live5h_scheduling.py -k focus`: **4 failed, 1 passed** in 49.16s. Full and partial overlap had no owner `intent_ids`; owned focus incorrectly skipped model review; focus swath stayed last. Ownership, meaningful once-only review, failure preservation and safe route reorder were implemented only after these failures.
4. Status RED `-k status`: **4 failed**, missing candidate/resource evidence parameters. GREEN status/controller subset: **5 passed**.
5. Empty acknowledgement and exhausted admission RED `-k 'acknowledgement or at_probe_cap or unexecutable'`: **2 failed, 1 passed**. After implementation: **3 passed**. The blocked-probe control already passed and remains unchanged.
6. Cross-zone priority RED `-k zone_containment`: **1 failed**, ordinary contained rectangle displaced higher-priority focus. After implementation: **1 passed**. Full coverage policy/window rerun with that regression: **32 passed** in 0.39s.
7. Retained pending-edge RED `::test_retained_pending_edges_do_not_make_an_empty_prompt_actionable`: **1 failed**, prompt candidates were empty but skip returned `None`. Shared filtering now excludes retained audit-only edges; final rerun below covers it.
8. An exploratory tiny-strip test initially expected an executable 1x5 residual. Inspection showed it violated the existing minimum 20-cell rectangle and aspect constraints. No geometry relaxation was made, and the attempted extractor change was discarded. The corrected regression preserves pending-owner association without duplicate/illegal candidates. Legal partially overlapping 5x5 submissions retain uncovered candidates.

## Related Verification

- Named task suites plus new regressions, feature commands/episode, idle dispatch, coverage policy, and full V07 physical integration: **156 passed, 1 failed** in 119.41s. The only failure was the confirmed pre-existing fixture assuming land is invalid focus geometry. Actual `_intent_searchable_mask` intentionally includes the entire task grid; the fixture now uses an out-of-bounds bbox and preserves its rejection assertions.
- New regressions + feature episode + coverage scan integration + coverage failure cleanup + heuristic coverage controller tests: **61 passed** in 56.05s.
- Final targeted rerun: `tests/mission/test_live5h_scheduling.py tests/mission/test_mission_scheduler.py tests/mission/test_feature_commands_integration.py tests/mission/test_coverage_prompt_window.py tests/mission/test_feature_episode_integration.py`: **105 passed** in 86.43s. This includes the corrected invalid-area fixture and the retained-edge regression.
- Final physical/geometry rerun: `tests/mission/test_live5h_scheduling.py::test_undersized_focus_fragment_does_not_duplicate_its_pending_owner tests/mission/test_feature_control_integration.py::test_v07_real_eo_samples_classify_then_handoff_and_eo_lock`: **2 passed** in 17.50s. The tiny residual explicitly asserts zero actual coverage and an unmet target rather than full service.
- `git diff --check`: clean.
- Existing unknown `pytest.mark.timeout` warning remains because mandated plugin autoload is disabled. No external provider calls or historical replay edits occurred.
- Parent's much broader pre-task offline baseline is separate evidence, not a passing final-branch claim and not rerun here.

## V07 Diagnosis

The first real engine step already published two executable probes and fresh initial SAR with gap 0%, desired search count 4. The fixture submitted four searches and two probes, then incorrectly stopped ordinary work at the minimum four. The strict validator rejected the complete selection with `underutilized_feasible_work:search:0:10:4:18`. Removing only the fixture's floor-as-ceiling early exit lets it fill additional legal work through the unchanged validator. The end-to-end EO sample count, classification, tracking transition and handoff assertions were preserved and pass.

## Integration Contracts

- No change to immutable `TaskRecord`, `ControlTask`, `ControlDecision`, selection schema, assignments or sensor evidence contracts.
- Prompt-only fields: `pending_intent_reviews` (the unacknowledged active intent records) and `ordinary_search_admission_limit` (integer ceiling or null).
- Internal policy helper `build_coverage_constraint(..., search_limit=None)` caps the computed desired count without redefining the ordinary minimum as a universal maximum.
- `IntentStore.evaluate(..., candidates=(), actionable_task_ids=())` has optional keyword-only evidence. `candidates=None` means eligibility is not yet known for this revision. Original callers retain their no-candidate behavior. Assigned task IDs exclude terminal/candidate-only records.
- Live route event: `intent_focus`, payload `{task_id, intent_id, revision, bbox}`. Existing route publication and `route_revision` are used; UI should display the unchanged owner and updated intent status.
- Tiny uncovered portions below configured legal rectangle geometry are not silently declared served and are not converted into illegal tasks. Their real SAR coverage ratio remains unmet, even where an overlapping owner exists.

## Files and Self-Review

Production: `src/schedule/task_allocator.py`, `src/mission/mission_scheduler.py`, `src/mission/intent_store.py`, `src/mission/coverage_policy.py`, `src/mission/prompts/mission_scheduler.txt`, `src/env/simulation.py`, `src/control/heuristic/coverage.py`.

Fixture/test: `scripts/evaluate_mixed_maritime.py`, `tests/mission/test_live5h_scheduling.py`, `tests/mission/test_feature_episode_integration.py`, `tests/mission/test_feature_commands_integration.py`.

Self-review caught a second empty-call source: pending retained edges were still actionable even though they were absent from the prompt. Added RED coverage and excluded them in the shared graph. Reviewed all changes against task boundaries; no Task1/Task2 behavior was reverted. Existing large allocator/engine/controller files were kept in place without unrelated restructuring.

This is offline bounded verification, not a new live-provider five-hour acceptance replay. Route reprioritization is conservative: if no safe connector exists at the current pose, existing execution remains in force. It does not weaken observation thresholds or claim coverage from planned waypoints.

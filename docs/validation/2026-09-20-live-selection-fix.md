# Live selection failure repair

The seed-42 live episode stopped at minute 25 because three consecutive model
selections failed assignment validation. The prompt advertised geometric route
edges for busy search aircraft without applying the final preemption policy.
The model selected investigations/searches that could not preempt those aircraft,
and sometimes selected more simultaneous tasks than distinct aircraft.

Changes:

- Filter prompt candidates and per-task UAV edges through the final matcher's
  resource, range, cooldown and preemption rules, including explicit prompt windows.
- Feed both schema and semantic selection errors into the gateway's existing
  bounded correction loop and shared deadline. Final atomic matching remains in
  place; no deterministic substitution of model decisions and no relaxed limits.
- Clarify simultaneous assignments versus sequential work in the model prompt.
- Distinguish early model pause from completed execution in CLI output; explain
  that --hold-server retains the website without advancing simulation time.
- Publish already released passive-position contacts to control observations.
  Such contacts have no AIS/visual samples and were omitted by the legacy target
  report adapter, causing an assigned probe to enter holding immediately. The
  added path preserves stale-contact filtering and creates no visual evidence.

Validation:

- New regression tests failed before the corresponding fixes: infeasible live
  selections were not corrected; illegal preemption candidates remained visible;
  passive-only contact observations were absent.
- 163 tests passed across mission scheduler, gateway, observation, prompt windows,
  coverage policy and simulation flow (pytest logging plugin disabled because an
  existing exception test asserts stderr logging). The suite emits an existing
  unregistered timeout-marker warning.
- Probe controller plus observation tests: 21 passed. Runtime configuration: 7 passed.
- Initial live smoke run completed 30 steps: outputs/simulation_20260920_114424.jsonl.
  This exposed the passive observation adapter defect repaired above.

This repair does not establish 480-step completion, 50% rolling coverage, or 80%
classification accuracy. Those require a separate full-duration live evaluation.
The failure threshold and observation freshness limits have not been increased.

Final live smoke run after both fixes: outputs/simulation_20260920_115001.jsonl.
30/30 strictly advancing frames, final runtime_status=running, no model pause,
0 mission_selection_failed events. Still 0% cumulative SAR coverage at minute 30;
this is a regression smoke test, not mission-performance acceptance.

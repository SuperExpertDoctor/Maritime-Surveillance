# Six-Hour Live Interaction Run Design

## Objective

Run the actual `main.py` process for 21,600 seconds of wall-clock time with the
configured live LongCat provider and browser-based operator interactions. The
run validates red reconnaissance response to map focus areas while blue vessel
population and Type II AIS state change. Fixture transport and accelerated
short-run results are not acceptance evidence.

## Execution

- Start `main.py` with a fresh, non-overwriting report directory, an available
  visualization port, `--steps 1000`, `--step-delay 60`, and
  `--wall-seconds 21600`. The 60-second delay paces each one-minute simulation
  step at real-time speed, keeping the recorded frame volume bounded while the
  six-hour wall-clock budget remains authoritative. Require the live provider
  probe to pass before beginning the timed run. Preserve all existing output
  artifacts.
- Use the live browser map and controls, not configuration edits or direct
  state mutation. Schedule actions from a monotonic elapsed-time clock.
- At elapsed hours 1, 2, 3, 4, and 5, draw and submit one randomly placed 5x5
  cell `search_priority` area through the map UI. Choose a box whose 25 cells
  are all in the live frame's searchable domain, and keep each intent active
  for 360 simulation minutes so provider latency cannot expire it before the
  run ends.
- At elapsed hours 1.5, 3, and 4.5, randomly select one active Type I and one
  active Type II vessel, delete each, and place a same-class replacement at a
  legal open-water cell. For each round, randomly select about half of the
  active Type II vessels (at least one) and toggle their AIS state.
- Persist the random seed and each target cell, vessel, and requested AIS
  state so the random choices are reproducible.

## Evidence

Store a timestamped operator JSONL ledger and screenshots in the run report
directory. For every action, record intended wall time, enqueue time, command
ID, receipt status, authoritative-frame confirmation time and state, and any
rejection. Keep the `main.py` JSONL frames and `report.json` as the authoritative
simulation, model-call, and event record.

After every focus-area submission, inspect the intent status, assigned task
IDs, and subsequent live decision records. A submitted or applied intent alone
does not prove red-side response. After vessel changes, confirm deletion,
replacement class/position, and Type II AIS state in an authoritative frame.

## Timing and Failure Handling

Actions are issued at their planned wall-clock offsets. Synchronous model calls
and the real-time step cadence can delay command application; report planned
and applied timestamps separately and never label enqueue as application.
Preserve rejected, unconfirmed, or model-blocked actions as failures in the
final audit rather than silently retrying through direct API calls. A UI retry
for a visibly paused model may be used only as an explicitly logged operator
action.

The run passes only if the process spans at least 21,600 seconds, all eight
scheduled operator events are confirmed in authoritative state, and each focus
area has inspectable red-side decision/task evidence. Provider failure,
early termination, missing evidence, or an unresponsive focus area leaves the
acceptance run incomplete; it must not be represented as a successful six-hour
validation.

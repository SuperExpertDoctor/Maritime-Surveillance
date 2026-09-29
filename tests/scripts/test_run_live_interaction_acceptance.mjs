import test from "node:test";
import assert from "node:assert/strict";
import {
  acceptanceWallSeconds,
  acceptanceWindowSeconds,
  buildMainArgs,
  choosePlacementCell,
  parseArgs,
} from "../../scripts/run_live_interaction_acceptance.mjs";

test("acceptance and smoke modes have fixed seed-only command lines", () => {
  assert.deepEqual(parseArgs(["--seed", "20260929"]), { smoke: false, seed: 20260929, hours: 6 });
  assert.deepEqual(parseArgs(["--smoke", "--seed", "7"]), { smoke: true, seed: 7, hours: 6 });
  assert.deepEqual(parseArgs(["--seed", "7", "--hours", "5"]), { smoke: false, seed: 7, hours: 5 });
  assert.deepEqual(parseArgs(["--seed", "7", "--hours", "5.5"]), { smoke: false, seed: 7, hours: 5.5 });
  assert.throws(() => parseArgs(["--seed", "7", "--wall-seconds", "10"]), /unknown option/);
  assert.throws(() => parseArgs(["--seed", "7", "--hours", "0"]), /positive number of hours/);
  assert.throws(() => parseArgs(["--seed", "7", "--hours", "soon"]), /positive number of hours/);
  assert.throws(() => parseArgs(["--seed", "-1"]), /unsigned 32-bit/);
});

test("five-hour acceptance extends the wall budget past the last scheduled event", () => {
  assert.equal(acceptanceWindowSeconds(5), 18000);
  assert.equal(acceptanceWallSeconds(5), 18720);
  assert.equal(acceptanceWallSeconds(6), 21720);
  assert.equal(acceptanceWallSeconds(8), 28920);
  assert.throws(() => acceptanceWallSeconds(0), /finite and positive/);
});

test("main launch arguments keep six-hour timing fixed and allow a full provider probe", () => {
  assert.deepEqual(buildMainArgs({
    smoke: false, port: 12345, mainScript: "main.py", memoryRoot: "memory", reportDir: "report",
  }), [
    "main.py", "--steps", "1000", "--step-delay", "60", "--wall-seconds", "21720",
    "--llm-probe-timeout", "120", "--port", "12345", "--memory-root", "memory",
    "--run-report-dir", "report",
  ]);
  assert.deepEqual(buildMainArgs({
    smoke: false, hours: 5, port: 12345, mainScript: "main.py", memoryRoot: "memory", reportDir: "report",
  }), [
    "main.py", "--steps", "1000", "--step-delay", "60", "--wall-seconds", "18720",
    "--llm-probe-timeout", "120", "--port", "12345", "--memory-root", "memory",
    "--run-report-dir", "report",
  ]);
  assert.deepEqual(buildMainArgs({
    smoke: true, port: 12345, mainScript: "main.py", memoryRoot: "memory", reportDir: "report",
  }), [
    "main.py", "--steps", "1000", "--step-delay", "1", "--wall-seconds", "600",
    "--llm-probe-timeout", "120", "--port", "12345", "--memory-root", "memory",
    "--run-report-dir", "report",
  ]);
});

test("placement picker excludes mainland, obstacles, and occupied cells", () => {
  const cells = [];
  for (let x = 0; x < 30; x += 1) for (let y = 0; y < 30; y += 1) cells.push([x, y]);
  const frame = {
    search_domain: { cols: 30, rows: 30, searchable_cells: cells },
    config_snapshot: { environment: { mainland_width_cells: 5, storm_safety_margin_cells: 1.0 } },
    obstacles: [
      { id: "island-1", type: "island", center: [10, 10], size: 2 },
      { id: "storm-1", type: "thunderstorm", center: [20.2, 15.7], size: 2 },
      { id: "island-2", type: "island", vertices: [[24, 24], [26, 24], [26, 26], [24, 26]] },
    ],
    scenario_vessels: [{ position: [7.5, 7.5] }],
  };
  const isLegal = (cell) =>
    cell[0] >= 5 && cell[0] < 29 && cell[1] >= 1 && cell[1] < 29
    && Math.hypot(7.5 - (cell[0] + 0.5), 7.5 - (cell[1] + 0.5)) >= 1.1
    && !(Math.abs(cell[0] + 0.5 - 10) <= 1 && Math.abs(cell[1] + 0.5 - 10) <= 1)
    && !(Math.abs(cell[0] + 0.5 - 20.2) <= 2.25 && Math.abs(cell[1] + 0.5 - 15.7) <= 2.25)
    && !(cell[0] + 0.5 >= 24 && cell[0] + 0.5 <= 26 && cell[1] + 0.5 >= 24 && cell[1] + 0.5 <= 26);
  for (const draw of [0, 0.25, 0.5, 0.75, 0.999999]) {
    const cell = choosePlacementCell(frame, () => draw);
    assert.ok(isLegal(cell), `cell ${cell} from draw ${draw}`);
  }
  // Cell order is [x, y] row-major; the mainland masks x<5 and the margin y>=1.
  assert.deepEqual(choosePlacementCell(frame, () => 0), [5, 1]);
  // The deleted vessel's last position keeps its clearance too: [5,1] and
  // [5,2] sit within 1.1 cells of (5.5,1.5), so the pick moves to [5,3].
  assert.deepEqual(choosePlacementCell(frame, () => 0, [[5.5, 1.5]]), [5, 3]);
  // A snapshot missing `storm_safety_margin_cells` falls back to the
  // configured 1.0-cell storm margin rather than dropping the clearance.
  const sparseFrame = JSON.parse(JSON.stringify(frame));
  delete sparseFrame.config_snapshot.environment.storm_safety_margin_cells;
  for (const draw of [0, 0.5, 0.999999]) {
    assert.ok(isLegal(choosePlacementCell(sparseFrame, () => draw)), `draw ${draw}`);
  }
  assert.throws(
    () => choosePlacementCell({ ...frame, search_domain: { cols: 30, rows: 30, searchable_cells: [] } }, () => 0),
    /no searchable cells/,
  );
});

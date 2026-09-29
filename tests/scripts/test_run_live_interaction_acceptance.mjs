import test from "node:test";
import assert from "node:assert/strict";
import {
  acceptanceWallSeconds,
  acceptanceWindowSeconds,
  buildMainArgs,
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

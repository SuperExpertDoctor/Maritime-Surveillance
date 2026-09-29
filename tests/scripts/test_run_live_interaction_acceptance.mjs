import test from "node:test";
import assert from "node:assert/strict";
import { buildMainArgs, parseArgs } from "../../scripts/run_live_interaction_acceptance.mjs";

test("acceptance and smoke modes have fixed seed-only command lines", () => {
  assert.deepEqual(parseArgs(["--seed", "20260929"]), { smoke: false, seed: 20260929 });
  assert.deepEqual(parseArgs(["--smoke", "--seed", "7"]), { smoke: true, seed: 7 });
  assert.throws(() => parseArgs(["--seed", "7", "--wall-seconds", "10"]), /unknown option/);
  assert.throws(() => parseArgs(["--seed", "-1"]), /unsigned 32-bit/);
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
    smoke: true, port: 12345, mainScript: "main.py", memoryRoot: "memory", reportDir: "report",
  }), [
    "main.py", "--steps", "1000", "--step-delay", "1", "--wall-seconds", "600",
    "--llm-probe-timeout", "120", "--port", "12345", "--memory-root", "memory",
    "--run-report-dir", "report",
  ]);
});

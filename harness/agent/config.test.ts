import assert from "node:assert/strict";
import { test } from "node:test";
import { longcatConfig, redact, TOOL_NAMES } from "./config.ts";

test("LongCat uses documented compatible endpoint and bounded output", () => {
  const config = longcatConfig();
  assert.equal(config.baseUrl, "https://api.longcat.chat/openai/v1");
  assert.equal(config.models[0].id, "LongCat-2.0");
  assert.equal(config.api, "openai-completions");
  assert.ok("maxTokens" in config.models[0] && config.models[0].maxTokens <= 4096);
});

test("task allowlist cannot grant permission or execute shell", () => {
  assert.equal(TOOL_NAMES.length, 5);
  assert.ok(TOOL_NAMES.includes("submit_selection"));
  assert.ok(TOOL_NAMES.includes("submit_plan_regions"));
  assert.ok(!TOOL_NAMES.includes("bash"));
  assert.ok(!TOOL_NAMES.includes("approve"));
});

test("diagnostics redact provider and runtime credentials", () => {
  assert.equal(redact("failed secret-key bearer worker-key", ["secret-key", "worker-key"]), "failed [REDACTED] bearer [REDACTED]");
});

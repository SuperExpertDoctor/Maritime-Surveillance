import assert from "node:assert/strict";
import { test } from "node:test";
import type { AgentSessionEvent } from "@earendil-works/pi-coding-agent";
import type { AssistantMessage } from "@earendil-works/pi-ai";
import { FeedbackDelivery, PublicEventProjector, publicDecisionReason, runUntilSettled, runWithReasonRetry } from "./bridge.ts";

test("no-plan reason is explicit public content, never implicit assistant text", () => {
  assert.equal(publicDecisionReason("无需变更。\n决策原因：目标观测稳定"), "目标观测稳定");
  assert.equal(publicDecisionReason("因为稳定所以不变"), null);
  assert.equal(publicDecisionReason("决策原因：  "), null);
});

const assistant: AssistantMessage = { role: "assistant", content: [{ type: "text", text: "public" }, { type: "thinking", thinking: "hidden" }], api: "openai-completions", provider: "longcat", model: "test", usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, stopReason: "stop", timestamp: 1 };

test("public event projection keeps message identity and strips thinking and secrets", () => {
  const project = new PublicEventProjector("run-1", ["credential"]);
  const start = project.project({ type: "message_start", message: structuredClone(assistant) } as AgentSessionEvent);
  const update = project.project({ type: "message_update", message: structuredClone(assistant), assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "credential", partial: structuredClone(assistant) } } as AgentSessionEvent);
  const end = project.project({ type: "message_end", message: structuredClone(assistant) } as AgentSessionEvent);
  assert.equal(start?.message_id, update?.message_id);
  assert.equal(end?.message_id, start?.message_id);
  assert.equal(update?.text, "[REDACTED]");
  assert.equal(end?.text, "public");
  assert.ok(!JSON.stringify(end).includes("hidden"));
  assert.equal(project.project({ type: "message_update", message: structuredClone(assistant), assistantMessageEvent: { type: "thinking_delta", contentIndex: 1, delta: "hidden", partial: structuredClone(assistant) } } as AgentSessionEvent), undefined);
  assert.deepEqual(project.project({ type: "agent_settled" }), { type: "agent_settled" });
  assert.equal(project.project({ type: "auto_retry_start", attempt: 1, maxAttempts: 2, delayMs: 10, errorMessage: "credential" })?.text, "[REDACTED]");
});

test("tool events have public call identity, result and error status", () => {
  const project = new PublicEventProjector("run", ["credential"]);
  const result = project.project({ type: "tool_execution_end", toolCallId: "c1", toolName: "get_mission_snapshot", result: { content: [{ type: "text", text: "credential" }], details: { private: "not-public" } }, isError: true });
  assert.equal(result?.tool_call_id, "c1");
  assert.equal(result?.text, "[REDACTED]");
  assert.equal(result?.is_error, true);
  assert.ok(!JSON.stringify(result).includes("not-public"));
});

test("credentials split across stream chunks are never forwarded", () => {
  const project = new PublicEventProjector("run", ["credential"]);
  project.project({ type: "message_start", message: structuredClone(assistant) });
  const first = project.project({ type: "message_update", message: structuredClone(assistant), assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "cred", partial: structuredClone(assistant) } });
  const second = project.project({ type: "message_update", message: structuredClone(assistant), assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "ential", partial: structuredClone(assistant) } });
  assert.equal(first, undefined);
  assert.equal(second?.text, "[REDACTED]");
});

test("feedback is acknowledged only after native enqueue and delivered once", async () => {
  const received: string[] = [];
  let reject = true;
  const delivery = new FeedbackDelivery({ steer: async (text: string) => { if (reject) throw new Error("busy"); received.push(text); }, followUp: async (text: string) => { received.push(text); } });
  const feedback = [{ id: "a", text: "steer", delivery: "steer" }, { id: "b", text: "later", delivery: "followUp" }];
  await assert.rejects(delivery.deliver(feedback));
  assert.deepEqual(delivery.acknowledgedIds(), []);
  reject = false;
  await delivery.deliver(feedback);
  await delivery.deliver(feedback);
  assert.deepEqual(received, ["steer", "later"]);
  assert.deepEqual(delivery.acknowledgedIds(), ["a", "b"]);
  delivery.confirmAcknowledged(["a"]);
  assert.deepEqual(delivery.acknowledgedIds(), ["b"]);
  await delivery.deliver(feedback);
  assert.deepEqual(received, ["steer", "later"]);
});

test("prompt completion waits for native idle before job completion", async () => {
  let release: (() => void) | undefined;
  const idle = new Promise<void>((resolve) => { release = resolve; });
  let complete = false;
  const running = runUntilSettled({ prompt: async () => {}, waitForIdle: () => idle }, "test").then(() => { complete = true; });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(complete, false);
  release?.();
  await running;
  assert.equal(complete, true);
});

test("a missing public plan reason prompts exactly one corrective retry", async () => {
  const reason = { missing: true };
  const prompts: string[] = [];
  const session = { prompt: async (text: string) => { prompts.push(text); if (prompts.length === 2) reason.missing = false; }, waitForIdle: async () => {} };
  await runWithReasonRetry(session, "initial", reason);
  assert.equal(prompts.length, 2);
  assert.match(prompts[1], /decision_reason/);
  reason.missing = true;
  const failing = { prompt: async (text: string) => { prompts.push(text); reason.missing = true; }, waitForIdle: async () => {} };
  await assert.rejects(runWithReasonRetry(failing, "initial", reason), /decision_reason_missing_after_retry/);
  assert.equal(prompts.length, 4);
});


import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { createAgentSession, DefaultResourceLoader, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import { TOOL_NAMES } from "./config.ts";
import { missionExtension } from "./extension.ts";
import type { BackendToolSpec } from "./tool-adapter.ts";
import { Value } from "typebox/value";
import { createMissionResources, readTrustedSkill } from "./resources.ts";
import { fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { createHarness, getUserTexts } from "../packages/coding-agent/test/suite/harness.ts";
import { FeedbackDelivery, PublicEventProjector, runUntilSettled, type PublicSessionEvent } from "./bridge.ts";

// Mirror of adapter/schedule_adapter.py's tool parameter schemas so these
// tests exercise the same shapes the adapter registers in production.
const ids = { type: "array", items: { type: "string", minLength: 1 }, maxItems: 64, uniqueItems: true };
const selection = {
  type: "object",
  properties: {
    schema_version: { const: "mission-selection/v1" },
    snapshot_id: { type: "string", minLength: 1 },
    selected_task_ids: ids,
    preempt_uav_ids: ids,
    defer_reason: { type: ["string", "null"] },
    notes: { type: "string", maxLength: 160 },
    information_version: { type: "integer", minimum: 0 },
  },
  required: ["schema_version", "snapshot_id", "selected_task_ids", "preempt_uav_ids", "defer_reason", "notes"],
  additionalProperties: false,
};
const obj = (properties: Record<string, unknown>, required?: string[]) => ({ type: "object", properties, additionalProperties: false, ...(required ? { required } : {}) });
const TEST_SPECS: BackendToolSpec[] = [
  { name: "get_mission_snapshot", description: "state", parameters: obj({}) },
  { name: "validate_selection", description: "validate", parameters: obj({ selection }, ["selection"]) },
  { name: "submit_selection", description: "submit", parameters: obj({ selection, decision_reason: { type: "string", minLength: 1, maxLength: 500 } }, ["selection", "decision_reason"]), execution_mode: "sequential" },
  { name: "submit_plan_regions", description: "regions", parameters: obj({ search_regions: { type: "array", items: { type: "object" }, maxItems: 24 }, notes: { type: "string", maxLength: 400 }, decision_reason: { type: "string", minLength: 1, maxLength: 500 } }, ["search_regions", "notes", "decision_reason"]), execution_mode: "sequential" },
  { name: "get_action_status", description: "status", parameters: obj({ action_id: { type: "string", minLength: 1 } }, ["action_id"]) },
];

test("real SDK exposes five independent strict mission tools and no coding tools", async () => {
  const directory = await mkdtemp(join(tmpdir(), "uav-sdk-test-"));
  let session;
  try {
    const settingsManager = SettingsManager.inMemory();
    const resourceLoader = new DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager,
      noExtensions: true, noSkills: true, noThemes: true, noContextFiles: true, noPromptTemplates: true,
      extensionFactories: [missionExtension(async () => ({ status: "test" }), TEST_SPECS)] });
    await resourceLoader.reload();
    ({ session } = await createAgentSession({ cwd: directory, agentDir: directory, resourceLoader, settingsManager,
      tools: [...TOOL_NAMES], sessionManager: SessionManager.inMemory(directory) }));
    assert.deepEqual(session.getActiveToolNames().sort(), [...TOOL_NAMES].sort());
    assert.ok(!session.getActiveToolNames().includes("bash"));
    assert.equal(session.getActiveToolNames().length, 5);
    const tools = session.agent.state.tools;
    const schema = (name: string) => {
      const found = tools.find((tool) => tool.name === name);
      assert.ok(found);
      assert.equal(found.parameters.additionalProperties, false);
      return found.parameters;
    };
    assert.equal(Value.Check(schema("get_mission_snapshot"), { members: ["UAV-1"] }), false);
    const legal = { schema_version: "mission-selection/v1", snapshot_id: "s1", selected_task_ids: ["t1"], preempt_uav_ids: [], defer_reason: null, notes: "keep" };
    assert.equal(Value.Check(schema("submit_selection"), { selection: legal, decision_reason: "理由" }), true);
    assert.equal(Value.Check(schema("submit_selection"), { selection: legal }), false);
    assert.equal(Value.Check(schema("submit_selection"), { selection: { ...legal, selected_task_ids: ["t1", "t1"] }, decision_reason: "x" }), false);
    assert.equal(Value.Check(schema("validate_selection"), { selection: legal }), true);
    assert.equal(Value.Check(schema("submit_plan_regions"), { search_regions: [], notes: "n", decision_reason: "r" }), true);
    assert.equal(Value.Check(schema("get_action_status"), {}), false);
  } finally {
    session?.dispose();
    await rm(directory, { recursive: true, force: true });
  }
});

test("native session streams tools and drains steer and followUp before settled", async () => {
  let feedback: FeedbackDelivery | undefined;
  const harness = await createHarness({
    settings: { compaction: { enabled: false }, retry: { enabled: false } },
    initialActiveToolNames: [...TOOL_NAMES], allowedToolNames: [...TOOL_NAMES],
    extensionFactories: [missionExtension(async () => {
      await feedback?.deliver([{ id: "s", text: "operator steering", delivery: "steer" }, { id: "f", text: "operator follow-up", delivery: "followUp" }]);
      return { status: "succeeded", episode_id: "offline" };
    }, TEST_SPECS)],
  });
  try {
    feedback = new FeedbackDelivery(harness.session);
    harness.setResponses([
      fauxAssistantMessage(fauxToolCall("get_mission_snapshot", {}, { id: "mission-read" }), { stopReason: "toolUse" }),
      fauxAssistantMessage("steering handled"),
      fauxAssistantMessage("follow-up handled"),
    ]);
    const project = new PublicEventProjector("offline", []);
    const projected: PublicSessionEvent[] = [];
    harness.session.subscribe((event) => { const value = project.project(event); if (value) projected.push(value); });
    await runUntilSettled(harness.session, "inspect state");
    assert.deepEqual(getUserTexts(harness), ["inspect state", "operator steering", "operator follow-up"]);
    assert.equal(harness.session.getLastAssistantText(), "follow-up handled");
    assert.equal(harness.session.isIdle, true);
    assert.equal(harness.session.pendingMessageCount, 0);
    assert.ok(projected.some((event) => event.type === "message_update" && event.text));
    assert.ok(projected.some((event) => event.type === "tool_execution_end" && event.tool_call_id === "mission-read"));
    assert.equal(projected.at(-1)?.type, "agent_settled");
  } finally {
    harness.cleanup();
  }
});

test("loader discovers every skill under the skills root and read stays inside it", async () => {
  const directory = await mkdtemp(join(tmpdir(), "uav-trusted-skill-test-"));
  const skillsRoot = join(directory, "skills");
  await mkdir(join(skillsRoot, "alpha-ops", "references"), { recursive: true });
  await writeFile(join(skillsRoot, "alpha-ops", "SKILL.md"), "---\nname: alpha-ops\ndescription: test skill\n---\n# Alpha Ops\nbody");
  await writeFile(join(skillsRoot, "alpha-ops", "references", "detail.md"), "detail body");
  let session;
  try {
    const settingsManager = SettingsManager.inMemory();
    const resourceLoader = await createMissionResources(directory, skillsRoot, settingsManager, async () => ({}), TEST_SPECS);
    await resourceLoader.reload();
    assert.deepEqual(resourceLoader.getSkills().skills.map((skill) => skill.name), ["alpha-ops"]);
    ({ session } = await createAgentSession({ cwd: directory, agentDir: directory, resourceLoader, settingsManager, tools: [...TOOL_NAMES, "read"], sessionManager: SessionManager.inMemory(directory) }));
    assert.deepEqual(session.getActiveToolNames().sort(), [...TOOL_NAMES, "read"].sort());
    const tool = session.agent.state.tools.find((entry) => entry.name === "read");
    assert.ok(tool);
    const result = await tool.execute("read-1", { path: join(skillsRoot, "alpha-ops", "SKILL.md") });
    assert.ok(JSON.stringify(result).includes("Alpha Ops"));
    assert.ok((await readTrustedSkill(skillsRoot, "alpha-ops/references/detail.md")).includes("detail body"));
    await assert.rejects(tool.execute("read-2", { path: resolve("package.json") }), /untrusted_skill_path/);
    await assert.rejects(readTrustedSkill(skillsRoot, "../../../package.json"), /untrusted_skill_path/);
    await symlink(resolve("package.json"), join(directory, "evil.md"));
    await assert.rejects(readTrustedSkill(directory, "evil.md"), /untrusted_skill_path/);
  } finally {
    session?.dispose();
    await rm(directory, { recursive: true, force: true });
  }
});

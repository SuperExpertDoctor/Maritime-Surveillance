import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { createAgentSession, ModelRuntime, SessionManager, SettingsManager, type AgentSession } from "@earendil-works/pi-coding-agent";
import { longcatConfig, redact } from "./config.ts";
import { FeedbackDelivery, PublicEventProjector, publicDecisionReason, runUntilSettled, runWithReasonRetry } from "./bridge.ts";
import { createMissionResources } from "./resources.ts";
import type { BackendToolSpec } from "./tool-adapter.ts";
import { RoutineCooldown, withRunHeartbeat } from "./scheduling.ts";

const root = resolve(import.meta.dirname, "..", "..");
const api = process.env.ADAPTER_BASE_URL || "http://127.0.0.1:8765";
const workerToken = process.env.ADAPTER_WORKER_TOKEN;
const key = process.env.LONGCAT_API_KEY;
if (!workerToken || !key) throw new Error("Set ADAPTER_WORKER_TOKEN and LONGCAT_API_KEY before starting PI worker");
const secrets = [workerToken, key];
const runtimeDir = resolve(process.env.ADAPTER_PI_RUNTIME_DIR || resolve(root, "outputs/pi"));
await mkdir(runtimeDir, { recursive: true, mode: 0o700 });
const modelRuntime = await ModelRuntime.create({ authPath: resolve(runtimeDir, "auth.json"), modelsPath: resolve(runtimeDir, "models.json"), allowModelNetwork: false });
modelRuntime.registerProvider("longcat", longcatConfig());
await modelRuntime.setRuntimeApiKey("longcat", key);
const model = modelRuntime.getModel("longcat", process.env.LONGCAT_MODEL || "LongCat-2.0");
if (!model) throw new Error("LongCat model registration failed");

const SUBMIT_TOOLS = new Set(["submit_selection", "submit_plan_regions"]);

interface Job { run_id: string; episode_id: string; kind: string; text: string; source: string }
let activeJob: Job | undefined;
let session: AgentSession | undefined;
let sessionEpisode = "";
let running = true;
let calls = 0;
let networkFailures = 0;
let feedback: FeedbackDelivery | undefined;
let heartbeatTask: Promise<void> = Promise.resolve();
let runFailure: Error | undefined;
let deadline: ReturnType<typeof setTimeout> | undefined;
let startDeadline: (() => void) | undefined;
const schedule = new RoutineCooldown();

async function request(path: string, data: unknown, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const response = await fetch(`${api}${path}`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${workerToken}` },
    body: JSON.stringify(data), signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000) });
  const result = await response.json() as Record<string, unknown>;
  if (!response.ok) throw new Error(String(result.error_code || `HTTP_${response.status}`));
  return result;
}

async function getJson(path: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${api}${path}`, { headers: { Authorization: `Bearer ${workerToken}` }, signal: AbortSignal.timeout(30000) });
  const result = await response.json() as Record<string, unknown>;
  if (!response.ok) throw new Error(String(result.error_code || `HTTP_${response.status}`));
  return result;
}

async function makeSession(episode: string): Promise<AgentSession> {
  // Tool catalog is backend-sourced: GET /internal/agent/tools is the single
  // contract; the adapter turns each spec into a full ToolDefinition.
  const toolSpecs = ((await getJson("/internal/agent/tools")).tools ?? []) as BackendToolSpec[];
  const toolNames = toolSpecs.map((entry) => entry.name);
  const pluginToolNames = new Set(toolSpecs.filter((entry) => entry.plugin).map((entry) => entry.name));
  const activeTools = [...toolNames.filter((name) => !pluginToolNames.has(name)), "read", ...(pluginToolNames.size ? ["load_plugin_tools"] : [])];
  const settings = SettingsManager.inMemory({ compaction: { enabled: true }, retry: { enabled: true, maxRetries: 1, baseDelayMs: 2000 } });
  const loader = await createMissionResources(runtimeDir, resolve(import.meta.dirname, "skills"), settings, async (name, params, signal) => {
      if (!activeJob || ++calls > 24) throw new Error("turn_tool_budget_exceeded");
      const job = activeJob;
      await heartbeatRun(job, signal);
      if (runFailure) throw runFailure;
      const result = await request(`/internal/tools/${name}`, { ...params, run_id: job.run_id, episode_id: job.episode_id }, signal);
      if (result.status !== "pending_approval" || typeof result.plan_id !== "string") return result;
      clearTimeout(deadline);
      deadline = undefined;
      try {
        let current = result;
        while (current.status === "pending_approval") {
          await delay(2000, undefined, { signal });
          if (activeJob !== job || runFailure) throw runFailure || new Error("run_cancelled");
          await heartbeatRun(job, signal);
          current = await request("/internal/tools/get_action_status", { action_id: result.plan_id, run_id: job.run_id, episode_id: job.episode_id }, signal);
        }
        return current;
      } finally {
        if (activeJob === job && !runFailure) startDeadline?.();
      }
  }, toolSpecs);
  await loader.reload();
  const { session: created } = await createAgentSession({ cwd: runtimeDir, agentDir: runtimeDir, modelRuntime, model,
    thinkingLevel: "off", tools: [...toolNames, "read", ...(pluginToolNames.size ? ["load_plugin_tools"] : [])], resourceLoader: loader, settingsManager: settings,
    sessionManager: SessionManager.continueRecent(runtimeDir, resolve(runtimeDir, episode)) });
  created.setActiveToolsByName(activeTools);
  if (created.getActiveToolNames().sort().join(",") !== activeTools.slice().sort().join(",")) throw new Error("Mission tool registration incomplete");
  return created;
}

function heartbeatRun(job: Job, signal?: AbortSignal): Promise<void> {
  heartbeatTask = heartbeatTask.then(async () => {
    if (activeJob !== job) return;
    const ids = feedback?.acknowledgedIds() || [];
    const response = await request("/internal/agent/heartbeat", { run_id: job.run_id, episode_id: job.episode_id, acknowledged_feedback_ids: ids, model: model.id }, signal);
    feedback?.confirmAcknowledged(ids);
    if (response.cancel) throw new Error("run_cancelled");
    if (session?.isStreaming) await feedback?.deliver(response.feedback);
  });
  return heartbeatTask;
}

function jobPrompt(job: Job): string {
  if (job.kind === "legacy-plan/v1") {
    return `Episode ${job.episode_id}. 搜索区规划任务（legacy-mission-plan）。调用 get_mission_snapshot 获取规划输入，给出 {search_regions, notes} 并调用 submit_plan_regions 提交（decision_reason 必填，简短中文说明真实理由）。若不需要新区域，最后一行输出 决策原因：... 说明维持现状的真实原因。`;
  }
  if (job.kind === "operator-message/v1") {
    return `Episode ${job.episode_id}. 操作员消息（来自 ${job.source}）：${job.text}\n先调用 get_mission_snapshot 查看当前态势再回复；如需调整任务分配，走 submit_selection 流程。`;
  }
  return `Episode ${job.episode_id}. 调度决策任务（mission-selection/v1，触发源 ${job.source}）：${job.text}\n先用 read 读取 <available_skills> 中的可信技能，然后调用 get_mission_snapshot 获取 mission-selection/v1 输入；产出 selection 并用 validate_selection 自检，最后调用 submit_selection 提交（decision_reason 必填，简短中文说明真实调度理由与被影响的 UAV）。校验失败要修正后重新提交，绝不能重复提交相同载荷。若决定不做任何变更，最后一行输出 决策原因：... 说明维持现有任务分配的真实原因。`;
}

process.on("SIGTERM", () => { running = false; void session?.abort(); });
process.on("SIGINT", () => { running = false; void session?.abort(); });
console.log("PI worker ready: LongCat, maritime mission tools active");
while (running) {
  try {
    const next = await request("/internal/agent/jobs", { action: "next", ...schedule.nextRequest(performance.now()) });
    networkFailures = 0;
    const job = next.job as Job | null;
    if (!job) { await delay(1000); continue; }
    activeJob = job;
    calls = 0;
    runFailure = undefined;
    heartbeatTask = Promise.resolve();
    if (!session || sessionEpisode !== job.episode_id) {
      session?.dispose();
      session = await makeSession(job.episode_id);
      sessionEpisode = job.episode_id;
    }
    const turnSession = session;
    feedback = new FeedbackDelivery(turnSession);
    const projector = new PublicEventProjector(job.run_id, secrets);
    let events: Promise<void> = Promise.resolve();
    let eventCount = 0;
    const reasonState = { missing: false };
    let planSubmitted = false;
    const invalidReasonCalls = new Set<string>();
    const unsubscribe = turnSession.subscribe((event) => {
      const projected = projector.project(event);
      if (!projected) return;
      if (event.type === "tool_execution_start" && SUBMIT_TOOLS.has(event.toolName)) {
        const args: unknown = event.args;
        if (!args || typeof args !== "object" || !("decision_reason" in args)
          || typeof args.decision_reason !== "string" || !args.decision_reason.trim() || args.decision_reason.trim().length > 500) invalidReasonCalls.add(event.toolCallId);
      }
      if (event.type === "tool_execution_end" && SUBMIT_TOOLS.has(event.toolName)) {
        if (!event.isError) { reasonState.missing = false; planSubmitted = true; }
        else if (invalidReasonCalls.has(event.toolCallId) || projected.text?.includes("decision_reason_required") || projected.text?.includes("invalid_decision_reason")) reasonState.missing = true;
      }
      if (++eventCount > 12000) {
        runFailure = new Error("turn_event_budget_exceeded");
        void turnSession.abort();
        return;
      }
      events = events.then(async () => {
        if (runFailure) return;
        await request("/internal/agent/event", { run_id: job.run_id, episode_id: job.episode_id, type: "session_event", event: projected });
      }).catch((error: unknown) => {
        runFailure = error instanceof Error ? error : new Error("event_delivery_failed");
        void turnSession.abort();
      });
    });
    startDeadline = () => { deadline = setTimeout(() => { runFailure = new Error("turn_deadline_exceeded"); void turnSession.abort(); }, 180000); };
    startDeadline();
    try {
      await withRunHeartbeat(async () => {
        await runWithReasonRetry(turnSession, jobPrompt(job), reasonState);
        if (!planSubmitted && !publicDecisionReason(turnSession.getLastAssistantText() || "")) {
          await runUntilSettled(turnSession, "本轮没有提交新的决策，缺少公开决策原因。请基于真实观测，用一行“决策原因：...”说明为什么维持现有任务安排。");
          if (!planSubmitted && !publicDecisionReason(turnSession.getLastAssistantText() || "")) throw new Error("decision_reason_missing_after_retry");
        }
        await heartbeatRun(job);
        await events;
        if (runFailure) throw runFailure;
        const last = [...turnSession.messages].reverse().find((message) => message.role === "assistant");
        if (last?.role === "assistant" && (last.stopReason === "error" || last.stopReason === "aborted")) {
          throw new Error(last.errorMessage || last.stopReason);
        }
        const finalText = redact(turnSession.getLastAssistantText() || "本轮没有新增操作。", secrets);
        await request("/internal/agent/event", { run_id: job.run_id, episode_id: job.episode_id, type: "completed", text: finalText,
          ...(!planSubmitted ? { decision_reason: publicDecisionReason(finalText) } : {}) });
      }, () => heartbeatRun(job), (error: unknown) => {
        runFailure = error instanceof Error ? error : new Error("heartbeat_failed");
        void turnSession.abort();
      });
    } finally {
      clearTimeout(deadline);
      deadline = undefined;
      startDeadline = undefined;
      unsubscribe();
      await turnSession.abort();
      await heartbeatTask.catch(() => {});
      await events;
      feedback = undefined;
    }
    activeJob = undefined;
    schedule.completed(performance.now());
  } catch (error) {
    const message = redact(error instanceof Error ? error.message : "worker_error", secrets).slice(0, 300);
    if (activeJob) {
      await request("/internal/agent/event", { run_id: activeJob.run_id, episode_id: activeJob.episode_id, type: "failed", error: message }).catch(() => {});
      activeJob = undefined;
    }
    console.error(`PI worker: ${message}`);
    if (++networkFailures > 60) { console.error("PI worker: adapter unreachable, exiting"); break; }
    await delay(5000);
  }
}
session?.dispose();

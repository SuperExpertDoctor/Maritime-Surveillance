import type { AgentSessionEvent } from "@earendil-works/pi-coding-agent";
import { redact } from "./config.ts";

export interface PublicSessionEvent {
  type: string;
  message_id?: string;
  role?: string;
  text?: string;
  tool_call_id?: string;
  tool_name?: string;
  is_error?: boolean;
  status?: string;
  attempt?: number;
  will_retry?: boolean;
  count?: number;
}

function publicText(value: unknown): string {
  if (!value || typeof value !== "object" || !("content" in value) || !Array.isArray(value.content)) return "";
  return value.content.filter((part: unknown): part is { type: "text"; text: string } => !!part && typeof part === "object" && "type" in part && part.type === "text" && "text" in part && typeof part.text === "string").map((part) => part.text).join("\n");
}

export class PublicEventProjector {
  private runId: string;
  private secrets: readonly string[];
  private sequence = 0;
  private currentMessage = "";
  private pendingText = "";

  constructor(runId: string, secrets: readonly string[]) {
    this.runId = runId;
    this.secrets = secrets;
  }

  project(event: AgentSessionEvent): PublicSessionEvent | undefined {
    let result: PublicSessionEvent;
    switch (event.type) {
      case "message_start":
      case "message_update":
      case "message_end": {
        if (event.message.role !== "assistant") return undefined;
        if (event.type === "message_start" || !this.currentMessage) {
          this.currentMessage = `${this.runId}:assistant:${++this.sequence}`;
          this.pendingText = "";
        }
        if (event.type === "message_update" && event.assistantMessageEvent.type !== "text_delta") return undefined;
        result = { type: event.type, role: "assistant", message_id: this.currentMessage,
          text: event.type === "message_update" && event.assistantMessageEvent.type === "text_delta" ? event.assistantMessageEvent.delta : publicText(event.message) };
        if (event.type === "message_update") {
          const text = redact(this.pendingText + result.text, this.secrets);
          let held = 0;
          // A credential may straddle provider chunks. Retain incomplete prefixes until the next delta.
          for (const secret of this.secrets) {
            for (let length = 1; length < secret.length && length <= text.length; length++) {
              if (text.endsWith(secret.slice(0, length))) held = Math.max(held, length);
            }
          }
          this.pendingText = held ? text.slice(-held) : "";
          result.text = held ? text.slice(0, -held) : text;
          if (!result.text) return undefined;
        }
        if (event.type === "message_end") {
          result.status = event.message.stopReason === "error" || event.message.stopReason === "aborted" ? "failed" : "completed";
          this.currentMessage = "";
          this.pendingText = "";
        }
        break;
      }
      case "tool_execution_start":
      case "tool_execution_update":
      case "tool_execution_end":
        result = { type: event.type, tool_call_id: event.toolCallId, tool_name: event.toolName,
          message_id: `${this.runId}:tool:${event.toolCallId}`,
          text: event.type === "tool_execution_start" ? JSON.stringify(event.args) : publicText(event.type === "tool_execution_end" ? event.result : event.partialResult) };
        if (event.type === "tool_execution_end") result.is_error = event.isError;
        break;
      case "agent_start":
      case "agent_settled":
        result = { type: event.type };
        break;
      case "agent_end":
        result = { type: event.type, will_retry: event.willRetry };
        break;
      case "queue_update":
        result = { type: event.type, count: event.steering.length + event.followUp.length };
        break;
      case "compaction_start":
        result = { type: event.type, text: event.reason };
        break;
      case "compaction_end":
        result = { type: event.type, text: event.errorMessage || event.reason, is_error: event.aborted || !!event.errorMessage, will_retry: event.willRetry };
        break;
      case "auto_retry_start":
      case "summarization_retry_scheduled":
        result = { type: event.type, text: event.errorMessage, attempt: event.attempt };
        break;
      case "auto_retry_end":
        result = { type: event.type, text: event.finalError, attempt: event.attempt, is_error: !event.success };
        break;
      case "summarization_retry_attempt_start":
      case "summarization_retry_finished":
        result = { type: event.type };
        break;
      default:
        return undefined;
    }
    if (result.text !== undefined) result.text = redact(result.text, this.secrets).slice(0, 32000);
    return result;
  }
}

interface FeedbackSession {
  steer(text: string): Promise<unknown>;
  followUp(text: string): Promise<unknown>;
}

export class FeedbackDelivery {
  private session: FeedbackSession;
  private delivered = new Set<string>();
  private pending = new Set<string>();

  constructor(session: FeedbackSession) { this.session = session; }

  async deliver(feedback: unknown): Promise<void> {
    if (!Array.isArray(feedback)) return;
    for (const item of feedback) {
      if (!item || typeof item !== "object" || typeof item.id !== "string" || typeof item.text !== "string" || (item.delivery !== "steer" && item.delivery !== "followUp")) throw new Error("invalid_feedback");
      if (this.delivered.has(item.id)) continue;
      await this.session[item.delivery as "steer" | "followUp"](item.text);
      this.delivered.add(item.id);
      this.pending.add(item.id);
    }
  }

  acknowledgedIds(): string[] { return [...this.pending]; }
  confirmAcknowledged(ids: readonly string[]): void { for (const id of ids) this.pending.delete(id); }
}

export async function runUntilSettled(session: { prompt(text: string): Promise<void>; waitForIdle(): Promise<void> }, text: string): Promise<void> {
  await session.prompt(text);
  await session.waitForIdle();
}

export async function runWithReasonRetry(session: { prompt(text: string): Promise<void>; waitForIdle(): Promise<void> }, text: string, reason: { missing: boolean }): Promise<void> {
  await runUntilSettled(session, text);
  if (!reason.missing) return;
  reason.missing = false;
  await runUntilSettled(session, "上次提交任务选择时缺少公开调度理由。请检查任务状态；如仍需提交，提供真实、简短的 decision_reason 并重新调用 submit_selection 或 submit_plan_regions。不要编造观测或审批。");
  if (reason.missing) throw new Error("decision_reason_missing_after_retry");
}

export function publicDecisionReason(text: string): string | null {
  const reason = text.match(/(?:^|\n)决策原因[：:]\s*([^\n]+)/)?.[1]?.trim();
  return reason && reason.length <= 500 ? reason : null;
}

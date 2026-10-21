import type { ProviderConfigInput } from "../packages/coding-agent/src/core/provider-composer.ts";

export const TOOL_NAMES: readonly string[] = ["get_mission_snapshot", "validate_selection", "submit_selection", "submit_plan_regions", "get_action_status"];

export function longcatConfig(): ProviderConfigInput & { models: NonNullable<ProviderConfigInput["models"]> } {
  return {
    baseUrl: "https://api.longcat.chat/openai/v1",
    api: "openai-completions",
    apiKey: "LONGCAT_API_KEY",
    models: [{ id: process.env.LONGCAT_MODEL || "LongCat-2.0", name: process.env.LONGCAT_MODEL || "LongCat-2.0",
      reasoning: false, input: ["text"], contextWindow: 1000000, maxTokens: 4096,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, maxTokensField: "max_tokens" } }],
  };
}

export function redact(text: string, secrets: readonly string[]): string {
  for (const secret of secrets) if (secret) text = text.replaceAll(secret, "[REDACTED]");
  return text.replace(/ak_[A-Za-z0-9]+/g, "[REDACTED]");
}

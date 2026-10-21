import type { TSchema } from "typebox";
import type { ConstrainedSamplingConfig } from "@earendil-works/pi-ai";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";

/**
 * Backend tool descriptor — the JSON half of pi's ToolDefinition, served by
 * GET /internal/agent/tools. Field names stay snake_case to match the
 * backend contract (agent_tools.contract / plugins TOOLS); the adapter maps
 * them onto the ToolDefinition pieces.
 */
export interface BackendToolSpec {
  name: string;
  label?: string;
  description: string;
  snippet?: string;
  guidelines?: string[];
  parameters: Record<string, unknown>;
  constrained_sampling?: false | ConstrainedSamplingConfig;
  execution_mode?: "sequential" | "parallel";
  /** Backend-set marker: the tool came from a plugin's TOOLS map, so it
   *  registers lazily (inactive until load_plugin_tools activates it). */
  plugin?: boolean;
}

/**
 * plugin/agent-tool spec -> pi registerTool() definition.
 *
 * The adapter is the whole bridge: a plugin file dropped into
 * tools/uuv_game/plugins/ that declares TOOLS lands in the backend catalog,
 * the worker passes every catalog entry through this function, and the
 * result is a native tool the model can call — no per-tool TS code needed.
 * execute() forwards the call to POST /internal/tools/<name> where the
 * plugin's own execute(runtime, params) runs.
 */
export function backendToolAdapter(
  spec: BackendToolSpec,
  call: (name: string, params: Record<string, unknown>, signal?: AbortSignal) => Promise<unknown>,
): ToolDefinition {
  return {
    name: spec.name,
    label: spec.label ?? spec.name,
    description: spec.description,
    ...(spec.snippet ? { promptSnippet: spec.snippet } : {}),
    ...(spec.guidelines?.length ? { promptGuidelines: spec.guidelines } : {}),
    parameters: spec.parameters as TSchema,
    ...(spec.constrained_sampling !== undefined ? { constrainedSampling: spec.constrained_sampling } : {}),
    ...(spec.execution_mode === "sequential" ? { executionMode: "sequential" as const } : {}),
    execute: async (_id, parameters, signal) => {
      const result = await call(spec.name, parameters as Record<string, unknown>, signal);
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: undefined };
    },
  };
}

import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { TSchema } from "typebox";
import { backendToolAdapter, type BackendToolSpec } from "./tool-adapter.ts";

/**
 * Mission-tool extension. Tool specs are no longer defined here — the
 * backend catalog (GET /internal/agent/tools) is the single source: ten
 * builtin tools live in tools/uuv_game/agent_tools/, and any plugin file
 * that declares TOOLS joins the same catalog. Every entry goes through
 * backendToolAdapter, which translates it into a full ToolDefinition
 * (description + promptSnippet + promptGuidelines + parameters +
 * constrainedSampling + executionMode + execute).
 *
 * Plugin tools (spec.plugin) are registered but start inactive so their
 * snippets/guidelines stay out of the system prompt until needed; the
 * agent-side load_plugin_tools loader lists and activates them on demand
 * (pi.setActiveTools), and the worker trims the initial active set to the
 * builtin tools right after session creation.
 */
export function missionExtension(call: (name: string, params: Record<string, unknown>, signal?: AbortSignal) => Promise<unknown>, specs: BackendToolSpec[]) {
  const pluginToolNames = new Set(specs.filter((spec) => spec.plugin).map((spec) => spec.name));
  return (pi: ExtensionAPI) => {
    pi.on("before_provider_request", (event) => ({ ...event.payload as Record<string, unknown>, max_tokens: 4096, thinking: { type: "disabled" } }));
    for (const spec of specs) {
      pi.registerTool(backendToolAdapter(spec, call));
    }
    pi.registerTool(loadPluginTools(pi, pluginToolNames));
  };
}

function loadPluginTools(pi: ExtensionAPI, pluginToolNames: ReadonlySet<string>): ToolDefinition {
  return {
    name: "load_plugin_tools",
    label: "Load plugin tools",
    description: "List and activate registered plugin tools. Plugin tools start inactive to keep the system prompt small. Call with an empty tool_names array to list every inactive plugin tool (name, description, parameters); call with names to activate them for the rest of the session, then invoke them by name like any other tool.",
    promptSnippet: "List or activate plugin tools on demand",
    promptGuidelines: [
      "Plugin tools start inactive: call load_plugin_tools with an empty tool_names to discover what is available, then activate the ones you need before calling them.",
    ],
    parameters: {
      type: "object",
      properties: { tool_names: { type: "array", items: { type: "string" } } },
      required: ["tool_names"],
      additionalProperties: false,
    } as TSchema,
    execute: async (_id, parameters) => {
      const requested = ((parameters as { tool_names?: string[] }).tool_names ?? []);
      const active = new Set(pi.getActiveTools());
      const registry = new Map(pi.getAllTools().map((tool) => [tool.name, tool]));
      if (requested.length === 0) {
        const inactive = [...pluginToolNames]
          .filter((name) => !active.has(name) && registry.has(name))
          .map((name) => {
            const tool = registry.get(name);
            return { name, description: tool?.description, parameters: tool?.parameters };
          });
        return {
          content: [{ type: "text", text: inactive.length ? JSON.stringify(inactive) : "No inactive plugin tools are registered." }],
          details: { inactive },
        };
      }
      const activated = requested.filter((name) => pluginToolNames.has(name) && registry.has(name));
      const skipped = requested.filter((name) => !activated.includes(name));
      if (activated.length) pi.setActiveTools([...active, ...activated]);
      const remaining = [...pluginToolNames].filter((name) => !active.has(name) && !activated.includes(name) && registry.has(name));
      return {
        content: [{ type: "text", text: `Activated: ${activated.join(", ") || "none"}. Skipped (not an inactive plugin tool): ${skipped.join(", ") || "none"}. Still inactive: ${remaining.join(", ") || "none"}.` }],
        details: { activated, skipped, inactive: remaining },
      };
    },
  };
}

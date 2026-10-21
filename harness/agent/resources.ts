import { readdir, readFile, realpath } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";
import { DefaultResourceLoader, type SettingsManager } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { missionExtension } from "./extension.ts";
import type { BackendToolSpec } from "./tool-adapter.ts";

async function skillEntryPoints(skillsRoot: string): Promise<string[]> {
  try {
    const entries = await readdir(skillsRoot, { withFileTypes: true });
    return entries
      .filter((entry) => entry.isDirectory())
      .map((entry) => resolve(skillsRoot, entry.name, "SKILL.md"))
      .sort();
  } catch {
    return [];
  }
}

/** Only Markdown inside the skills root is trusted; no other filesystem reads. */
export async function readTrustedSkill(skillsRoot: string, requestedPath: string): Promise<string> {
  const root = await realpath(skillsRoot);
  const requested = isAbsolute(requestedPath) ? resolve(requestedPath) : resolve(root, requestedPath);
  const inside = relative(root, requested);
  if (inside.startsWith("..") || isAbsolute(inside) || !inside.endsWith(".md")) throw new Error("untrusted_skill_path");
  const actual = await realpath(requested);
  if (actual !== requested || relative(root, actual).startsWith("..")) throw new Error("untrusted_skill_path");
  const text = await readFile(actual, "utf8");
  if (text.length > 32000) throw new Error("skill_resource_too_large");
  return text;
}

export async function createMissionResources(directory: string, skillsRoot: string, settingsManager: SettingsManager, call: (name: string, params: Record<string, unknown>, signal?: AbortSignal) => Promise<unknown>, toolSpecs: BackendToolSpec[]): Promise<DefaultResourceLoader> {
  const additionalSkillPaths = await skillEntryPoints(skillsRoot);
  return new DefaultResourceLoader({ cwd: directory, agentDir: directory, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    additionalSkillPaths,
    systemPrompt: "You coordinate a 2D multi-UAV maritime surveillance GAME. Reply in concise Chinese. Before mission decisions, read a trusted skill from <available_skills> with the restricted read tool. Only use registered mission tools and trusted skill reads. No real-world vehicle control. Never invent execution, observations or approvals. Every submit_selection or submit_plan_regions call needs a nonempty PUBLIC decision_reason explaining the actual scheduling choice and the involved UAVs; if the tool rejects a missing reason, correct it and retry. Public outputs only; do not reveal hidden reasoning or credentials.",
    extensionFactories: [missionExtension(call, toolSpecs), (pi) => {
      pi.registerTool({ name: "read", label: "Read trusted mission skill", description: "Read a Markdown file under the trusted skills root (a SKILL.md or one of its references). Accept an absolute path inside that root or a path relative to it. No arbitrary filesystem access.",
        parameters: Type.Object({ path: Type.String({ minLength: 1 }) }, { additionalProperties: false }),
        execute: async (_id, params, signal) => {
          signal?.throwIfAborted();
          const text = await readTrustedSkill(skillsRoot, params.path);
          signal?.throwIfAborted();
          return { content: [{ type: "text", text }], details: {} };
        },
      });
    }],
  });
}

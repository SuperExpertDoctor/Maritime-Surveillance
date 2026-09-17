import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(frontendDir, "../../..");
const replayRoot = process.env.REPLAY_ACCEPTANCE_DIR;
if (!replayRoot) {
  throw new Error("REPLAY_ACCEPTANCE_DIR is required for replay acceptance tests");
}

const acceptanceDir = path.resolve(replayRoot);
const backendPort = 18866;
const frontendPort = 5186;
const baseURL = `http://127.0.0.1:${frontendPort}`;
const quote = (value) => JSON.stringify(value);

export default defineConfig({
  testMatch: "tests/replay-restoration.spec.js",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: 1,
  use: {
    baseURL,
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `python scripts/run_replay_acceptance_server.py --output-dir ${quote(acceptanceDir)} --port ${backendPort}`,
      cwd: projectRoot,
      url: `http://127.0.0.1:${backendPort}/api/replay/list`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      cwd: frontendDir,
      env: { ...process.env, VITE_BACKEND_PORT: String(backendPort) },
      url: baseURL,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
  reporter: [["list"]],
});

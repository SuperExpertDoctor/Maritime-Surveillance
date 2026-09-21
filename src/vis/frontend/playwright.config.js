import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH
  || (process.platform === "win32" ? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" : undefined);
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:5180";
const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(frontendDir, "../../..");
const backendPort = process.env.PLAYWRIGHT_BACKEND_PORT || "18766";
const backendURL = `http://127.0.0.1:${backendPort}`;

export default defineConfig({
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: 1,
  testIgnore: ["tests/legacy-search-scheduling.spec.js"],
  use: {
    baseURL,
    ...(executablePath ? { launchOptions: { executablePath } } : {}),
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `python scripts/run_visual_fixture_server.py --config configs --port ${backendPort}`,
      cwd: projectRoot,
      url: `${backendURL}/api/config`,
      reuseExistingServer: true,
      timeout: 900_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 5180",
      cwd: frontendDir,
      env: { ...process.env, VITE_BACKEND_PORT: backendPort },
      url: baseURL,
      reuseExistingServer: true,
      timeout: 30_000,
    },
  ],
  reporter: [["list"]],
});

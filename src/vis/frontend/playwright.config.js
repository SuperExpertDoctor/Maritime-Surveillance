import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const defaultChromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH
  || (process.platform === "win32" && existsSync(defaultChromePath)
    ? defaultChromePath
    : undefined);
const baseURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:5180";
const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(frontendDir, "../../..");
const backendPort = process.env.PLAYWRIGHT_BACKEND_PORT || "18766";
const backendURL = `http://127.0.0.1:${backendPort}`;
const quote = (value) => JSON.stringify(value);
const pythonExecutable = process.env.PLAYWRIGHT_PYTHON
  || path.join(projectRoot, ".runtime", "venv-py314", "Scripts", "python.exe");
const npmExecutable = process.env.PLAYWRIGHT_NPM
  || path.join(projectRoot, ".runtime", "node-v24.19.0-win-x64", "npm.cmd");

export default defineConfig({
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: 1,
  testIgnore: ["tests/legacy-search-scheduling.spec.js", "tests/production-drawer.spec.js", "tests/drawer-integration.spec.js"],
  use: {
    baseURL,
    ...(executablePath ? { launchOptions: { executablePath } } : {}),
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `${quote(pythonExecutable)} scripts/run_visual_fixture_server.py --config configs --port ${backendPort}`,
      cwd: projectRoot,
      url: `${backendURL}/api/config`,
      reuseExistingServer: true,
      timeout: 900_000,
    },
    {
      command: `${quote(npmExecutable)} run dev -- --host 127.0.0.1 --port 5180`,
      cwd: frontendDir,
      env: { ...process.env, VITE_BACKEND_PORT: backendPort },
      url: baseURL,
      reuseExistingServer: true,
      timeout: 30_000,
    },
  ],
  reporter: [["list"]],
});

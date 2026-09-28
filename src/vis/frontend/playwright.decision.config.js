import { defineConfig } from "@playwright/test";

const port = 5193;

export default defineConfig({
  testDir: "./tests",
  testMatch: ["decision-log.spec.js", "drawer-state.spec.js", "telemetry-consistency.spec.js"],
  timeout: 45_000,
  expect: { timeout: 8_000 },
  workers: 1,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: true,
    timeout: 30_000,
  },
});

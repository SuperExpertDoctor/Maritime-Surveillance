import { defineConfig } from "@playwright/test";

export default defineConfig({
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: 0,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5173",
    launchOptions: {
      executablePath: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    },
    trace: "retain-on-failure",
  },
  reporter: [["list"]],
});

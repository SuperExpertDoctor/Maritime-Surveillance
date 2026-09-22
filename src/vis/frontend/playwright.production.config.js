import { defineConfig } from '@playwright/test';
// Does not start/stop servers, mock routes, invoke models, or mutate the mission.
export default defineConfig({
  testMatch: ['tests/production-drawer.spec.js'], timeout: 60000, workers: 1,
  use: { baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:8766',
    viewport: { width: 1600, height: 1000 }, trace: 'retain-on-failure' },
});

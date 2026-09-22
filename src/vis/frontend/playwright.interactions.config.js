import { defineConfig } from '@playwright/test';
import config from './playwright.config.js';

// These browser tests supply their own HTTP responses and telemetry frames.
export default defineConfig({
  ...config,
  testMatch: ['tests/vessel-interactions.spec.js', 'tests/mixed-maritime.spec.js'],
  webServer: {
    ...config.webServer[1],
    command: 'npm run dev -- --host 127.0.0.1 --port 5180',
  },
});

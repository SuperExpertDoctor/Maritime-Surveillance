import { defineConfig } from '@playwright/test';
export default defineConfig({
  testMatch: ['tests/drawer-integration.spec.js'], timeout: 30000, workers: 1,
  use: { baseURL: 'http://127.0.0.1:18769', trace: 'retain-on-failure' },
  webServer: { command: 'PYTHONPATH=. python tests/vis/drawer_fixture_server.py', cwd: '../../..',
    url: 'http://127.0.0.1:18769/api/config', reuseExistingServer: false },
});

import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '.', testMatch: '*.spec.mjs', workers: 1, timeout: 60000,
  use: { browserName: 'chromium', headless: true, trace: 'retain-on-failure' },
  reporter: [['list']],
});

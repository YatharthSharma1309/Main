import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.js',
  timeout: 60_000,
  reporter: [['list'], ['json', { outputFile: 'tests/playwright_results.json' }]],
  use: {
    baseURL: 'http://localhost:3000',
    headless: true,
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
});

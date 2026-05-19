import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  // the live specs share one session
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:5175',
    viewport: { width: 1280, height: 1000 },
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
    },
  },
  webServer: [
    {
      command: 'uv run uvicorn server.tests.demo_app:app --host 127.0.0.1 --port 8001',
      url: 'http://127.0.0.1:8001/api/health',
      env: { CYD_TILES: '0', CYD_FEEDS: '0', CYD_MODE: 'live' },
      reuseExistingServer: !process.env.CI,
    },
    {
      command: 'uv run uvicorn server.app:app --host 127.0.0.1 --port 8003',
      url: 'http://127.0.0.1:8003/api/health',
      env: { CYD_TILES: '0', CYD_FEEDS: '0', CYD_MODE: 'live', CYD_FEED_CACHE: '.cache/e2e-empty-live' },
      reuseExistingServer: !process.env.CI,
    },
    {
      command: 'npm run dev -- --port 5176 --strictPort',
      url: 'http://127.0.0.1:5176',
      env: { CYD_API_URL: 'http://127.0.0.1:8003' },
      reuseExistingServer: !process.env.CI,
    },
    {
      command: 'npm run dev -- --port 5175 --strictPort',
      url: 'http://127.0.0.1:5175',
      env: { CYD_API_URL: 'http://127.0.0.1:8001' },
      reuseExistingServer: !process.env.CI,
    },
  ],
})

import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:8911',
  },
  webServer: {
    command:
      'cd .. && rm -f .local_agent/playwright.sqlite3 && LOCAL_AGENT_SKIP_SUDO_FOR_MODEL_LOAD=true LOCAL_AGENT_DATABASE_URL=sqlite:///./.local_agent/playwright.sqlite3 LOCAL_AGENT_MOCK_MODELS=true LOCAL_AGENT_USE_DBOS=false uv run local-agent serve --host 127.0.0.1 --port 8911',
    url: 'http://127.0.0.1:8911/health',
    timeout: 30_000,
    reuseExistingServer: false,
  },
})

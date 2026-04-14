import { defineConfig, devices } from "@playwright/test"

/**
 * Phase 49E — Playwright config.
 *
 * Spins up both the FastAPI backend and the Next.js dev server as a
 * single `webServer` list so tests run against a live integration.
 * Kept deliberately small: headless Chromium only, one happy-path
 * spec. Expanded browser coverage and shards land as a follow-up.
 */

const BACKEND_PORT = Number(process.env.OMNISIGHT_E2E_BACKEND_PORT ?? "18830")
const FRONTEND_PORT = Number(process.env.OMNISIGHT_E2E_FRONTEND_PORT ?? "3100")

// On systems where the Playwright-shipped browsers need extra shared
// libraries (libnspr4 / libnss3 / libasound2), OMNISIGHT_PW_LIB_DIR lets
// callers point LD_LIBRARY_PATH at a user-space copy without sudo.
// Leave the env var unset on CI where `npx playwright install --with-deps`
// already put the libs in system paths.
const pwLibDir = process.env.OMNISIGHT_PW_LIB_DIR
if (pwLibDir) {
  process.env.LD_LIBRARY_PATH = pwLibDir +
    (process.env.LD_LIBRARY_PATH ? ":" + process.env.LD_LIBRARY_PATH : "")
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,  // dev server + backend are singleton-ish
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1440, height: 900 },
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: undefined },
    },
  ],

  webServer: [
    {
      // Backend — FastAPI. `reuseExistingServer` makes local iteration
      // painless when the dev already has it up.
      command: `python3 -m uvicorn backend.main:app --host 127.0.0.1 --port ${BACKEND_PORT}`,
      env: {
        // E2E browser origin differs from dev default; whitelist it so
        // cross-origin EventSource (used by the SSE round-trip test)
        // isn't blocked by CORS.
        OMNISIGHT_EXTRA_CORS_ORIGINS: `http://127.0.0.1:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT}`,
      },
      port: BACKEND_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      // Frontend — production build via `next start`. Using dev mode
      // (Turbopack or webpack) under Next 16 + React 19 leaves onClick
      // handlers unhydrated intermittently in the E2E browser, swallowing
      // UI clicks. Building once and serving with `next start` gives
      // deterministic hydration, which is what E2E actually needs.
      command: `sh -c 'next build >/dev/null && next start --port ${FRONTEND_PORT}'`,
      env: { BACKEND_URL: `http://127.0.0.1:${BACKEND_PORT}` },
      port: FRONTEND_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 300_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
})

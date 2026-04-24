/**
 * Z.4 #293 checkbox 7 — Playwright config for visual-regression specs.
 *
 * Separate from the base `playwright.config.ts` because visual specs fully
 * stub `/api/v1/**` at the browser layer via `page.route()` — no FastAPI
 * backend is needed. The base config's webServer insists on booting the
 * backend, which (a) costs ~10 s we don't need, (b) trips strict-mode env
 * checks on developer workstations whose `.env` carries real production
 * credentials, and (c) is not the thing the visual spec is testing anyway.
 *
 * Usage:
 *
 *   pnpm exec playwright test \
 *     --config=playwright.visual.config.ts
 *
 * Tests under `e2e/*-visual.spec.ts` (currently just
 * z4-provider-rollup-visual.spec.ts) match this config's `testMatch`.
 * Other e2e specs keep using the base config + live backend.
 */

import { defineConfig, devices } from "@playwright/test"

const FRONTEND_PORT = Number(process.env.OMNISIGHT_E2E_FRONTEND_PORT ?? "3100")

const pwLibDir = process.env.OMNISIGHT_PW_LIB_DIR
if (pwLibDir) {
  process.env.LD_LIBRARY_PATH = pwLibDir +
    (process.env.LD_LIBRARY_PATH ? ":" + process.env.LD_LIBRARY_PATH : "")
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*-visual\.spec\.ts/,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 60_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1440, height: 900 },
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],

  webServer: [
    {
      // Frontend only — production build + `next start`. Every `/api/v1/**`
      // request is stubbed in-spec via `page.route()`, so the absence of
      // an upstream backend does not affect the page under test.
      command: `sh -c './node_modules/.bin/next build >/dev/null && ./node_modules/.bin/next start --port ${FRONTEND_PORT}'`,
      // Point the rewrites target at a port no one listens on. In-spec
      // `page.route()` handlers intercept requests at the browser layer
      // before they would ever traverse this rewrite; the setting is here
      // only to satisfy next.config.mjs's `backendUrl` lookup during build.
      env: { BACKEND_URL: "http://127.0.0.1:65535" },
      port: FRONTEND_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 300_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
})

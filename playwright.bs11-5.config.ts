/**
 * BS.11.5 — Playwright config for the catalog visual-regression spec.
 *
 * Separate from `playwright.visual.config.ts` because BS.11.5 uses
 * `next dev` (turbopack) instead of `next build && next start`. The
 * production build path triggers a known SSR module-evaluation cycle
 * in the catalog-card / catalog-tab modules under Next 16 turbopack
 * (`Cannot access 'aO' before initialization`); the dev server bundles
 * eagerly and avoids the cycle entirely. The visual spec only needs
 * the rendered DOM to match the production component contract — `next
 * dev` is sufficient and considerably faster (no production build
 * step) for this row.
 *
 * Usage:
 *
 *   OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.bs11-5.config.ts
 *
 * Tests under `e2e/bs11-5-*-visual.spec.ts` match this config's
 * `testMatch`. Other visual specs (notably
 * `e2e/z4-provider-rollup-visual.spec.ts`) keep using
 * `playwright.visual.config.ts` because their fixture page is small
 * and the production build is the more accurate environment for
 * them.
 */

import { defineConfig, devices } from "@playwright/test"

const FRONTEND_PORT = Number(process.env.OMNISIGHT_E2E_FRONTEND_PORT ?? "3101")

const pwLibDir = process.env.OMNISIGHT_PW_LIB_DIR
if (pwLibDir) {
  process.env.LD_LIBRARY_PATH = pwLibDir +
    (process.env.LD_LIBRARY_PATH ? ":" + process.env.LD_LIBRARY_PATH : "")
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: /bs11-5-.*-visual\.spec\.ts/,
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
      // Frontend in dev mode (turbopack). Production build trips a
      // module-evaluation cycle on the catalog modules; dev mode
      // avoids it. Every `/api/v1/**` request is stubbed in-spec via
      // `page.route()`, so the absence of an upstream backend does
      // not affect the page under test.
      command: `./node_modules/.bin/next dev --port ${FRONTEND_PORT}`,
      // Point the rewrites target at a port no one listens on. In-spec
      // `page.route()` handlers intercept requests at the browser layer
      // before they would ever traverse this rewrite; the setting is here
      // only to satisfy `next.config.mjs`'s `backendUrl` lookup.
      env: { BACKEND_URL: "http://127.0.0.1:65535" },
      port: FRONTEND_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
})

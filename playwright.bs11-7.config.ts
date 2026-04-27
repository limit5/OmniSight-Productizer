/**
 * BS.11.7 — Playwright config for the Platforms-page Lighthouse-
 * equivalent a11y audit spec. Same shape as the BS.11.5 / BS.11.6
 * sibling configs:
 *
 *   • Runs `next dev` (turbopack) on port 3103 so the catalog
 *     modules' SSR module-evaluation cycle in Next 16 turbopack
 *     stays out of the loop. (BS.11.5 documents the cycle in detail;
 *     BS.11.7 reuses the same fixture page at
 *     `app/e2e-fixtures/catalog-page/page.tsx`.)
 *   • `testMatch` scoped to `bs11-7-*-a11y.spec.ts` so the spec
 *     never runs alongside the BS.11.5 visual matrix or the BS.11.6
 *     perf sampler — axe injection is heavy and would distort the
 *     perf measurement if interleaved.
 *   • Port 3103 avoids colliding with bs11-5's 3101 and bs11-6's
 *     3102 if all three configs are exercised in parallel during
 *     local development.
 *   • `workers: 1` + `fullyParallel: false` — axe `axe.run()` does a
 *     full DOM walk and is CPU-heavy; running scenarios in parallel
 *     would add timing noise without speed gains.
 *   • `timeout: 90s` — axe injection + run on the rendered Platforms
 *     fixture takes ~4-8 s per scenario; 90 s leaves slack for CI.
 *
 * Usage:
 *
 *   OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.bs11-7.config.ts
 *
 * The JSON a11y reports land in `test-results/bs11-7-platforms-a11y-*`
 * (already gitignored under `/test-results/`). Each scenario's
 * verdict + axe violations are attached as
 * `bs11-7-<scenario>.json` so the report bundle ships them.
 */

import { defineConfig, devices } from "@playwright/test"

const FRONTEND_PORT = Number(process.env.OMNISIGHT_E2E_FRONTEND_PORT ?? "3103")

const pwLibDir = process.env.OMNISIGHT_PW_LIB_DIR
if (pwLibDir) {
  process.env.LD_LIBRARY_PATH = pwLibDir +
    (process.env.LD_LIBRARY_PATH ? ":" + process.env.LD_LIBRARY_PATH : "")
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: /bs11-7-.*-a11y\.spec\.ts/,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"]],
  timeout: 90_000,
  expect: { timeout: 15_000 },

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
      // `next dev` mirrors BS.11.5 / BS.11.6's choice — production
      // build trips a module-evaluation cycle on the catalog modules
      // under Next 16 turbopack. Dev mode bundles eagerly and avoids
      // it. Every `/api/v1/**` request is stubbed in-spec via
      // `page.route()` so the absence of an upstream backend does
      // not affect the page under test.
      command: `./node_modules/.bin/next dev --port ${FRONTEND_PORT}`,
      env: { BACKEND_URL: "http://127.0.0.1:65535" },
      port: FRONTEND_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
})

/**
 * BS.11.6 — Playwright config for the catalog FPS-budget perf spec.
 *
 * Same shape as `playwright.bs11-6.config.ts`'s sibling
 * `playwright.bs11-5.config.ts` — runs `next dev` (turbopack) instead
 * of `next build && next start` so the catalog modules' SSR
 * module-evaluation cycle in Next 16 turbopack stays out of the loop.
 * BS.11.5 documents the cycle in detail; BS.11.6 reuses the same
 * fixture page (`app/e2e-fixtures/catalog-page/page.tsx`) and only
 * needs the rendered DOM + browser frame schedule to be production-
 * representative — `next dev` is sufficient.
 *
 * Differences from `playwright.bs11-5.config.ts`:
 *   • `testMatch` scoped to `bs11-6-*-perf.spec.ts` so the spec
 *     never runs alongside the visual diff matrix (perf sampling is
 *     sensitive to other workers' CPU pressure).
 *   • Uses port 3102 to avoid colliding with bs11-5's port 3101 if
 *     both configs are run in parallel during local development.
 *   • `timeout` widened to 90s — each scenario runs ~5s of measurement
 *     plus open/settle/throttle overhead; the 4× CPU-throttled scenario
 *     in particular needs slack on CI runners.
 *   • `workers: 1` + `fullyParallel: false` — perf sampling demands a
 *     quiet CPU; running scenarios in parallel would self-poison the
 *     measurement.
 *
 * Usage:
 *
 *   OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.bs11-6.config.ts
 *
 * Strict mid-tier budget gate (operator-run):
 *
 *   OMNISIGHT_BS11_6_PERF_STRICT=1 \
 *     OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.bs11-6.config.ts
 *
 * The JSON FPS reports land in `test-results/bs11-6-catalog-perf-...`
 * (already gitignored under `/test-results/`). They are attached to
 * each test as `bs11-6-<scenario>.json` so the report bundle ships
 * them.
 */

import { defineConfig, devices } from "@playwright/test"

const FRONTEND_PORT = Number(process.env.OMNISIGHT_E2E_FRONTEND_PORT ?? "3102")

const pwLibDir = process.env.OMNISIGHT_PW_LIB_DIR
if (pwLibDir) {
  process.env.LD_LIBRARY_PATH = pwLibDir +
    (process.env.LD_LIBRARY_PATH ? ":" + process.env.LD_LIBRARY_PATH : "")
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: /bs11-6-.*-perf\.spec\.ts/,
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
      // `next dev` mirrors BS.11.5's choice — production build trips
      // a module-evaluation cycle on the catalog modules under Next
      // 16 turbopack. Dev mode bundles eagerly and avoids it. Every
      // `/api/v1/**` request is stubbed in-spec via `page.route()`
      // so the absence of an upstream backend does not affect the
      // page under test.
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

/**
 * Q.8 (#302) — nightly Playwright config.
 *
 * Runs ONLY the multi-device parity harness in `test/e2e/`. Kept in a
 * separate config from `playwright.config.ts` so the main PR pipeline's
 * `frontend-e2e` job stays lean — the six-scenario parity sweep takes
 * noticeably longer than the per-PR happy-path specs and would bloat
 * every PR's wall-clock if bolted on.
 *
 * The companion GH Actions workflow (`.github/workflows/e2e-multi-device-nightly.yml`)
 * seeds the required session-mode env knobs (OMNISIGHT_AUTH_MODE=session,
 * OMNISIGHT_ADMIN_PASSWORD=changeme123!) before invoking this config.
 *
 * Failures retain a full Playwright trace + screenshot for every failed
 * test so the operator can open the PR / nightly run artifact tab and
 * step through the two-context timeline frame by frame. The base
 * `playwright.config.ts` already sets `trace: "retain-on-failure"`; this
 * config mirrors that + bumps the HTML report so trace-link browsing
 * works directly from the uploaded artifact.
 */
import { defineConfig, devices } from "@playwright/test"

const BACKEND_PORT = Number(process.env.OMNISIGHT_E2E_BACKEND_PORT ?? "18830")
const FRONTEND_PORT = Number(process.env.OMNISIGHT_E2E_FRONTEND_PORT ?? "3100")

const pwLibDir = process.env.OMNISIGHT_PW_LIB_DIR
if (pwLibDir) {
  process.env.LD_LIBRARY_PATH = pwLibDir +
    (process.env.LD_LIBRARY_PATH ? ":" + process.env.LD_LIBRARY_PATH : "")
}

export default defineConfig({
  testDir: "./test/e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  // One retry in CI — the Q.2 new-device alert has a per-(user, subnet)
  // 24h dedup window (backend/auth.py::_new_device_alert_should_fire)
  // that a same-day rerun of the workflow can trip even when the code
  // is correct. A single retry + the trace-on-failure artifact lets the
  // operator disambiguate "real regression" from "dedup already fired"
  // without burning a second full pipeline slot.
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [
    ["list"],
    // HTML report is what the GH artifact tab unpacks — trace-viewer
    // deep links work directly from it. `never` keeps CI noise down.
    ["html", { outputFolder: "playwright-report-nightly", open: "never" }],
  ],
  timeout: 60_000,       // six scenarios × login + polling; give headroom
  expect: { timeout: 10_000 },

  use: {
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    viewport: { width: 1440, height: 900 },
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],

  webServer: [
    {
      command: `python3 -m uvicorn backend.main:app --host 127.0.0.1 --port ${BACKEND_PORT}`,
      env: {
        OMNISIGHT_EXTRA_CORS_ORIGINS: `http://127.0.0.1:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT}`,
        // Q.8 requires session-mode so the auth / session-revocation /
        // new-device scenarios actually exercise real state. The
        // workflow that drives this config also sets AUTH_MODE=session
        // at the env layer so the uvicorn process inherits it; we
        // forward it here explicitly too in case a dev wires their
        // own shell without exporting it.
        ...(process.env.OMNISIGHT_AUTH_MODE
          ? { OMNISIGHT_AUTH_MODE: process.env.OMNISIGHT_AUTH_MODE }
          : { OMNISIGHT_AUTH_MODE: "session" }),
        ...(process.env.OMNISIGHT_ADMIN_EMAIL
          ? { OMNISIGHT_ADMIN_EMAIL: process.env.OMNISIGHT_ADMIN_EMAIL }
          : { OMNISIGHT_ADMIN_EMAIL: "admin@omnisight.local" }),
        ...(process.env.OMNISIGHT_ADMIN_PASSWORD
          ? { OMNISIGHT_ADMIN_PASSWORD: process.env.OMNISIGHT_ADMIN_PASSWORD }
          : { OMNISIGHT_ADMIN_PASSWORD: "changeme123!" }),
        ...(process.env.OMNISIGHT_DATABASE_PATH
          ? { OMNISIGHT_DATABASE_PATH: process.env.OMNISIGHT_DATABASE_PATH }
          : {}),
      },
      port: BACKEND_PORT,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
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

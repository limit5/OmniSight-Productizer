/**
 * BS.11.6 — Performance budget for the Platforms catalog under heavy
 * motion. Drives the BS.11.5 fixture page at
 * `/e2e-fixtures/catalog-page` with `motion=dramatic` (full Layer 1 +
 * Layer 4 + Layer 6 + Layer 7 + Layer 9 BS.6.6 motion stack active)
 * and samples per-frame timestamps via an injected
 * `requestAnimationFrame` loop running for ~3.2 s. The pure helper at
 * `lib/perf/fps-budget.ts` translates the timestamp array into mean /
 * p10 / min fps and a pass/fail verdict.
 *
 * Budget (from `BS.11.6` row): mean fps ≥ 50 during heavy motion on a
 * mid-tier device. We model "mid-tier" with two scenarios:
 *
 *   1. `dramatic-desktop` — viewport 1440×900, no CPU throttle. This
 *      represents a healthy laptop. Strict: mean ≥ 50 fps and
 *      p10 ≥ 30 fps. Failure here means heavy motion has regressed
 *      below acceptable on hardware where it should be effortless.
 *   2. `dramatic-mid-tier` — viewport 1440×900 with CDP
 *      `Emulation.setCPUThrottlingRate({rate:4})` matching Lighthouse
 *      mobile preset. This is the operator-facing spec target. Strict
 *      assertion is OPT-IN behind `OMNISIGHT_BS11_6_PERF_STRICT=1`
 *      because CI runner CPU varies wildly — by default the spec
 *      records the measurement and warns below threshold without
 *      red-failing. The runbook at
 *      `docs/ops/bs11_6_perf_runbook.md` documents the manual
 *      Chrome DevTools profiler verification path that `[O]`-locks
 *      this row on real mid-tier hardware.
 *   3. `off-control` — viewport 1440×900, motion=off (R25.2 OS short-
 *      circuit + user-pref). Smoke-tests that the rAF sampler itself
 *      is healthy: when the page is static, fps should pin near
 *      vsync (≥55fps). If this scenario fails the entire spec is
 *      suspect — likely a sampler bug, not a motion-stack regression.
 *
 * Determinism levers (mirrors BS.11.5 — see header in
 * `bs11-5-catalog-visual.spec.ts`):
 *   • `Date.now()` frozen via `addInitScript` to a fixed epoch.
 *   • `/api/v1/auth/whoami` + `/auth/tenants` + `/events` stubbed.
 *   • `/api/v1/user-preferences/{motion_level,catalog_density}`
 *     stubbed to the requested motion + a stable `comfortable`
 *     density.
 *   • `page.emulateMedia({ reducedMotion: ... })` per scenario.
 *   • Catch-all `/api/v1/**` → 404 to ensure no live network leaks
 *     into the perf measurement.
 *
 * What we deliberately do NOT do (compared to BS.11.5):
 *   • No `disableRunningAnimations()` injection — the whole point of
 *     this spec is to measure the running animations.
 *   • No screenshot comparison — output is the FPS verdict + JSON
 *     report attached to the test result.
 *
 * Running locally:
 *
 *   OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.bs11-6.config.ts
 *
 * Strict mid-tier budget gate (operator runs on mid-tier hardware):
 *
 *   OMNISIGHT_BS11_6_PERF_STRICT=1 \
 *     OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.bs11-6.config.ts
 *
 * The JSON report lands in `test-results/bs11-6-catalog-perf-<...>/`
 * (gitignored) and is attached to the test as `bs11-6-fps.json` so
 * the report bundle ships it.
 */

import { test, expect, type Page, type CDPSession } from "@playwright/test"

import {
  BS11_6_DRAMATIC_MIN_MEAN_FPS,
  BS11_6_DRAMATIC_MIN_P10_FPS,
  BS11_6_OFF_MIN_MEAN_FPS,
  computeFpsStats,
  evaluateFpsVerdict,
  formatFpsSummary,
  type FpsVerdict,
} from "../lib/perf/fps-budget"

const FROZEN_NOW_MS = 1777887600000 // 2026-04-25T10:00:00Z
const SAMPLE_DURATION_MS = 3200
const SETTLE_DELAY_MS = 750
const MOTION_SETTLE_TIMEOUT_MS = 15_000

const STRICT = process.env.OMNISIGHT_BS11_6_PERF_STRICT === "1"

type MotionLevel = "off" | "normal" | "dramatic"

async function freezeClock(page: Page) {
  await page.addInitScript((frozenMs: number) => {
    const RealDate = Date
    const FrozenDate = class extends RealDate {
      constructor(...args: unknown[]) {
        if (args.length === 0) {
          super(frozenMs)
        } else {
          super(...(args as [number]))
        }
      }
      static now() {
        return frozenMs
      }
    }
    ;(globalThis as { Date: DateConstructor }).Date = FrozenDate as unknown as DateConstructor
  }, FROZEN_NOW_MS)
}

async function stubAuthRoutes(page: Page) {
  await page.route("**/api/v1/auth/whoami", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        user: {
          id: "fixture-admin",
          email: "admin@fixture.local",
          name: "Fixture Admin",
          role: "admin",
          enabled: true,
          tenant_id: "default",
        },
        auth_mode: "open",
        session_id: null,
      }),
    })
  })
  await page.route("**/api/v1/auth/tenants", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json" },
      body: "[]",
    })
  })
}

async function stubUserPreferences(page: Page, motionLevel: MotionLevel) {
  await page.route("**/api/v1/user-preferences/motion_level", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key: "motion_level", value: motionLevel }),
    })
  })
  await page.route("**/api/v1/user-preferences/catalog_density", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key: "catalog_density", value: "comfortable" }),
    })
  })
}

async function stubCatchAllRoutes(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    await route.fulfill({
      status: 404,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ detail: "not_found_fixture_stub" }),
    })
  })
  await page.route("**/api/v1/events", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: ":keepalive\n\n",
    })
  })
}

interface SamplePayload {
  timestamps: number[]
  startedAtMs: number
  endedAtMs: number
}

/**
 * Inject an rAF sampler that records `performance.now()` for every
 * paint over `durationMs` and resolves to the timestamp array. The
 * sampler runs in the page context so the timestamps reflect the
 * browser's actual frame schedule (modulo background-tab throttling
 * — Playwright keeps the page foregrounded, so vsync is the floor).
 */
async function sampleFps(page: Page, durationMs: number): Promise<SamplePayload> {
  return await page.evaluate(async (totalMs: number): Promise<SamplePayload> => {
    return await new Promise<SamplePayload>((resolve) => {
      const timestamps: number[] = []
      const startedAtMs = performance.now()
      const stopAt = startedAtMs + totalMs

      const tick = () => {
        const now = performance.now()
        timestamps.push(now)
        if (now >= stopAt) {
          resolve({
            timestamps,
            startedAtMs,
            endedAtMs: now,
          })
          return
        }
        requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
  }, durationMs)
}

interface OpenFixtureInput {
  motion: MotionLevel
}

async function openFixture(page: Page, { motion }: OpenFixtureInput) {
  await page.goto(`/e2e-fixtures/catalog-page?motion=${motion}&view=grid`)
  const root = page.getByTestId("bs11-5-fixture-root").first()
  await expect(root).toBeVisible({ timeout: 15_000 })
  await expect(root).toHaveAttribute("data-fixture-view", "grid")

  const tab = page.getByTestId("catalog-tab").first()
  await expect(tab).toBeVisible({ timeout: 10_000 })
  await expect(tab).toHaveAttribute("data-catalog-density", "comfortable", {
    timeout: 5_000,
  })

  // For BS.11.6 we MUST verify the requested motion level actually
  // applied — sampling fps under the wrong motion is meaningless.
  // BS.11.5's soft check is too lenient for a perf gate; we hard-
  // wait here on a longer timeout, and the test fails fast if the
  // resolver chain didn't land where we asked. The 15 s ceiling
  // covers the dev-server hydration race noted in BS.11.5 (SSR
  // commit ships default before user-pref API resolves).
  await expect(tab).toHaveAttribute("data-catalog-motion-level", motion, {
    timeout: MOTION_SETTLE_TIMEOUT_MS,
  })

  // Allow the BS.3 motion hooks (`useFloatingCard`, `useGlassReflection`,
  // `useScrollParallax`, etc.) to start their rAF subscriptions before
  // we begin sampling. Without this, the first ~10 frames are dominated
  // by hook-bootstrap setup and skew the mean upward.
  await page.waitForTimeout(SETTLE_DELAY_MS)
  return root
}

interface ScenarioReport {
  scenario: string
  motion: MotionLevel
  viewport: { width: number; height: number }
  cpuThrottlingRate: number
  sampleDurationMs: number
  startedAtMs: number
  endedAtMs: number
  verdict: FpsVerdict
}

function buildReport(input: {
  scenario: string
  motion: MotionLevel
  viewport: { width: number; height: number }
  cpuThrottlingRate: number
  sample: SamplePayload
  minMeanFps: number
  minP10Fps?: number
}): ScenarioReport {
  const stats = computeFpsStats(input.sample.timestamps)
  const verdict = evaluateFpsVerdict({
    scenario: input.scenario,
    minMeanFps: input.minMeanFps,
    minP10Fps: input.minP10Fps,
    stats,
  })
  return {
    scenario: input.scenario,
    motion: input.motion,
    viewport: input.viewport,
    cpuThrottlingRate: input.cpuThrottlingRate,
    sampleDurationMs: SAMPLE_DURATION_MS,
    startedAtMs: input.sample.startedAtMs,
    endedAtMs: input.sample.endedAtMs,
    verdict,
  }
}

async function attachReport(testInfo: import("@playwright/test").TestInfo, report: ScenarioReport) {
  await testInfo.attach(`bs11-6-${report.scenario}.json`, {
    body: JSON.stringify(report, null, 2),
    contentType: "application/json",
  })
}

async function setCpuThrottle(client: CDPSession | null, rate: number) {
  if (!client) return
  await client.send("Emulation.setCPUThrottlingRate", { rate })
}

test.describe("BS.11.6 — Platforms catalog FPS budget under heavy motion", () => {
  test.beforeEach(async ({ page }) => {
    await freezeClock(page)
    await stubAuthRoutes(page)
  })

  test("off-control · 1440×900 (no throttle) — sampler health smoke", async ({ page }, testInfo) => {
    await stubCatchAllRoutes(page)
    await stubUserPreferences(page, "off")
    await page.emulateMedia({ reducedMotion: "reduce" })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openFixture(page, { motion: "off" })

    const sample = await sampleFps(page, SAMPLE_DURATION_MS)
    const report = buildReport({
      scenario: "off-control",
      motion: "off",
      viewport: { width: 1440, height: 900 },
      cpuThrottlingRate: 1,
      sample,
      minMeanFps: BS11_6_OFF_MIN_MEAN_FPS,
    })
    await attachReport(testInfo, report)

    console.log(formatFpsSummary(report.verdict))

    // The sampler-health smoke is a hard assertion always — if it
    // fails, the rAF instrumentation itself is broken and every
    // dramatic-budget verdict below is suspect. Far rarer to fail
    // than the dramatic strict gate.
    expect.soft(report.verdict.passed, report.verdict.reasons.join("; ")).toBeTruthy()
    expect(report.verdict.stats.frameCount).toBeGreaterThanOrEqual(30)
  })

  test("dramatic-desktop · 1440×900 (no throttle) — strict 50fps gate", async ({ page }, testInfo) => {
    await stubCatchAllRoutes(page)
    await stubUserPreferences(page, "dramatic")
    await page.emulateMedia({ reducedMotion: "no-preference" })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openFixture(page, { motion: "dramatic" })

    const sample = await sampleFps(page, SAMPLE_DURATION_MS)
    const report = buildReport({
      scenario: "dramatic-desktop",
      motion: "dramatic",
      viewport: { width: 1440, height: 900 },
      cpuThrottlingRate: 1,
      sample,
      minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      minP10Fps: BS11_6_DRAMATIC_MIN_P10_FPS,
    })
    await attachReport(testInfo, report)

    console.log(formatFpsSummary(report.verdict))

    // Hard gate: full motion stack on healthy hardware MUST clear
    // 50 fps mean / 30 fps p10. Failure here is a real regression
    // in the BS.3 / BS.5 / BS.6 motion library or an unbatched rAF
    // hot path. The error message includes every threshold breach
    // so the operator sees both mean and p10 violations at once.
    expect(report.verdict.passed, report.verdict.reasons.join("; ")).toBeTruthy()
  })

  test("dramatic-mid-tier · 1440×900 (CPU throttle 4×) — Lighthouse mid-tier emulation", async ({
    page,
    context,
  }, testInfo) => {
    await stubCatchAllRoutes(page)
    await stubUserPreferences(page, "dramatic")
    await page.emulateMedia({ reducedMotion: "no-preference" })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openFixture(page, { motion: "dramatic" })

    // Apply Lighthouse mobile preset's 4× CPU slowdown via CDP. The
    // `Emulation.setCPUThrottlingRate` command throttles main-thread
    // JS execution by the requested factor. 4× matches the Moto-G4
    // class device the Lighthouse mobile preset targets — a fair
    // proxy for "mid-tier device" without requiring physical
    // hardware. We do this AFTER `openFixture()` so hydration
    // doesn't fight the throttle (otherwise the mount-time motion
    // settle window blows past the 15 s timeout on slow runners).
    let client: CDPSession | null = null
    try {
      client = await context.newCDPSession(page)
      await setCpuThrottle(client, 4)
      // Give the BS.3 hooks a fresh settle window under throttle —
      // the previous 750 ms covered no-throttle mount; under 4× we
      // need another 750 ms for hooks to stabilise their rAF
      // batching at the throttled cadence.
      await page.waitForTimeout(SETTLE_DELAY_MS)

      const sample = await sampleFps(page, SAMPLE_DURATION_MS)
      const report = buildReport({
        scenario: "dramatic-mid-tier",
        motion: "dramatic",
        viewport: { width: 1440, height: 900 },
        cpuThrottlingRate: 4,
        sample,
        minMeanFps: BS11_6_DRAMATIC_MIN_MEAN_FPS,
      })
      await attachReport(testInfo, report)

      console.log(formatFpsSummary(report.verdict))

      if (STRICT) {
        // Operator-run mode: enforce the BS.11.6 row's literal budget
        // (mean fps ≥ 50) under 4× CPU throttle. Failure means the
        // motion library doesn't meet the spec on a real mid-tier
        // device and the row should not flip to `[D]` until the
        // motion path is optimised.
        expect(report.verdict.passed, report.verdict.reasons.join("; ")).toBeTruthy()
      } else {
        // Default mode: emit the report + log a warning if below the
        // budget but do NOT fail the test. CI runner CPU varies too
        // widely for the 4× throttle floor to be a stable gate, and
        // the row's acceptance gate is the manual Chrome DevTools
        // profiler verification documented in the runbook.
        if (!report.verdict.passed) {

          console.warn(
            `[BS.11.6] mid-tier soft warning — ${report.verdict.reasons.join("; ")}\n` +
            `Set OMNISIGHT_BS11_6_PERF_STRICT=1 to enforce this gate (operator mid-tier-device runs).`,
          )
        }
        // Still assert sampler health so a bricked CDP throttle
        // doesn't silently produce 0 frames forever.
        expect(report.verdict.stats.frameCount).toBeGreaterThanOrEqual(30)
      }
    } finally {
      if (client) {
        try {
          await setCpuThrottle(client, 1)
        } catch {
          // ignore — CDP session may already be detached.
        }
        try {
          await client.detach()
        } catch {
          // ignore.
        }
      }
    }
  })
})

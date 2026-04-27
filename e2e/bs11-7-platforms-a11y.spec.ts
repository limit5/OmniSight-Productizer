/**
 * BS.11.7 — Lighthouse-equivalent accessibility audit for the
 * Platforms page. Drives the BS.11.5 fixture page at
 * `/e2e-fixtures/catalog-page` (the same deterministic
 * `<PlatformHero />` + `<CatalogTab />` + `<CatalogDetailPanel />`
 * surface the BS.11.5 visual matrix uses) and runs `axe-core` against
 * the rendered DOM, then translates the result into a Lighthouse-
 * style weighted-average score via `lib/a11y/lighthouse-score.ts`.
 *
 * Budget (from the BS.11.7 row): Lighthouse a11y score ≥ 90 on the
 * Platforms page. We verify:
 *
 *   1. `platforms-grid` — `?view=grid&motion=normal` (the canonical
 *      operator-facing view of the catalog tab).
 *   2. `platforms-grid-reduced-motion` — same view but with
 *      `motion=off` and `page.emulateMedia({reducedMotion:"reduce"})`.
 *      A11y rules around contrast / focus order should be unchanged
 *      between motion levels; the additional run guards against the
 *      reduce-motion path inadvertently injecting hidden / mis-
 *      labelled elements (e.g. an aria-hidden wrapper that swallows
 *      a focusable child).
 *   3. `platforms-detail` — `?view=detail&motion=normal` exercising
 *      the BS.6.3 `<CatalogDetailPanel />` surface alone. The detail
 *      panel ships its own `aria-live`, focus-restoration, and
 *      back-button autofocus contracts (BS.11.2 + BS.11.3); the
 *      audit pins them.
 *
 * Why we measure on the fixture page (not `/settings/platforms`):
 *   • The fixture is deterministic — same DOM every run. The real
 *     `/settings/platforms` page depends on `useEngine`, `useAuth`,
 *     and the SSE event stream which would change tab counters and
 *     status chips between runs.
 *   • The Platforms page's authentication shell is the same across
 *     all admin pages — covering it here would test the global
 *     layout, not the BS.11 epic's catalog surface. The runbook at
 *     `docs/ops/bs11_7_a11y_runbook.md` documents the manual `npx
 *     lhci collect` cross-check on the real page, which is the row's
 *     `[D]` flip gate.
 *   • The fixture renders the same `<PlatformHero />` + `<CatalogTab />`
 *     components the operator sees, so an a11y regression in those
 *     components surfaces here as cleanly as on the real page.
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
 *     into the audit.
 *
 * Running locally:
 *
 *   OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.bs11-7.config.ts
 *
 * The JSON reports land in `test-results/bs11-7-platforms-a11y-<...>/`
 * (gitignored) and are attached to each test as
 * `bs11-7-<scenario>.json` so the report bundle ships them.
 */

import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { test, expect, type Page } from "@playwright/test"

import {
  BS11_7_PLATFORMS_MIN_A11Y_SCORE,
  computeA11yScore,
  formatA11ySummary,
  type A11yScoreVerdict,
  type AxeRunSummary,
} from "../lib/a11y/lighthouse-score"

const FROZEN_NOW_MS = 1777887600000 // 2026-04-25T10:00:00Z

const AXE_SOURCE = readFileSync(
  resolve(__dirname, "..", "node_modules", "axe-core", "axe.min.js"),
  "utf8",
)

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

interface OpenFixtureInput {
  motion: MotionLevel
  view: "grid" | "detail"
}

async function openFixture(page: Page, { motion, view }: OpenFixtureInput) {
  await page.goto(`/e2e-fixtures/catalog-page?motion=${motion}&view=${view}`)
  const root = page.getByTestId("bs11-5-fixture-root").first()
  await expect(root).toBeVisible({ timeout: 15_000 })
  await expect(root).toHaveAttribute("data-fixture-view", view)
  if (view === "grid") {
    const tab = page.getByTestId("catalog-tab").first()
    await expect(tab).toBeVisible({ timeout: 10_000 })
    await expect(tab).toHaveAttribute("data-catalog-density", "comfortable", {
      timeout: 5_000,
    })
  } else {
    const detailSection = page.getByTestId("bs11-5-fixture-detail-section").first()
    await expect(detailSection).toBeVisible({ timeout: 10_000 })
  }
  return root
}

/**
 * Inject axe-core into the page and run an audit on the entire
 * document, returning the subset of `axe.AxeResults` we score on.
 *
 * Lighthouse runs axe with the `wcag2a`, `wcag2aa`, `wcag21a`,
 * `wcag21aa`, `best-practice` tags by default. Mirroring that here
 * keeps our weighted score comparable to a Lighthouse-CLI run on the
 * same DOM. The `reporter: "v2"` setting matches Lighthouse and emits
 * the `nodes[].failureSummary` strings — useful for human review of
 * the JSON report when an audit fails.
 */
async function runAxeOnPage(page: Page): Promise<AxeRunSummary> {
  await page.evaluate((axeSrc: string) => {
    if (!(window as unknown as { axe?: unknown }).axe) {
      // Inject axe-core's UMD bundle by evaluating its source. We
      // deliberately avoid `addScriptTag({content})` here because
      // CSP on dev-server pages can sometimes block inline scripts;
      // `eval()` runs in the page context with the same origin and
      // sidesteps the policy.
      ;(0, eval)(axeSrc)
    }
  }, AXE_SOURCE)

  const result = (await page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: (root: Document, opts: unknown) => Promise<unknown> } }).axe
    return await axe.run(document, {
      reporter: "v2",
      runOnly: {
        type: "tag",
        values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"],
      },
    })
  })) as Partial<AxeRunSummary>

  return {
    violations: result.violations ?? [],
    passes: result.passes ?? [],
    incomplete: result.incomplete ?? [],
    inapplicable: result.inapplicable ?? [],
  }
}

interface ScenarioReport {
  scenario: string
  view: "grid" | "detail"
  motion: MotionLevel
  reducedMotionMedia: "reduce" | "no-preference"
  threshold: number
  verdict: A11yScoreVerdict
  /** Top-level violations with up to 5 example node selectors each
   *  for human review. Failures get the full set; passes attach the
   *  count summary only to keep the JSON report compact. */
  violationDetails: Array<{
    id: string
    impact: string | null
    weight: number
    nodes: Array<{ target: string[]; failureSummary?: string }>
  }>
}

function buildReport(input: {
  scenario: string
  view: "grid" | "detail"
  motion: MotionLevel
  reducedMotionMedia: "reduce" | "no-preference"
  threshold: number
  summary: AxeRunSummary
}): ScenarioReport {
  const verdict = computeA11yScore({
    summary: input.summary,
    threshold: input.threshold,
  })
  const violationDetails: ScenarioReport["violationDetails"] = []
  for (const v of input.summary.violations) {
    const rawNodes = v.nodes as ReadonlyArray<{ target?: unknown; failureSummary?: unknown }>
    const nodes = rawNodes.slice(0, 5).map((n) => ({
      target: Array.isArray(n.target) ? n.target.map((t: unknown) => String(t)) : [],
      failureSummary: typeof n.failureSummary === "string" ? n.failureSummary : undefined,
    }))
    violationDetails.push({
      id: v.id,
      impact: v.impact ?? null,
      weight: verdict.violations.find((d) => d.id === v.id)?.weight ?? 1,
      nodes,
    })
  }
  return {
    scenario: input.scenario,
    view: input.view,
    motion: input.motion,
    reducedMotionMedia: input.reducedMotionMedia,
    threshold: input.threshold,
    verdict,
    violationDetails,
  }
}

async function attachReport(testInfo: import("@playwright/test").TestInfo, report: ScenarioReport) {
  await testInfo.attach(`bs11-7-${report.scenario}.json`, {
    body: JSON.stringify(report, null, 2),
    contentType: "application/json",
  })
}

test.describe("BS.11.7 — Platforms page Lighthouse-equivalent a11y audit", () => {
  test.beforeEach(async ({ page }) => {
    await freezeClock(page)
    await stubAuthRoutes(page)
  })

  test("platforms-grid · motion=normal — Lighthouse a11y ≥ 90", async ({ page }, testInfo) => {
    await stubCatchAllRoutes(page)
    await stubUserPreferences(page, "normal")
    await page.emulateMedia({ reducedMotion: "no-preference" })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openFixture(page, { motion: "normal", view: "grid" })

    const summary = await runAxeOnPage(page)
    const report = buildReport({
      scenario: "platforms-grid",
      view: "grid",
      motion: "normal",
      reducedMotionMedia: "no-preference",
      threshold: BS11_7_PLATFORMS_MIN_A11Y_SCORE,
      summary,
    })
    await attachReport(testInfo, report)

    console.log(formatA11ySummary(report.scenario, report.verdict))

    expect(report.verdict.passed, report.verdict.reasons.join("; ")).toBeTruthy()
  })

  test("platforms-grid-reduced-motion · motion=off — Lighthouse a11y ≥ 90", async ({ page }, testInfo) => {
    await stubCatchAllRoutes(page)
    await stubUserPreferences(page, "off")
    await page.emulateMedia({ reducedMotion: "reduce" })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openFixture(page, { motion: "off", view: "grid" })

    const summary = await runAxeOnPage(page)
    const report = buildReport({
      scenario: "platforms-grid-reduced-motion",
      view: "grid",
      motion: "off",
      reducedMotionMedia: "reduce",
      threshold: BS11_7_PLATFORMS_MIN_A11Y_SCORE,
      summary,
    })
    await attachReport(testInfo, report)

    console.log(formatA11ySummary(report.scenario, report.verdict))

    expect(report.verdict.passed, report.verdict.reasons.join("; ")).toBeTruthy()
  })

  test("platforms-detail · motion=normal — Lighthouse a11y ≥ 90", async ({ page }, testInfo) => {
    await stubCatchAllRoutes(page)
    await stubUserPreferences(page, "normal")
    await page.emulateMedia({ reducedMotion: "no-preference" })
    await page.setViewportSize({ width: 1440, height: 900 })
    await openFixture(page, { motion: "normal", view: "detail" })

    const summary = await runAxeOnPage(page)
    const report = buildReport({
      scenario: "platforms-detail",
      view: "detail",
      motion: "normal",
      reducedMotionMedia: "no-preference",
      threshold: BS11_7_PLATFORMS_MIN_A11Y_SCORE,
      summary,
    })
    await attachReport(testInfo, report)

    console.log(formatA11ySummary(report.scenario, report.verdict))

    expect(report.verdict.passed, report.verdict.reasons.join("; ")).toBeTruthy()
  })
})

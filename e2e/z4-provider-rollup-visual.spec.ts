/**
 * Z.4 #293 checkbox 7 — Playwright visual regression for the per-provider
 * roll-up (<ProviderRollup> + <ProviderStatusBadge> + <ProviderCardExpansion>).
 *
 * Strategy: the spec navigates to the dedicated fixture page at
 * `app/e2e-fixtures/provider-rollup/page.tsx` (a thin render of just the
 * three components under test). The fixture page mounts no hooks that
 * fetch from the backend, so the entire app shell — useEngine, SSE,
 * dashboard summary, decision queue — is bypassed. The visual test is
 * therefore a test of rendering, not of the dashboard's data pipeline.
 *
 * Three scenarios are driven by `?scenario=`:
 *
 *   1. fully-configured  — 9 provider balance envelopes with healthy
 *                          balance + granted totals, status="ok", green tier.
 *   2. all-empty         — usage rows exist so the groups still render,
 *                          but no balance envelopes → every badge falls
 *                          through to the gray "no data" tier and every
 *                          expansion block is omitted (`null` return).
 *   3. mixed             — one envelope per tier: green, yellow, red,
 *                          unsupported, error, plus one provider missing
 *                          from the envelope array (loading fallback).
 *
 * Determinism: `Date.now()` is frozen to a fixed epoch via
 * `page.addInitScript`, matching the `FROZEN_NOW_SEC` constant in the
 * fixture page so `formatLastSynced()` renders stable "N s ago" labels.
 *
 * Screenshots are emitted to `test-results/z4-provider-rollup/` (already
 * gitignored). They are artifacts for human review — this spec does
 * NOT use `toHaveScreenshot()` because the repo has no existing snapshot
 * baseline in git and managing binary pixel diffs across CI runners is
 * out of scope for this checkbox. The test still fails on genuine
 * regression because each scenario carries DOM assertions locking the
 * rollup shape (group count, data-provider-key attributes, expanded
 * state, badge tier, balance text), so breakage in the rendering path
 * will red-fail the CI job without a pixel baseline.
 *
 * Running locally:
 *
 *   OMNISIGHT_PW_LIB_DIR=/path/to/nss-libs \
 *     pnpm exec playwright test --config=playwright.visual.config.ts
 */

import { test, expect, type Page } from "@playwright/test"

const FROZEN_NOW_MS = 1777887600000  // 2026-04-25T10:00:00Z

async function prepPage(page: Page) {
  // Freeze Date so formatLastSynced renders stable labels.
  await page.addInitScript((frozenMs: number) => {
    const RealDate = Date
    const FrozenDate = class extends RealDate {
      constructor(...args: unknown[]) {
        if (args.length === 0) {
          super(frozenMs)
        } else {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          super(...(args as [any]))
        }
      }
      static now() { return frozenMs }
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(globalThis as any).Date = FrozenDate as unknown as DateConstructor
  }, FROZEN_NOW_MS)

  // AuthProvider in the root layout calls /api/v1/auth/whoami on mount —
  // stub it so it doesn't spam the test with network errors, even though
  // the fixture page itself never reads the auth context.
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
  // Tenant list — empty is fine for the header dropdown.
  await page.route("**/api/v1/auth/tenants", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "application/json" },
      body: "[]",
    })
  })
  // Any other /api/v1/* route: 404 so the FE's request() helper rejects
  // cleanly and subscribers fall through to their error branches. We
  // don't want `{}` defaults because components looking for arrays would
  // then hit `.map is not a function` in an unrelated part of the tree.
  await page.route("**/api/v1/**", async (route) => {
    await route.fulfill({
      status: 404,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ detail: "not_found_fixture_stub" }),
    })
  })
  // Same for the SSE endpoint, but a text/event-stream with a single
  // keepalive so the browser's reconnect loop stays idle.
  await page.route("**/api/v1/events", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: ":keepalive\n\n",
    })
  })
}

async function openFixture(page: Page, scenario: string) {
  await page.goto(`/e2e-fixtures/provider-rollup?scenario=${scenario}`)
  // React 19 + Next 16 sometimes double-renders the root on initial
  // paint (SSR placeholder + hydrated client copy); `.first()` is
  // deliberate — the two elements are identical, we just pick one.
  const root = page.getByTestId("e2e-fixture-root").first()
  await expect(root).toBeVisible({ timeout: 15_000 })
  await expect(root).toHaveAttribute("data-scenario", scenario)
  const rollup = page.getByTestId("provider-rollup").first()
  await expect(rollup).toBeVisible({ timeout: 10_000 })
  return rollup
}

test.describe("Z.4 #293 — ProviderRollup visual regression", () => {
  test.beforeEach(async ({ page }) => {
    await prepPage(page)
  })

  test("fully-configured — 9 provider envelopes, all green", async ({ page }, testInfo) => {
    const rollup = await openFixture(page, "fully-configured")

    // 7 of the 9 registry providers render: Groq + Together have no
    // AI_MODEL_INFO resolver path so their usage rows (and balance
    // envelopes) have no home in the rollup. Locked by the fixture's
    // buildRows() — if someone adds Groq / Together to AI_MODEL_INFO in
    // the future this count flips to 8 or 9 and the spec must be
    // updated alongside.
    const groups = rollup.locator('[data-testid^="provider-rollup-group-"]')
    await expect(groups).toHaveCount(7)

    // Every rendered group key matches the lowercase backend registry.
    for (const key of ["anthropic", "google", "openai", "xai", "deepseek", "openrouter", "local"]) {
      await expect(rollup.locator(`[data-provider-key="${key}"]`)).toBeVisible()
    }

    // Expansion renders for every GROUP whose providerKey matches a
    // balance envelope provider. We have 7 groups but only 6 match: the
    // `local` group (from AI_MODEL_INFO's "Local" label for the Ollama
    // model) does NOT match the backend's "ollama" envelope key. This
    // is the documented Ollama-label drift from Z.4 checkbox 5.
    await expect(rollup.getByTestId("provider-card-expansion")).toHaveCount(6)

    // The `local` group deliberately has no expansion — confirming the
    // documented mismatch is still in play.
    const localGroup = rollup.locator('[data-provider-key="local"]')
    await expect(localGroup).toBeVisible()
    await expect(localGroup.getByTestId("provider-card-expansion")).toHaveCount(0)

    // Anthropic's balance value carries the full "$X / $Y" format.
    const anthropic = rollup.locator('[data-provider-key="anthropic"]')
    await expect(
      anthropic.getByTestId("provider-card-expansion-balance-value"),
    ).toContainText("$85.00 / $100.00")

    // DeepSeek uses the CNY ¥ symbol.
    const deepseek = rollup.locator('[data-provider-key="deepseek"]')
    await expect(
      deepseek.getByTestId("provider-card-expansion-balance-value"),
    ).toContainText("¥")

    await rollup.screenshot({
      path: testInfo.outputPath("rollup-fully-configured.png"),
    })
    await page.screenshot({
      path: testInfo.outputPath("page-fully-configured.png"),
      fullPage: true,
    })
  })

  test("all-empty — usage rows exist but no balance data", async ({ page }, testInfo) => {
    const rollup = await openFixture(page, "all-empty")

    const groups = rollup.locator('[data-testid^="provider-rollup-group-"]')
    await expect(groups).toHaveCount(7)

    // No expansion should be painted: renderExpansion returns null
    // whenever the envelope is absent.
    await expect(rollup.getByTestId("provider-card-expansion")).toHaveCount(0)

    // Status slot mounts for every group — badge renders gray "no data".
    for (const key of ["anthropic", "google", "openai", "xai", "deepseek", "openrouter", "local"]) {
      await expect(
        rollup.locator(`[data-testid="provider-rollup-status-slot-${key}"]`),
      ).toBeVisible()
    }

    await rollup.screenshot({
      path: testInfo.outputPath("rollup-all-empty.png"),
    })
    await page.screenshot({
      path: testInfo.outputPath("page-all-empty.png"),
      fullPage: true,
    })
  })

  test("mixed — green + yellow + red + unsupported + error + loading", async ({ page }, testInfo) => {
    const rollup = await openFixture(page, "mixed")

    const groups = rollup.locator('[data-testid^="provider-rollup-group-"]')
    await expect(groups).toHaveCount(7)

    // Green: Anthropic got a healthy envelope ($72 / $100).
    const anthropicGroup = rollup.locator('[data-provider-key="anthropic"]')
    await expect(
      anthropicGroup.getByTestId("provider-card-expansion-balance-value"),
    ).toContainText("$72.00")

    // Yellow: OpenAI at 18% remaining → expansion paints normally.
    const openaiGroup = rollup.locator('[data-provider-key="openai"]')
    await expect(
      openaiGroup.getByTestId("provider-card-expansion-balance-value"),
    ).toContainText("$18.00")

    // Red: DeepSeek at 3%, CNY currency → ¥ symbol in expansion.
    const deepseekGroup = rollup.locator('[data-provider-key="deepseek"]')
    await expect(
      deepseekGroup.getByTestId("provider-card-expansion-balance-value"),
    ).toContainText("¥")

    // Unsupported: xAI envelope has status="unsupported" → expansion
    // short-circuits to the advisory message instead of the three rows.
    const xaiGroup = rollup.locator('[data-provider-key="xai"]')
    await expect(
      xaiGroup.getByTestId("provider-card-expansion-unsupported-message"),
    ).toBeVisible()

    // Error: OpenRouter envelope has status="error" → expansion adds the
    // error-message paragraph on top of the three standard rows.
    const openrouterGroup = rollup.locator('[data-provider-key="openrouter"]')
    await expect(
      openrouterGroup.getByTestId("provider-card-expansion-error-message"),
    ).toContainText(/401|Unauthorized/)

    // Loading: Google has no envelope → renderExpansion returns null,
    // but the summary-row status slot still mounts the loading badge.
    const googleGroup = rollup.locator('[data-provider-key="google"]')
    await expect(googleGroup).toBeVisible()
    await expect(
      googleGroup.locator('[data-testid="provider-rollup-status-slot-google"]'),
    ).toBeVisible()
    await expect(googleGroup.getByTestId("provider-card-expansion")).toHaveCount(0)

    await rollup.screenshot({
      path: testInfo.outputPath("rollup-mixed.png"),
    })
    await page.screenshot({
      path: testInfo.outputPath("page-mixed.png"),
      fullPage: true,
    })
  })
})

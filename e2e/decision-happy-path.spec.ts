/**
 * Phase 49E — Browser-level happy path for the Autonomous Decision
 * Engine surface (Phase 47-48). Runs against the live Next.js dev
 * server + FastAPI backend started by playwright.config webServer.
 *
 * Scope (intentionally thin):
 *   1. App loads, ModeSelector is visible with the current mode.
 *   2. Switching the mode via UI updates UI + backend round-trip.
 *   3. Decision Dashboard + Budget Panel are mounted.
 *   4. Switching budget strategy via UI round-trips to the backend.
 *   5. Sweep button works (empty result is fine).
 */

import { test, expect } from "@playwright/test"

const BACKEND_PORT = Number(process.env.OMNISIGHT_E2E_BACKEND_PORT ?? "18830")

test.describe("Autonomous Decision Engine — happy path", () => {
  test.beforeEach(async ({ page, request }) => {
    // Reset backend state so test ordering doesn't matter.
    await request.put(`http://127.0.0.1:${BACKEND_PORT}/api/v1/operation-mode`, {
      data: { mode: "supervised" },
    })
    await request.put(`http://127.0.0.1:${BACKEND_PORT}/api/v1/budget-strategy`, {
      data: { strategy: "balanced" },
    })
    await page.goto("/")
  })

  test("home page mounts all three decision panels", async ({ page }) => {
    // Wait for the ModeSelector hydration to complete — its radios appear
    // once the initial GET /operation-mode settles. Accessible name on
    // the radiogroup is "MODE" (aria-labelledby points at the "MODE"
    // label span in the header).
    await expect(page.getByRole("radio", { name: "SUPERVISED" })).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole("radiogroup", { name: "MODE" })).toBeVisible()
    // DecisionDashboard header
    await expect(page.getByRole("heading", { name: "DECISION QUEUE" })).toBeVisible()
    // BudgetStrategyPanel header
    await expect(page.getByRole("heading", { name: "BUDGET STRATEGY" })).toBeVisible()
  })

  test("switching operation mode round-trips to the backend", async ({ page, request }) => {
    // Wait for hydration
    await expect(page.getByRole("radio", { name: "SUPERVISED" }))
      .toHaveAttribute("aria-checked", "true", { timeout: 10_000 })

    // Header renders two ModeSelectors (mobile `md:hidden` + desktop
    // `hidden md:flex`), both with the same accessible-name radios. At
    // 1440×900 only the desktop one is visible; filter accordingly so
    // the click lands on the painted element.
    const fullAuto = page
      .getByRole("radio", { name: "FULL AUTO", exact: true })
      .filter({ visible: true })
    await fullAuto.click()

    // Assert backend state first — this is the durable contract and is
    // immune to turbopack React re-render jitter.
    await expect.poll(
      async () => (await (await request.get(
        `http://127.0.0.1:${BACKEND_PORT}/api/v1/operation-mode`)).json()).mode,
      { timeout: 10_000 },
    ).toBe("full_auto")

    // And the UI does eventually reflect it.
    await expect(fullAuto).toHaveAttribute("aria-checked", "true", { timeout: 10_000 })

    const res = await request.get(`http://127.0.0.1:${BACKEND_PORT}/api/v1/operation-mode`)
    expect((await res.json()).parallel_cap).toBe(4)
  })

  test("switching budget strategy round-trips to the backend", async ({ page, request }) => {
    // Wait for BudgetStrategyPanel to hydrate — balanced radio is checked
    // by default once the initial GET /budget-strategy resolves.
    await expect(page.getByRole("radio", { name: /BALANCED/ }))
      .toHaveAttribute("aria-checked", "true", { timeout: 10_000 })

    // BudgetStrategyPanel renders once (outside the dual-mobile header),
    // but keep the visible filter defensive in case future layouts add
    // a compact twin.
    const costSaver = page
      .getByRole("radio", { name: /COST SAVER/ })
      .filter({ visible: true })
    await costSaver.click()

    await expect.poll(
      async () => (await (await request.get(
        `http://127.0.0.1:${BACKEND_PORT}/api/v1/budget-strategy`)).json()).strategy,
      { timeout: 10_000 },
    ).toBe("cost_saver")

    await expect(costSaver).toHaveAttribute("aria-checked", "true", { timeout: 10_000 })

    const res = await request.get(`http://127.0.0.1:${BACKEND_PORT}/api/v1/budget-strategy`)
    const body = await res.json()
    expect(body.tuning.max_retries).toBe(1)
    expect(body.tuning.model_tier).toBe("budget")
  })

  test("sweep button completes even when the queue is empty", async ({ page }) => {
    const sweep = page.getByRole("button", { name: /^SWEEP$/ })
    await expect(sweep).toBeVisible()
    await sweep.click()
    // Returns quickly; the button label may flicker to SWEEP… and back.
    // Just wait until it's re-enabled before leaving the test.
    await expect(sweep).toBeEnabled()
  })

  test("SSE stream delivers mode_changed event to the browser", async ({ page, request }) => {
    // Real SSE round-trip: open an EventSource in the browser, trigger a
    // mode change on the backend, and assert the browser receives the
    // corresponding mode_changed event through the Next.js rewrite proxy.
    // This is the genuine SSE contract Phase 47-48 added; the earlier
    // version of this test asserted nothing SSE-specific.
    // Connect EventSource directly to the backend rather than through
    // the Next.js dev-server rewrite: turbopack buffers SSE responses
    // and eats events in dev. CORS for :FRONTEND_PORT is whitelisted in
    // playwright.config.ts via OMNISIGHT_EXTRA_CORS_ORIGINS.
    const received = await page.evaluate(async (backendPort) => {
      return await new Promise<{ event: string; data: unknown } | null>((resolve) => {
        const es = new EventSource(`http://127.0.0.1:${backendPort}/api/v1/events`)
        const timer = setTimeout(() => { es.close(); resolve(null) }, 8000)
        es.addEventListener("mode_changed", (ev: MessageEvent) => {
          clearTimeout(timer)
          const data = JSON.parse(ev.data)
          es.close()
          resolve({ event: "mode_changed", data })
        })
        // Only fire the PUT once the EventSource is OPEN — otherwise a
        // fast local backend can publish before the subscriber attaches.
        es.onopen = () => {
          fetch(`http://127.0.0.1:${backendPort}/api/v1/operation-mode`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode: "turbo" }),
          })
        }
      })
    }, BACKEND_PORT)
    expect(received).not.toBeNull()
    expect(received!.event).toBe("mode_changed")
    const payload = received!.data as { mode: string }
    expect(payload.mode).toBe("turbo")

    // Reset so next test's beforeEach isn't the only thing restoring state.
    await request.put(`http://127.0.0.1:${BACKEND_PORT}/api/v1/operation-mode`, {
      data: { mode: "supervised" },
    })
  })
})

/**
 * H3 row 1529 — Playwright E2E for the Host Load Panel + Coordinator
 * decision-transparency surface (rows 1521-1528).
 *
 * Validates the live wire-up that JSDOM tests cannot exercise:
 *   • host.metrics.tick SSE flowing from FastAPI through the Next.js
 *     rewrite proxy to the in-browser EventSource
 *   • /api/v1/ops/summary HTTP polling from the OpsSummaryPanel
 *   • /api/v1/coordinator/force-turbo POST round-trip on confirm
 *
 * SSE: the backend's host_metrics.run_host_sampling_loop fires every
 * SAMPLE_INTERVAL_S (5s, with cpu_interval=1.0 inside), so a fresh page
 * load should see at least one tick land within ~15s. The pill flipping
 * from "SSE WAITING" to "SSE LIVE" is the assertion.
 *
 * /ops/summary + /coordinator/force-turbo: intercepted with page.route()
 * so the spec doesn't depend on the backend actually being in a derated
 * state. The interception runs at the BROWSER → Next.js boundary, so the
 * Next.js rewrite proxy never sees the request — exactly the contract
 * we want to lock.
 */

import { test, expect, type Page } from "@playwright/test"

interface CoordinatorSnap {
  capacity_max: number
  effective_budget: number
  queue_depth: number
  deferred_5m: number
  derated: boolean
  derate_reason: string | null
}

interface OpsSummaryPayload {
  checked_at: number
  uptime_s: number | null
  daily_cost_usd: number
  hourly_cost_usd: number
  token_frozen: boolean
  budget_level: string
  decisions_pending: number
  sse_subscribers: number
  watchdog_age_s: number | null
  coordinator?: CoordinatorSnap | null
}

const baseOpsPayload: OpsSummaryPayload = {
  checked_at: 1700000000,
  uptime_s: 120,
  daily_cost_usd: 0.12,
  hourly_cost_usd: 0.005,
  token_frozen: false,
  budget_level: "normal",
  decisions_pending: 0,
  sse_subscribers: 1,
  watchdog_age_s: 15,
}

async function stubOpsSummary(page: Page, snap: CoordinatorSnap | null) {
  await page.route("**/api/v1/ops/summary", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...baseOpsPayload,
        coordinator: snap,
      }),
    })
  })
}

test.describe("H3 — Host Load Panel + Coordinator transparency", () => {
  test("HostDevicePanel SYSTEM INFO mounts with BASELINE pill at 16c / 64GB / 512GB", async ({ page }) => {
    // Stub ops-summary so the DerateBadge / Force turbo button noise
    // doesn't affect this test's assertions on the host panel.
    await stubOpsSummary(page, null)
    await page.goto("/")

    // Desktop layout is the default at the configured 1440×900 viewport;
    // the SYSTEM INFO card lives in the far-left aside.
    const systemInfo = page.getByTestId("system-info")
    await expect(systemInfo).toBeVisible({ timeout: 10_000 })

    // BASELINE pill is hardcoded — must show 16c / 64GB / 512GB regardless
    // of what the host actually advertises (downstream H4a math assumes
    // this reference rig).
    const baseline = page.getByTestId("host-baseline")
    await expect(baseline).toBeVisible()
    const baselineText = ((await baseline.textContent()) ?? "").replace(/\s+/g, " ")
    expect(baselineText).toContain("BASELINE 16c / 64GB / 512GB")

    // SSE status pill exists; it starts "WAITING" and flips to "LIVE"
    // once a host.metrics.tick lands.
    await expect(page.getByTestId("host-sse-status")).toBeVisible()
  })

  test("host.metrics.tick SSE flips host-sse-status to SSE LIVE within 15s", async ({ page }) => {
    await stubOpsSummary(page, null)
    await page.goto("/")
    // Backend sampling loop fires every 5s (with a 1s cpu_interval blocking
    // sample), so first tick should arrive within ~6s. Generous 15s budget
    // accommodates first-page hydration + EventSource handshake.
    await expect(page.getByTestId("host-sse-status")).toContainText("SSE LIVE", { timeout: 15_000 })
  })

  test("Once SSE is LIVE the five live-metric cards render with pressure attribute", async ({ page }) => {
    await stubOpsSummary(page, null)
    await page.goto("/")
    await expect(page.getByTestId("host-sse-status")).toContainText("SSE LIVE", { timeout: 15_000 })
    // All five live-metric cards must render. They each carry their own
    // testid; the three percent cards (cpu/mem/disk) additionally carry
    // a data-pressure attribute reflecting the H3 traffic-light band.
    for (const id of ["metric-cpu", "metric-mem", "metric-disk", "metric-loadavg", "metric-containers"]) {
      await expect(page.getByTestId(id)).toBeVisible()
    }
    for (const id of ["metric-cpu", "metric-mem", "metric-disk"]) {
      const pressure = await page.getByTestId(id).getAttribute("data-pressure")
      expect(["normal", "warn", "critical"]).toContain(pressure)
    }
  })

  test("OpsSummaryPanel COORDINATOR section renders with EFF BUDGET / QUEUE / DEFERRED tiles", async ({ page }) => {
    await stubOpsSummary(page, {
      capacity_max: 12,
      effective_budget: 12,
      queue_depth: 2,
      deferred_5m: 5,
      derated: false,
      derate_reason: null,
    })
    await page.goto("/")
    await expect(page.getByTestId("ops-coordinator-section")).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText("COORDINATOR", { exact: true })).toBeVisible()
    await expect(page.getByText("QUEUE", { exact: true })).toBeVisible()
    await expect(page.getByText("DEFERRED 5m", { exact: true })).toBeVisible()
    await expect(page.getByText("EFF BUDGET", { exact: true })).toBeVisible()
    await expect(page.getByTestId("ops-eff-budget")).toContainText("12/12")
    // Healthy snapshot — no derate badge.
    await expect(page.getByTestId("ops-derate-badge")).toHaveCount(0)
  })

  test("Auto-derate badge renders with target mode + reason tooltip when derated", async ({ page }) => {
    await stubOpsSummary(page, {
      capacity_max: 12,
      effective_budget: 4, // ratio ≈ 0.33 → "supervised" rung
      queue_depth: 6,
      deferred_5m: 22,
      derated: true,
      derate_reason: "CPU 92% > threshold",
    })
    await page.goto("/")
    const badge = page.getByTestId("ops-derate-badge")
    await expect(badge).toBeVisible({ timeout: 10_000 })
    await expect(badge).toHaveAttribute("data-derate-target", "supervised")
    await expect(badge).toContainText("Coordinator auto-derated to supervised")
    const title = (await badge.getAttribute("title")) ?? ""
    expect(title).toContain("CPU 92%")
    expect(title).toContain("effective 4 / 12 tokens")
    // Effective budget tile mirrors the derate ratio.
    await expect(page.getByTestId("ops-eff-budget")).toContainText("4/12")
  })

  test("Force turbo button is visible + carries the OOM warning a11y label", async ({ page }) => {
    await stubOpsSummary(page, {
      capacity_max: 12,
      effective_budget: 4,
      queue_depth: 1,
      deferred_5m: 3,
      derated: true,
      derate_reason: "MEM 90% > threshold",
    })
    await page.goto("/")
    const btn = page.getByTestId("ops-force-turbo-btn")
    await expect(btn).toBeVisible({ timeout: 10_000 })
    await expect(btn).toContainText("Force turbo")
    const aria = (await btn.getAttribute("aria-label")) ?? ""
    const tooltip = (await btn.getAttribute("title")) ?? ""
    expect(aria).toContain("OOM")
    expect(tooltip).toContain("OOM")
  })

  test("Force turbo: cancelling the confirm dialog never POSTs the override", async ({ page }) => {
    await stubOpsSummary(page, {
      capacity_max: 12,
      effective_budget: 4,
      queue_depth: 0,
      deferred_5m: 0,
      derated: true,
      derate_reason: "CPU 88% > threshold",
    })
    let postCount = 0
    await page.route("**/api/v1/coordinator/force-turbo", async (route) => {
      postCount += 1
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" })
    })

    // Reject the next window.confirm call. dialog event is the modern
    // Playwright way; fall back to overriding window.confirm so the
    // component (which uses window.confirm directly, not native dialog)
    // sees a falsy return.
    await page.addInitScript(() => {
      ;(window as Window & typeof globalThis).confirm = () => false
    })
    await page.goto("/")

    const btn = page.getByTestId("ops-force-turbo-btn")
    await expect(btn).toBeVisible({ timeout: 10_000 })
    await btn.click()
    // Give the click a moment to either dispatch the POST or not.
    await page.waitForTimeout(300)
    expect(postCount).toBe(0)
    // No success / error message should appear.
    await expect(page.getByTestId("ops-force-turbo-msg")).toHaveCount(0)
  })

  test("Force turbo: accepting the confirm POSTs with confirm=true and surfaces success message", async ({ page }) => {
    await stubOpsSummary(page, {
      capacity_max: 12,
      effective_budget: 4,
      queue_depth: 0,
      deferred_5m: 0,
      derated: true,
      derate_reason: "CPU 88% > threshold",
    })
    let capturedBody: unknown = null
    await page.route("**/api/v1/coordinator/force-turbo", async (route) => {
      const req = route.request()
      try {
        capturedBody = req.postDataJSON()
      } catch {
        capturedBody = req.postData()
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          applied: true,
          cleared_turbo_derate: true,
          reset_capacity_derate: true,
          before: { turbo_derate_active: true, capacity_derate_ratio: 0.33 },
          after: {
            turbo_derate_active: false,
            capacity_derate_ratio: 1.0,
            restored_to_budget: 12,
            manual_override: true,
            at: 1700000001,
          },
        }),
      })
    })

    // Accept the next window.confirm call.
    await page.addInitScript(() => {
      ;(window as Window & typeof globalThis).confirm = () => true
    })
    await page.goto("/")

    const btn = page.getByTestId("ops-force-turbo-btn")
    await expect(btn).toBeVisible({ timeout: 10_000 })
    await btn.click()

    const msg = page.getByTestId("ops-force-turbo-msg")
    await expect(msg).toBeVisible({ timeout: 5_000 })
    await expect(msg).toContainText("Force turbo applied")
    await expect(msg).toContainText("turbo-derate cleared")
    await expect(msg).toContainText("capacity-derate reset")

    // Backend POST must carry confirm:true — server enforces it as the
    // anti-curl-bypass gate.
    expect(capturedBody).toEqual({ confirm: true })
  })

  test("Force turbo: backend failure surfaces a red error message", async ({ page }) => {
    await stubOpsSummary(page, {
      capacity_max: 12,
      effective_budget: 6,
      queue_depth: 0,
      deferred_5m: 0,
      derated: true,
      derate_reason: "CPU 88% > threshold",
    })
    await page.route("**/api/v1/coordinator/force-turbo", async (route) => {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({ detail: "decision-token required" }),
      })
    })
    await page.addInitScript(() => {
      ;(window as Window & typeof globalThis).confirm = () => true
    })
    await page.goto("/")

    const btn = page.getByTestId("ops-force-turbo-btn")
    await expect(btn).toBeVisible({ timeout: 10_000 })
    await btn.click()

    const msg = page.getByTestId("ops-force-turbo-msg")
    await expect(msg).toBeVisible({ timeout: 5_000 })
    await expect(msg).toContainText("Force turbo failed")
  })

  test("Older backend (no coordinator key) hides COORDINATOR section gracefully", async ({ page }) => {
    await stubOpsSummary(page, null)
    await page.goto("/")
    // OpsSummaryPanel still mounts (other KPIs); just no coordinator block.
    await expect(page.getByText("OPS SUMMARY", { exact: true })).toBeVisible({ timeout: 10_000 })
    await expect(page.getByTestId("ops-coordinator-section")).toHaveCount(0)
    await expect(page.getByTestId("ops-derate-badge")).toHaveCount(0)
    await expect(page.getByTestId("ops-force-turbo-btn")).toHaveCount(0)
  })
})

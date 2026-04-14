/**
 * C4 audit-fix — deep-link / URL state round-trip.
 *
 * Phase 50D claims `?panel=…` and `?decision=…` work as deep links,
 * but the only E2E prior to this spec was decision-happy-path. These
 * tests cover the URL→panel mapping the hydration fix (R2-#21) was
 * ostensibly protecting.
 */

import { test, expect } from "@playwright/test"

test.describe("URL deep-link routing", () => {
  test("?panel=timeline activates the Pipeline Timeline panel", async ({ page }) => {
    await page.goto("/?panel=timeline")
    // Accept either aria-label or heading — markup may evolve. The
    // Pipeline Timeline panel always carries "timeline" in its label
    // surface.
    await expect(
      page.getByRole("region", { name: /pipeline timeline/i })
        .or(page.getByRole("heading", { name: /pipeline timeline/i })),
    ).toBeVisible({ timeout: 8000 })
  })

  test("?decision=… deep-link lands on the Decision Queue panel", async ({ page }) => {
    // Any id is fine — we're checking the *routing*, not the lookup.
    await page.goto("/?decision=dec-deadbeef")
    await expect(
      page.getByRole("region", { name: /decision dashboard/i })
        .or(page.getByRole("heading", { name: /decision queue/i })),
    ).toBeVisible({ timeout: 8000 })
  })

  test("invalid ?panel= value falls back to orchestrator (no crash)", async ({ page }) => {
    const errors: string[] = []
    page.on("pageerror", (err) => errors.push(err.message))
    await page.goto("/?panel=totally-not-a-panel")
    // Give React a beat to finish hydrating.
    await page.waitForLoadState("networkidle")
    expect(errors, "page must not crash on invalid deep link").toEqual([])
  })
})

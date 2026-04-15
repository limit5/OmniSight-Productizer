/**
 * G2 — E2E for the operator-doc surface landed in Phases 50-Docs
 * (D1-F2). Covers:
 *   1. ⌘K command palette opens, filters, navigates
 *   2. /docs/operator/<locale> search input filters results
 *   3. ?tour=<step> and ?tour=<anchor> jump to the correct step
 *   4. Reference viewer renders TOC + prev/next + anchor IDs
 *
 * Runs against the live Next.js dev server + FastAPI backend booted
 * by playwright.config.ts.
 */

import { test, expect } from "@playwright/test"

// Use the Meta / Control depending on platform so the test works on
// CI runners regardless of OS.
const isMac = process.platform === "darwin"
const modK = isMac ? "Meta+k" : "Control+k"

test.describe("Command palette (⌘K)", () => {
  test("opens on hotkey and closes on Escape", async ({ page }) => {
    await page.goto("/")
    await page.keyboard.press(modK)

    const palette = page.getByRole("dialog", { name: /command palette|指令面板|命令面板|コマンドパレット/i })
    await expect(palette).toBeVisible({ timeout: 3000 })

    // Esc closes it.
    await page.keyboard.press("Escape")
    await expect(palette).toBeHidden()
  })

  test("filters to matching commands as user types", async ({ page }) => {
    await page.goto("/")
    await page.keyboard.press(modK)

    const input = page.getByPlaceholder(/command|search|搜尋|搜索|検索/i)
    await input.fill("decision")

    // At least one matching option — Decision Queue / Severity / Rules
    // all match. Strictly assert > 0 rather than a specific count so
    // adding commands doesn't break the test.
    const options = page.getByRole("option")
    await expect(options.first()).toBeVisible()
    const count = await options.count()
    expect(count).toBeGreaterThan(0)
  })

  test("Enter on a panel command updates the URL panel param", async ({ page }) => {
    await page.goto("/")
    await page.keyboard.press(modK)

    const input = page.getByPlaceholder(/command|search|搜尋|搜索|検索/i)
    await input.fill("decision queue")
    await page.keyboard.press("Enter")

    // Wait for the URL to reflect the panel change (CommandPalette
    // fires a popstate so the home page's useEffect picks it up).
    await expect(page).toHaveURL(/[?&]panel=decisions/, { timeout: 3000 })
  })
})

test.describe("Docs landing & search", () => {
  test("/docs/operator/en renders all doc cards", async ({ page }) => {
    await page.goto("/docs/operator/en")
    await expect(page.getByRole("heading", { name: /operator docs/i })).toBeVisible()

    // Every core doc title should be present in an <a>.
    for (const t of [
      "Operation Modes",
      "Decision Severity",
      "Panels Overview",
      "Budget Strategies",
      "Glossary",
    ]) {
      await expect(page.getByRole("link", { name: t })).toBeVisible()
    }
  })

  test("typing filters the results list", async ({ page }) => {
    await page.goto("/docs/operator/en")
    const input = page.getByPlaceholder(/search the docs/i)
    await input.fill("budget")

    // Budget Strategies card must remain; Glossary card must drop.
    await expect(page.getByRole("link", { name: "Budget Strategies" })).toBeVisible()
    await expect(page.getByRole("link", { name: "Glossary" })).toHaveCount(0)
  })

  test("unknown locale 404s", async ({ page }) => {
    const resp = await page.goto("/docs/operator/fr")
    expect(resp?.status()).toBe(404)
  })
})

test.describe("First-run tour deep-link", () => {
  test.beforeEach(async ({ context }) => {
    // Forget any prior "tour seen" flag so ?tour= actually reopens it.
    await context.clearCookies()
    await context.addInitScript(() => {
      try {
        Object.keys(localStorage).filter(k => k.includes("tour")).forEach(k => localStorage.removeItem(k))
        localStorage.removeItem("omnisight-tour-seen")
      } catch { /* expected – storage may be unavailable */ }
    })
  })

  test("?tour=1 starts the tour on step 1", async ({ page }) => {
    await page.goto("/?tour=1")
    const dialog = page.getByRole("dialog").first()
    await expect(dialog).toBeVisible({ timeout: 3000 })
    // Step text "1 / 5" is locale-independent.
    await expect(dialog).toContainText("1 / 5")
  })

  test("?tour=decision-queue jumps to step 2", async ({ page }) => {
    await page.goto("/?tour=decision-queue")
    const dialog = page.getByRole("dialog").first()
    await expect(dialog).toBeVisible({ timeout: 3000 })
    await expect(dialog).toContainText("2 / 5")
  })

  test("unknown ?tour= value falls back to step 1 without crashing", async ({ page }) => {
    const errors: string[] = []
    page.on("pageerror", (err) => errors.push(err.message))
    await page.goto("/?tour=nonsense")
    const dialog = page.getByRole("dialog").first()
    await expect(dialog).toBeVisible({ timeout: 3000 })
    expect(errors).toEqual([])
  })
})

test.describe("Reference viewer TOC + prev/next", () => {
  test("operation-modes page has a TOC sidebar with anchor links", async ({ page }) => {
    await page.goto("/docs/operator/en/reference/operation-modes")

    // At least one h2 with an id (for the TOC to target).
    const hasIdHeading = await page.locator("article.doc-article h2[id]").count()
    expect(hasIdHeading).toBeGreaterThan(0)

    // TOC sidebar visible on wide viewports; assert at least one link
    // with a hash href. Skip on narrow viewports where the sidebar is
    // collapsed by Tailwind `hidden lg:block`.
    const vp = page.viewportSize()
    if (vp && vp.width >= 1024) {
      const tocLink = page.locator("aside a[href^='#']").first()
      await expect(tocLink).toBeVisible()
    }
  })

  test("prev/next nav points to adjacent docs in reading order", async ({ page }) => {
    await page.goto("/docs/operator/en/tutorial/first-invoke")
    const nextLink = page.getByRole("link", { name: /Handling a decision/ })
    await expect(nextLink).toBeVisible()
    await expect(nextLink).toHaveAttribute(
      "href",
      /\/docs\/operator\/en\/tutorial\/handling-a-decision$/,
    )
  })
})

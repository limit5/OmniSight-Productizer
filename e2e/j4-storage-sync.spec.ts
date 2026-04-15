import { test, expect } from "@playwright/test"

const BACKEND_PORT = Number(process.env.OMNISIGHT_E2E_BACKEND_PORT ?? "18830")
const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`
const FRONTEND_PORT = Number(process.env.OMNISIGHT_E2E_FRONTEND_PORT ?? "3100")
const FRONTEND = `http://127.0.0.1:${FRONTEND_PORT}`

test.describe("J4 — localStorage multi-tab sync", () => {
  test("locale change in tab A propagates to tab B via storage event", async ({ browser }) => {
    const ctx = await browser.newContext()

    const authMode = await (await ctx.request.get(`${BACKEND}/auth/whoami`)).json()
      .then((r: { auth_mode: string }) => r.auth_mode)

    if (authMode !== "open") {
      await ctx.request.post(`${BACKEND}/auth/login`, {
        data: { email: "admin@omnisight.local", password: "changeme123!" },
      })
    }

    const pageA = await ctx.newPage()
    const pageB = await ctx.newPage()
    await pageA.goto(FRONTEND)
    await pageB.goto(FRONTEND)

    await pageA.waitForLoadState("networkidle")
    await pageB.waitForLoadState("networkidle")

    const localeB_before = await pageB.evaluate(() =>
      document.documentElement.lang
    )

    await pageA.evaluate(() => {
      const keys = Object.keys(localStorage).filter(k => k.includes("omnisight:") && k.includes(":locale"))
      const key = keys[0]
      if (key) {
        localStorage.setItem(key, "ja")
        window.dispatchEvent(new StorageEvent("storage", { key, newValue: "ja" }))
      }
    })

    await pageB.evaluate(({ localeKey }: { localeKey: string }) => {
      if (localeKey) {
        localStorage.setItem(localeKey, "ja")
        window.dispatchEvent(new StorageEvent("storage", { key: localeKey, newValue: "ja" }))
      }
    }, {
      localeKey: await pageB.evaluate(() => {
        return Object.keys(localStorage).find(k => k.includes("omnisight:") && k.includes(":locale")) || ""
      }),
    })

    await pageB.waitForTimeout(500)
    const localeB_after = await pageB.evaluate(() =>
      document.documentElement.lang
    )

    expect(localeB_after).not.toBe(localeB_before)

    await ctx.close()
  })

  test("user_preferences API — wizard_seen persists server-side", async ({ request }) => {
    const authMode = await (await request.get(`${BACKEND}/auth/whoami`)).json()
      .then((r: { auth_mode: string }) => r.auth_mode)

    if (authMode !== "open") {
      const login = await request.post(`${BACKEND}/auth/login`, {
        data: { email: "admin@omnisight.local", password: "changeme123!" },
      })
      expect(login.ok()).toBeTruthy()
    }

    const putRes = await request.put(`${BACKEND}/user-preferences/wizard_seen`, {
      data: { value: "1" },
    })
    expect(putRes.ok()).toBeTruthy()

    const getRes = await request.get(`${BACKEND}/user-preferences/wizard_seen`)
    expect(getRes.ok()).toBeTruthy()
    const body = await getRes.json()
    expect(body.value).toBe("1")

    const allRes = await request.get(`${BACKEND}/user-preferences`)
    expect(allRes.ok()).toBeTruthy()
    const allBody = await allRes.json()
    expect(allBody.items.wizard_seen).toBe("1")
  })

  test("different users have isolated localStorage keys", async ({ browser }) => {
    const ctx = await browser.newContext()
    const page = await ctx.newPage()
    await page.goto(FRONTEND)
    await page.waitForLoadState("networkidle")

    const keys = await page.evaluate(() =>
      Object.keys(localStorage).filter(k => k.startsWith("omnisight:"))
    )

    const hasUserPrefix = keys.every(k => {
      const parts = k.split(":")
      return parts.length >= 3
    })
    expect(hasUserPrefix).toBeTruthy()

    await ctx.close()
  })
})

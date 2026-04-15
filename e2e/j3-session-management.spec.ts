import { test, expect } from "@playwright/test"

const BACKEND_PORT = Number(process.env.OMNISIGHT_E2E_BACKEND_PORT ?? "18830")
const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`

test.describe("J3 — Session management UI", () => {
  test("revoke a session → that session's next API call returns 401", async ({ request }) => {
    const authMode = (await (await request.get(`${BACKEND}/auth/whoami`)).json()).auth_mode
    if (authMode === "open") {
      test.skip(true, "auth_mode=open — session revoke has no effect")
    }

    const admin = await request.post(`${BACKEND}/auth/login`, {
      data: { email: "admin@omnisight.local", password: "changeme123!" },
    })
    expect(admin.ok()).toBeTruthy()
    const adminCookies = admin.headers()["set-cookie"] ?? ""

    const victim = await request.post(`${BACKEND}/auth/login`, {
      data: { email: "admin@omnisight.local", password: "changeme123!" },
    })
    expect(victim.ok()).toBeTruthy()
    const victimCookies = victim.headers()["set-cookie"] ?? ""
    const victimSessionCookie = victimCookies.split(";").find(c => c.trim().startsWith("omnisight_session="))
    expect(victimSessionCookie).toBeDefined()

    const whoamiBefore = await request.get(`${BACKEND}/auth/whoami`, {
      headers: { Cookie: victimCookies },
    })
    expect(whoamiBefore.ok()).toBeTruthy()

    const sessionsResp = await request.get(`${BACKEND}/auth/sessions`, {
      headers: { Cookie: adminCookies },
    })
    expect(sessionsResp.ok()).toBeTruthy()
    const sessions = (await sessionsResp.json()).items as Array<{
      token_hint: string; is_current: boolean
    }>

    const targetSession = sessions.find(s => !s.is_current)
    expect(targetSession).toBeDefined()

    const csrfCookie = adminCookies.split(";").map(c => c.trim()).find(c => c.startsWith("omnisight_csrf="))
    const csrfToken = csrfCookie ? csrfCookie.split("=")[1] : ""

    const revokeResp = await request.delete(
      `${BACKEND}/auth/sessions/${targetSession!.token_hint}`,
      { headers: { Cookie: adminCookies, "X-CSRF-Token": csrfToken } },
    )
    expect(revokeResp.ok()).toBeTruthy()

    const whoamiAfter = await request.get(`${BACKEND}/auth/whoami`, {
      headers: { Cookie: victimCookies },
    })
    expect(whoamiAfter.status()).toBe(401)
  })

  test("revoke all other sessions — only current survives", async ({ request }) => {
    const authMode = (await (await request.get(`${BACKEND}/auth/whoami`)).json()).auth_mode
    if (authMode === "open") {
      test.skip(true, "auth_mode=open — session revoke has no effect")
    }

    const main = await request.post(`${BACKEND}/auth/login`, {
      data: { email: "admin@omnisight.local", password: "changeme123!" },
    })
    expect(main.ok()).toBeTruthy()
    const mainCookies = main.headers()["set-cookie"] ?? ""

    const other1 = await request.post(`${BACKEND}/auth/login`, {
      data: { email: "admin@omnisight.local", password: "changeme123!" },
    })
    expect(other1.ok()).toBeTruthy()
    const other1Cookies = other1.headers()["set-cookie"] ?? ""

    const csrfCookie = mainCookies.split(";").map(c => c.trim()).find(c => c.startsWith("omnisight_csrf="))
    const csrfToken = csrfCookie ? csrfCookie.split("=")[1] : ""

    const revokeAllResp = await request.delete(`${BACKEND}/auth/sessions`, {
      headers: { Cookie: mainCookies, "X-CSRF-Token": csrfToken },
    })
    expect(revokeAllResp.ok()).toBeTruthy()
    const body = await revokeAllResp.json()
    expect(body.revoked_count).toBeGreaterThanOrEqual(1)

    const mainWhoami = await request.get(`${BACKEND}/auth/whoami`, {
      headers: { Cookie: mainCookies },
    })
    expect(mainWhoami.ok()).toBeTruthy()

    const other1Whoami = await request.get(`${BACKEND}/auth/whoami`, {
      headers: { Cookie: other1Cookies },
    })
    expect(other1Whoami.status()).toBe(401)
  })
})

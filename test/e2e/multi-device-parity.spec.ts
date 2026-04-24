/**
 * Q.8 (#302) — Multi-device parity E2E harness.
 *
 * Two browser contexts share one user (admin@omnisight.local) and exercise
 * six cross-device parity scenarios that span Q.1 / Q.2 / Q.3 / Q.6 / Q.7
 * plus the general SSE broadcast contract. Contexts are deliberately
 * isolated (separate cookies + UA + localStorage) so device A and device B
 * look like two physical browsers sharing one account.
 *
 * This file is wired ONLY into the nightly Playwright config
 * (`playwright.nightly.config.ts`) — the main PR pipeline's
 * `playwright.config.ts` still points at `./e2e` and does not discover
 * this spec. That keeps per-PR runtime flat while giving us a daily
 * parity signal. Failures retain screenshot + trace per
 * `playwright.nightly.config.ts::use.trace = "retain-on-failure"`.
 *
 * Preconditions the nightly runner must satisfy (set in the GH Actions
 * workflow, not inferred here):
 *   OMNISIGHT_AUTH_MODE=session     — open mode skips the whole suite
 *   OMNISIGHT_ADMIN_EMAIL=admin@omnisight.local
 *   OMNISIGHT_ADMIN_PASSWORD=changeme123!   — bypasses the bootstrap
 *                                             `must_change_password` gate
 *
 * When `auth_mode=open` every login-dependent scenario is `test.skip()`'d
 * up-front so a dev running `pnpm exec playwright test` locally (without
 * the session env knobs) gets a clean "skipped" summary instead of red.
 */
import { test, expect, type BrowserContext } from "@playwright/test"

const BACKEND_PORT = Number(process.env.OMNISIGHT_E2E_BACKEND_PORT ?? "18830")
const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`
const API = `${BACKEND}/api/v1`

const ADMIN_EMAIL = process.env.OMNISIGHT_ADMIN_EMAIL ?? "admin@omnisight.local"
const ADMIN_PASSWORD = process.env.OMNISIGHT_ADMIN_PASSWORD ?? "changeme123!"

const UA_DEVICE_A =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Q8DeviceA/1.0"
const UA_DEVICE_B =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Q8DeviceB/1.0"

async function getAuthMode(ctx: BrowserContext): Promise<string> {
  const resp = await ctx.request.get(`${API}/auth/whoami`)
  if (!resp.ok()) return "unknown"
  const body = await resp.json()
  return String(body.auth_mode ?? "unknown")
}

async function login(ctx: BrowserContext): Promise<void> {
  const resp = await ctx.request.post(`${API}/auth/login`, {
    data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
  })
  expect(
    resp.ok(),
    `login failed for ${ADMIN_EMAIL}: status=${resp.status()} body=${await resp.text()}`,
  ).toBeTruthy()
}

/**
 * Read the CSRF token the backend set during login. Double-submit cookie
 * pattern — the value lives in `omnisight_csrf` and must echo back in
 * the `X-CSRF-Token` header on all mutating requests.
 */
async function csrf(ctx: BrowserContext): Promise<string> {
  const cookies = await ctx.cookies(BACKEND)
  const row = cookies.find(c => c.name === "omnisight_csrf")
  return row?.value ?? ""
}

/**
 * Subscribe to the SSE stream from inside the page so cookies attach
 * correctly (EventSource only sends same-origin cookies; server-side
 * `request.get` would need explicit header wiring). Returns a handle to
 * an array that the page-side listener appends parsed events to.
 */
async function openSse(
  ctx: BrowserContext,
): Promise<{
  reader: (filterType?: string) => Promise<Array<{ type: string; data: unknown }>>
  close: () => Promise<void>
}> {
  const page = await ctx.newPage()
  // The backend's SSE endpoint is same-origin with the API, so we land
  // the page on an empty backend response first — that gives us a page
  // context whose `new EventSource` defaults to credentials: 'same-origin'
  // which picks up the session cookie.
  await page.goto(`${BACKEND}/health`, { waitUntil: "domcontentloaded" })
  await page.evaluate(
    ({ url }) => {
      const seen: Array<{ type: string; data: unknown }> = []
      // @ts-expect-error — attach to window for later read-back
      window.__q8Events = seen
      const es = new EventSource(url, { withCredentials: true })
      const push = (type: string) => (ev: MessageEvent) => {
        let parsed: unknown = ev.data
        try { parsed = JSON.parse(ev.data) } catch { /* keep as string */ }
        seen.push({ type, data: parsed })
      }
      // Known event types we care about across the six scenarios.
      for (const t of [
        "open", "heartbeat", "task_update", "security.new_device_login",
        "runtime_settings_updated", "llm_provider_switched",
      ]) {
        es.addEventListener(t, push(t))
      }
      // Catch-all onmessage (default event, no `event:` line).
      es.onmessage = (ev: MessageEvent) => {
        let parsed: unknown = ev.data
        try { parsed = JSON.parse(ev.data) } catch { /* keep as string */ }
        seen.push({ type: "message", data: parsed })
      }
      // @ts-expect-error — stash close handle
      window.__q8EventSourceClose = () => es.close()
    },
    { url: `${API}/events` },
  )
  const reader = async (
    filterType?: string,
  ): Promise<Array<{ type: string; data: unknown }>> => {
    const all = await page.evaluate(
      // @ts-expect-error — attached above
      () => (window.__q8Events ?? []).slice(),
    )
    return filterType
      ? all.filter((e: { type: string }) => e.type === filterType)
      : all
  }
  const close = async () => {
    try {
      await page.evaluate(
        // @ts-expect-error — attached above
        () => { (window.__q8EventSourceClose as (() => void) | undefined)?.() },
      )
    } catch { /* page may already be closed */ }
    await page.close()
  }
  // Wait for the `open` event so we know the SSE handshake completed
  // before the test does anything else (otherwise the first downstream
  // mutation can race the subscription and the event is lost).
  await expect.poll(async () => (await reader("open")).length, {
    message: "SSE never opened — backend may be down or 401'd the stream",
    timeout: 10_000,
  }).toBeGreaterThan(0)
  return { reader, close }
}

// ─────────────────────────────────────────────────────────────────────────
// Suite
// ─────────────────────────────────────────────────────────────────────────

test.describe("Q.8 — multi-device parity (nightly)", () => {
  let ctxA: BrowserContext
  let ctxB: BrowserContext
  let authMode: string

  test.beforeAll(async ({ browser }) => {
    // Two distinct contexts ≡ two distinct browsers: separate cookies,
    // separate localStorage, distinct UA so the new-device fingerprint
    // for scenario 4 actually trips.
    ctxA = await browser.newContext({ userAgent: UA_DEVICE_A })
    ctxB = await browser.newContext({ userAgent: UA_DEVICE_B })
    authMode = await getAuthMode(ctxA)
  })

  test.afterAll(async () => {
    await ctxA?.close()
    await ctxB?.close()
  })

  // ───── Scenario 1 (Q.3): LLM provider switch visible cross-device ─────
  test("1. A changes LLM provider → B sees the new active provider", async () => {
    if (authMode === "open") test.skip(true, "auth_mode=open — login gating absent")
    await login(ctxA)
    await login(ctxB)

    const beforeResp = await ctxA.request.get(`${API}/providers`)
    expect(beforeResp.ok()).toBeTruthy()
    const before = await beforeResp.json() as {
      active_provider: string
      providers: Array<{ name: string; configured: boolean }>
    }
    // Pick any provider that's NOT currently active. Prefer a configured
    // one so the switch doesn't get rejected for "no key". Fall back to
    // any different provider if none is configured — the test still
    // exercises the parity path even if the switch itself is rejected,
    // because we assert cross-device *agreement* on the post-state, not
    // on switch success.
    const candidate =
      before.providers.find(p => p.configured && p.name !== before.active_provider)
      ?? before.providers.find(p => p.name !== before.active_provider)
    if (!candidate) {
      test.skip(true, "only one provider exists — cannot exercise a switch")
    }

    const switchResp = await ctxA.request.post(`${API}/providers/switch`, {
      headers: { "X-CSRF-Token": await csrf(ctxA) },
      data: { provider: candidate!.name },
    })
    // Accept 2xx (switch landed) OR 4xx (provider unreachable / no key).
    // Either way, A and B must converge on the same server state —
    // that's the parity contract Q.3 is defending.
    expect([200, 400, 409, 503]).toContain(switchResp.status())

    // Poll B up to 5 s — Q.3 SSE push is not yet implemented (see TODO
    // Q.3 #297 checkbox "SSE broadcast for runtime settings"), so we
    // fall back to short-interval polling. When Q.3 lands, tighten this
    // to an SSE-event wait.
    await expect.poll(async () => {
      const r = await ctxB.request.get(`${API}/providers`)
      if (!r.ok()) return null
      const j = await r.json() as { active_provider: string }
      return j.active_provider
    }, {
      message: "B did not converge on A's active_provider within 5s",
      timeout: 5_000,
      intervals: [250, 500, 1000],
    }).toBe((await (await ctxA.request.get(`${API}/providers`)).json()).active_provider)
  })

  // ───── Scenario 2 (Q.4 SSE): task_update broadcast cross-device ─────
  test("2. A creates a task → B sees task_update (action=created) on SSE", async () => {
    if (authMode === "open") test.skip(true, "auth_mode=open — session SSE scope absent")
    await login(ctxA)
    await login(ctxB)

    const sseB = await openSse(ctxB)
    try {
      const createResp = await ctxA.request.post(`${API}/tasks`, {
        headers: { "X-CSRF-Token": await csrf(ctxA) },
        data: {
          title: `q8-parity-${Date.now()}`,
          description: "Q.8 scenario 2 — task_update SSE broadcast",
          priority: "medium",
        },
      })
      expect(createResp.ok()).toBeTruthy()
      const task = await createResp.json() as { id: string }

      // Wait up to 5 s for B's SSE stream to carry the task_update with
      // our specific task id. action=created is the Q.3-SUB-2 contract
      // (tasks.py:133-142).
      await expect.poll(async () => {
        const events = await sseB.reader("task_update")
        return events.some(e => {
          const d = e.data as { task_id?: string; action?: string }
          return d?.task_id === task.id && d?.action === "created"
        })
      }, {
        message: `B never saw task_update action=created for ${task.id}`,
        timeout: 5_000,
        intervals: [200, 500, 1000],
      }).toBeTruthy()
    } finally {
      await sseB.close()
    }
  })

  // ───── Scenario 3 (Q.1): password change → peer 401 with trigger ─────
  test("3. A changes password → B's next request 401s with user_security_event", async () => {
    if (authMode === "open") test.skip(true, "auth_mode=open — session revocation no-op")
    // Fresh logins so this scenario doesn't interact with earlier ones.
    await login(ctxA)
    await login(ctxB)

    // Sanity — B is authenticated right now.
    const before = await ctxB.request.get(`${API}/auth/whoami`)
    expect(before.ok(), `B should be authenticated before change-password, got ${before.status()}`).toBeTruthy()
    const beforeBody = await before.json()
    expect(beforeBody.email ?? beforeBody.user?.email).toBe(ADMIN_EMAIL)

    // Change password: flip to a new value then flip back so subsequent
    // test runs (and scenarios below if the suite re-orders) can still
    // log in with the canonical password.
    const newPw = `q8-temp-${Date.now()}!`
    const change1 = await ctxA.request.post(`${API}/auth/change-password`, {
      headers: { "X-CSRF-Token": await csrf(ctxA) },
      data: { old_password: ADMIN_PASSWORD, new_password: newPw },
    })
    expect(change1.ok(), `change-password #1 failed: ${change1.status()} ${await change1.text()}`).toBeTruthy()

    try {
      // B's next request must 401. The body carries the Q.1 trigger so
      // the frontend banner can render the localised "Your password was
      // changed on another device" copy (app/login/page.tsx:17-40).
      const after = await ctxB.request.get(`${API}/auth/whoami`)
      expect(after.status(), "B's whoami should be 401 after peer password change").toBe(401)
      const detail = await after.json().catch(() => ({}))
      // FastAPI wraps error responses in {detail: {...}} — look at both
      // shapes to stay tolerant of handler refactors.
      const reason = detail?.detail?.reason ?? detail?.reason
      const trigger = detail?.detail?.trigger ?? detail?.trigger
      expect(reason, `401 body missing reason: ${JSON.stringify(detail)}`).toBe("user_security_event")
      expect(trigger).toBe("password_change")
    } finally {
      // Reset password — A's cookies were rotated by the cascade, so
      // re-login first, then flip back. Without the reset, later
      // scenarios (5, 6) and the next nightly run fail at login.
      await login(ctxA)
      const change2 = await ctxA.request.post(`${API}/auth/change-password`, {
        headers: { "X-CSRF-Token": await csrf(ctxA) },
        data: { old_password: newPw, new_password: ADMIN_PASSWORD },
      })
      expect(change2.ok(), `password rollback failed: ${change2.status()}`).toBeTruthy()
    }
  })

  // ───── Scenario 4 (Q.2): new-device login → peer SSE alert ─────
  test("4. A (established) subscribed + B new-device login → A sees security.new_device_login", async () => {
    if (authMode === "open") test.skip(true, "auth_mode=open — new-device fingerprint absent")
    await login(ctxA)

    // A subscribes first so the SSE stream is hot before B's login
    // fires the alert. Skipping this ordering makes the event land
    // before the subscriber and the test turns flaky.
    const sseA = await openSse(ctxA)
    try {
      // Drop any cookie B might still carry so its login looks like a
      // clean new-device login (fresh ua + fresh cookies ≡ new
      // fingerprint per `_record_session_fingerprint` 30d window).
      await ctxB.clearCookies()
      await login(ctxB)

      await expect.poll(async () => {
        const events = await sseA.reader("security.new_device_login")
        return events.length > 0
      }, {
        // The Q.2 dedup rate-limit is per-(user, /24 subnet) 24h; if
        // another scenario already fired the alert in the same run A
        // may never see a second one. Keep the timeout modest and
        // annotate the failure so CI artifacts point the operator at
        // the dedup window, not at a missing wire.
        message:
          "A never received security.new_device_login — expected within " +
          "5s of B's login. If this is an early-run flake, inspect " +
          "backend.auth._new_device_alert_should_fire dedup state.",
        timeout: 5_000,
        intervals: [250, 500, 1000],
      }).toBeTruthy()
    } finally {
      await sseA.close()
    }
  })

  // ───── Scenario 5 (Q.7): concurrent PATCH task → exactly one 409 ─────
  test("5. A + B PATCH same task concurrently → one 200, one 409 with version body", async () => {
    if (authMode === "open") test.skip(true, "auth_mode=open — optimistic-lock path needs session")
    await login(ctxA)
    await login(ctxB)

    // Seed the task from A. ``version`` comes back 0 per the Q.7 UPDATE
    // guard (tasks.py:195-220) — both contexts will send If-Match: 0.
    const createResp = await ctxA.request.post(`${API}/tasks`, {
      headers: { "X-CSRF-Token": await csrf(ctxA) },
      data: {
        title: `q8-concurrent-${Date.now()}`,
        description: "Q.8 scenario 5 — concurrent PATCH 409 assertion",
        priority: "low",
      },
    })
    expect(createResp.ok()).toBeTruthy()
    const task = await createResp.json() as { id: string; version?: number }
    const initialVersion = task.version ?? 0

    // Resolve CSRF tokens up-front: fetching them inside Promise.all's
    // legs would force the two PATCHes to serialise on the cookie read
    // and defeat the race the 409 assertion depends on.
    const aCsrf = await csrf(ctxA)
    const bCsrf = await csrf(ctxB)
    const [rA, rB] = await Promise.all([
      ctxA.request.patch(`${API}/tasks/${task.id}`, {
        headers: { "X-CSRF-Token": aCsrf, "If-Match": String(initialVersion) },
        data: { description: `q8-concurrent-A-${Date.now()}` },
      }),
      ctxB.request.patch(`${API}/tasks/${task.id}`, {
        headers: { "X-CSRF-Token": bCsrf, "If-Match": String(initialVersion) },
        data: { description: `q8-concurrent-B-${Date.now()}` },
      }),
    ])

    const statuses = [rA.status(), rB.status()].sort((a, b) => a - b)
    expect(statuses, `expected one winner + one 409, got ${statuses}`).toEqual([200, 409])

    const loser = rA.status() === 409 ? rA : rB
    const body = await loser.json().catch(() => ({}))
    // Q.7 #301 contract — body.detail carries four named fields.
    const detail = body?.detail ?? body
    expect(detail).toMatchObject({
      resource: "task",
      hint: expect.any(String),
    })
    expect(typeof detail.your_version).toBe("number")
    // current_version may be null when the row was missing; for a fresh
    // seed it should be an int ≥ initialVersion.
    if (detail.current_version !== null) {
      expect(typeof detail.current_version).toBe("number")
      expect(detail.current_version).toBeGreaterThanOrEqual(initialVersion)
    }
  })

  // ───── Scenario 6 (Q.6): draft PUT on A → GET on B restores it ─────
  test("6. A writes a draft slot → B restores the same content", async () => {
    if (authMode === "open") test.skip(true, "auth_mode=open — drafts require session user")
    await login(ctxA)
    await login(ctxB)

    const slotKey = `invoke:q8-${Date.now()}`
    const content = `Hello from device A at ${new Date().toISOString()}`

    const putResp = await ctxA.request.put(`${API}/user/drafts/${slotKey}`, {
      headers: { "X-CSRF-Token": await csrf(ctxA) },
      data: { content },
    })
    expect(putResp.ok(), `PUT draft failed: ${putResp.status()}`).toBeTruthy()
    const putBody = await putResp.json() as { slot_key: string; content: string; updated_at: string }
    expect(putBody.content).toBe(content)
    expect(putBody.updated_at).toBeTruthy()

    // B restores on the next mount. Q.6 is last-writer-wins HTTP-only
    // (no SSE), so a direct GET is the parity check — the exact
    // signal the frontend `use-draft-restore` hook consumes.
    const getResp = await ctxB.request.get(`${API}/user/drafts/${slotKey}`)
    expect(getResp.ok()).toBeTruthy()
    const getBody = await getResp.json() as { content: string; updated_at: string }
    expect(getBody.content).toBe(content)
    expect(getBody.updated_at).toBe(putBody.updated_at)
  })
})

/**
 * Y8 row 8 — Playwright E2E for the multi-tenant invite + switcher flow.
 *
 * Lives under `test/e2e/` so it is picked up by `playwright.nightly.config.ts`
 * (the only CI lane that boots the backend with `OMNISIGHT_AUTH_MODE=session`
 * + the seeded admin password). The default `playwright.config.ts` lane
 * leaves `auth_mode=open`, where every login-gated path in this spec is
 * a no-op — running it there would just `test.skip()` and burn a matrix
 * slot. Putting it here lets the nightly job actually exercise the full
 * flow against a session-mode backend (the spec also keeps the explicit
 * `test.skip(auth_mode==="open", ...)` guard so a dev who runs it
 * locally against an open-mode backend still gets a clear skip reason).
 *
 * Walks the contract that the operator hits in the wild, end-to-end, in
 * a real browser against a real backend:
 *
 *   1. As super-admin (REST), create two tenants (A, B), one project per
 *      tenant (PA, PB), and two invites for the same brand-new email
 *      address — one into each tenant.
 *   2. As an anonymous browser, navigate to `/invite/<inv-A>.<token>`,
 *      fill the anon form, submit. Backend mints a fresh user row,
 *      upserts the tenant-A membership.
 *   3. Log the new user in via `page.request` (cookies land on the page
 *      context). Use that authenticated session to also accept the
 *      tenant-B invite via REST (covers the authed branch of the same
 *      endpoint).
 *   4. Hit `/` (dashboard). TenantSwitcher must be visible (the user has
 *      ≥2 tenants now). Open the dropdown, confirm both tenants appear.
 *   5. Switch to tenant A via the UI. ProjectSwitcher (single-project
 *      static label) must show project A's name and NOT B's.
 *   6. Switch to tenant B. ProjectSwitcher must show B's name and NOT
 *      A's.
 *   7. Switch back to tenant A. ProjectSwitcher must show A's name and
 *      NOT B's — proving cross-tenant data does not leak across the
 *      switch.
 *   8. Cross-check at the API layer: as the invitee, GET each tenant's
 *      project list and assert the project from the other tenant is
 *      absent (the X-Tenant-Id header gate is the source of truth; the
 *      UI flow above is what the operator sees).
 *
 * Auth-mode skip rule
 * ───────────────────
 * Mirrors j3/j4 — the spec is meaningful only when the backend is
 * running in `session` mode. In `open` mode (the default for the local
 * `frontend-e2e` CI matrix) every request is a synthetic super-admin
 * and tenant switchers / login flows are no-ops. In `session` mode (set
 * by `e2e-multi-device-nightly.yml`) the full flow exercises real login
 * + cookie-bearing requests.
 *
 * "Project artifact" interpretation
 * ─────────────────────────────────
 * The Y8 row 8 task line in TODO.md says "看 project artifact". There is
 * no public REST surface to seed an artifact (artifacts are produced by
 * agent runs internally). The meaningful invariant is "tenant-scoped
 * data is not visible after switching tenants" — the project list IS
 * tenant-scoped via Y4 row 2 + Y5 _project_header_gate, so it serves as
 * the tenant-isolation oracle. The UI surface that visibly reflects the
 * scope flip is the ProjectSwitcher label (single-project tenants
 * render `project-switcher-static`).
 */

import { test, expect } from "@playwright/test"

const BACKEND_PORT = Number(process.env.OMNISIGHT_E2E_BACKEND_PORT ?? "18830")
const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`

// e2e-multi-device-nightly.yml seeds the admin user with this password
// after `OMNISIGHT_AUTH_MODE=session`. The default (`omnisight-admin`)
// would trip `must_change_password=1` and block REST login.
const ADMIN_EMAIL = process.env.OMNISIGHT_ADMIN_EMAIL ?? "admin@omnisight.local"
const ADMIN_PASSWORD = process.env.OMNISIGHT_ADMIN_PASSWORD ?? "changeme123!"

interface TenantOption { id: string; name: string }
interface ProjectRow { project_id: string; name: string; tenant_id: string }
interface InviteCreated { invite_id: string; token_plaintext: string }

function _csrfFromSetCookie(setCookieHeader: string): string {
  const c = setCookieHeader
    .split(";")
    .map(s => s.trim())
    .find(s => s.startsWith("omnisight_csrf="))
  return c ? c.split("=")[1] : ""
}

test.describe("Y8 — multi-tenant invite + switcher flow", () => {
  test("invite anon → accept → switch tenant/project → cross-tenant isolation", async ({
    request,
    page,
  }) => {
    // ─── 0. Skip in open mode (mirrors j3/j4) ──────────────────────
    const whoamiInit = await (
      await request.get(`${BACKEND}/api/v1/auth/whoami`)
    ).json()
    if (whoamiInit.auth_mode === "open") {
      test.skip(
        true,
        "auth_mode=open — invite/login/tenant-switch flow has no semantics in open mode (every request is synthetic super-admin)",
      )
    }

    // ─── 1. Admin login (test-scoped request — does NOT pollute the
    //       browser context that the invitee will use later). ───────
    const adminLogin = await request.post(`${BACKEND}/api/v1/auth/login`, {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    })
    expect(
      adminLogin.ok(),
      `admin login failed (HTTP ${adminLogin.status()}): ${await adminLogin.text()}`,
    ).toBeTruthy()
    const adminSetCookie = adminLogin.headers()["set-cookie"] ?? ""
    expect(adminSetCookie).toContain("omnisight_session=")
    const adminCsrf = _csrfFromSetCookie(adminSetCookie)
    const adminHeaders: Record<string, string> = {
      Cookie: adminSetCookie,
      "X-CSRF-Token": adminCsrf,
    }

    const whoamiAdmin = await (
      await request.get(`${BACKEND}/api/v1/auth/whoami`, {
        headers: { Cookie: adminSetCookie },
      })
    ).json()
    expect(whoamiAdmin.user.role).toBe("super_admin")

    // ─── 2. Setup: 2 tenants + 1 project per tenant + 2 invites ────
    const stamp = Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
    const tenantA = `t-y8e2e-a-${stamp}`
    const tenantB = `t-y8e2e-b-${stamp}`
    const projectAName = `Y8 E2E Project A ${stamp}`
    const projectBName = `Y8 E2E Project B ${stamp}`
    const projectASlug = `y8-a-${stamp}`
    const projectBSlug = `y8-b-${stamp}`
    const inviteEmail = `y8e2e-invitee-${stamp}@example.test`

    const createTA = await request.post(`${BACKEND}/api/v1/admin/tenants`, {
      data: {
        id: tenantA,
        name: `Y8 E2E Tenant A ${stamp}`,
        plan: "starter",
      },
      headers: adminHeaders,
    })
    expect(
      createTA.ok(),
      `create tenant A failed (HTTP ${createTA.status()}): ${await createTA.text()}`,
    ).toBeTruthy()

    const createTB = await request.post(`${BACKEND}/api/v1/admin/tenants`, {
      data: {
        id: tenantB,
        name: `Y8 E2E Tenant B ${stamp}`,
        plan: "starter",
      },
      headers: adminHeaders,
    })
    expect(
      createTB.ok(),
      `create tenant B failed (HTTP ${createTB.status()}): ${await createTB.text()}`,
    ).toBeTruthy()

    const createPA = await request.post(
      `${BACKEND}/api/v1/tenants/${tenantA}/projects`,
      {
        data: {
          product_line: "embedded",
          name: projectAName,
          slug: projectASlug,
        },
        headers: adminHeaders,
      },
    )
    expect(
      createPA.ok(),
      `create project A failed (HTTP ${createPA.status()}): ${await createPA.text()}`,
    ).toBeTruthy()
    const projectA: ProjectRow = await createPA.json()

    const createPB = await request.post(
      `${BACKEND}/api/v1/tenants/${tenantB}/projects`,
      {
        data: {
          product_line: "embedded",
          name: projectBName,
          slug: projectBSlug,
        },
        headers: adminHeaders,
      },
    )
    expect(
      createPB.ok(),
      `create project B failed (HTTP ${createPB.status()}): ${await createPB.text()}`,
    ).toBeTruthy()
    const projectB: ProjectRow = await createPB.json()

    expect(projectA.project_id).toMatch(/^p-/)
    expect(projectB.project_id).toMatch(/^p-/)
    expect(projectA.project_id).not.toBe(projectB.project_id)

    // Invite into tenant A — `admin` role gives the invitee project
    // visibility on tenant A (project_members default rule).
    const createInviteA = await request.post(
      `${BACKEND}/api/v1/tenants/${tenantA}/invites`,
      {
        data: { email: inviteEmail, role: "admin" },
        headers: adminHeaders,
      },
    )
    expect(
      createInviteA.ok(),
      `create invite A failed (HTTP ${createInviteA.status()}): ${await createInviteA.text()}`,
    ).toBeTruthy()
    const inviteA: InviteCreated = await createInviteA.json()
    expect(inviteA.invite_id).toMatch(/^inv-[a-z0-9]+$/)
    expect(typeof inviteA.token_plaintext).toBe("string")
    expect(inviteA.token_plaintext.length).toBeGreaterThan(16)

    // ─── 3. Anon invite-accept via the UI (covers the unauthenticated
    //       branch of POST /api/v1/invites/{id}/accept). ─────────────
    const inviteePassword = `Y8e2e-${stamp}-Aa1!`
    const inviteSegmentA = `${inviteA.invite_id}.${inviteA.token_plaintext}`
    await page.goto(`/invite/${inviteSegmentA}`)
    await expect(page.getByTestId("invite-anon-form")).toBeVisible()
    await page.getByTestId("invite-name-input").fill(`Y8 E2E User ${stamp}`)
    await page.getByTestId("invite-password-input").fill(inviteePassword)
    await page.getByTestId("invite-anon-submit").click()
    await expect(page.getByTestId("invite-success-panel")).toBeVisible()
    // The success headline embeds the tenant id (anon branch with a
    // freshly-minted user) — tightens the assertion against the right
    // tenant_id surfacing.
    await expect(page.getByTestId("invite-success-headline")).toContainText(
      tenantA,
    )

    // ─── 4. Log the new user in via page.request — cookies land on
    //       the same browser context that page.goto() will use next.
    //       We hit the FRONTEND port (baseURL) so the Set-Cookie has
    //       the right origin for the dashboard subsequently. ──────
    const inviteeLogin = await page.request.post("/api/v1/auth/login", {
      data: { email: inviteEmail, password: inviteePassword },
    })
    expect(
      inviteeLogin.ok(),
      `invitee login failed (HTTP ${inviteeLogin.status()}): ${await inviteeLogin.text()}`,
    ).toBeTruthy()
    const inviteeWhoami = await (
      await page.request.get("/api/v1/auth/whoami")
    ).json()
    expect(inviteeWhoami.user.email).toBe(inviteEmail)

    // ─── 5. Issue + accept invite into tenant B as the now-authed
    //       invitee. Covers the authenticated branch of the same
    //       accept endpoint AND seeds the multi-tenant state we need
    //       for the switcher flow. ────────────────────────────────
    const createInviteB = await request.post(
      `${BACKEND}/api/v1/tenants/${tenantB}/invites`,
      {
        data: { email: inviteEmail, role: "admin" },
        headers: adminHeaders,
      },
    )
    expect(
      createInviteB.ok(),
      `create invite B failed (HTTP ${createInviteB.status()}): ${await createInviteB.text()}`,
    ).toBeTruthy()
    const inviteB: InviteCreated = await createInviteB.json()

    const acceptB = await page.request.post(
      `/api/v1/invites/${inviteB.invite_id}/accept`,
      { data: { token: inviteB.token_plaintext } },
    )
    expect(
      acceptB.ok(),
      `authed accept of invite B failed (HTTP ${acceptB.status()}): ${await acceptB.text()}`,
    ).toBeTruthy()
    const acceptBBody = await acceptB.json()
    expect(acceptBBody.tenant_id).toBe(tenantB)
    expect(acceptBBody.already_member).toBeFalsy()

    // ─── 6. Sanity-check membership at /auth/tenants — the dashboard
    //       picker is fed by this endpoint via TenantProvider. ─────
    const tenantsResp = await page.request.get("/api/v1/auth/tenants")
    expect(tenantsResp.ok()).toBeTruthy()
    const tenantsBody = await tenantsResp.json()
    const tenantsList: TenantOption[] = Array.isArray(tenantsBody)
      ? tenantsBody
      : tenantsBody.items ?? tenantsBody.tenants ?? []
    const tenantIds = tenantsList.map(t => t.id)
    expect(tenantIds).toContain(tenantA)
    expect(tenantIds).toContain(tenantB)

    // ─── 7. Cross-tenant API isolation cross-check — done BEFORE the
    //       UI flow so we have a hard backstop on the contract even
    //       if a UI repaint races. ────────────────────────────────
    const projsForA = await (
      await page.request.get(`/api/v1/tenants/${tenantA}/projects`)
    ).json()
    const projsForB = await (
      await page.request.get(`/api/v1/tenants/${tenantB}/projects`)
    ).json()
    const projAIds = (projsForA.items ?? projsForA).map(
      (p: ProjectRow) => p.project_id,
    )
    const projBIds = (projsForB.items ?? projsForB).map(
      (p: ProjectRow) => p.project_id,
    )
    expect(projAIds).toContain(projectA.project_id)
    expect(projAIds).not.toContain(projectB.project_id)
    expect(projBIds).toContain(projectB.project_id)
    expect(projBIds).not.toContain(projectA.project_id)

    // ─── 8. UI flow: dashboard → switcher → assert isolation ──────
    await page.goto("/")

    // TenantSwitcher only renders the dropdown when membership count
    // ≥ 2 — guaranteed by the two accept calls above. Wait for the
    // button rather than a fragile visual cue.
    await expect(page.getByTestId("tenant-switcher-btn")).toBeVisible({
      timeout: 15_000,
    })

    // Open dropdown; both options visible.
    await page.getByTestId("tenant-switcher-btn").click()
    await expect(page.getByTestId("tenant-switcher-list")).toBeVisible()
    await expect(
      page.getByTestId(`tenant-option-${tenantA}`),
    ).toBeVisible()
    await expect(
      page.getByTestId(`tenant-option-${tenantB}`),
    ).toBeVisible()

    // Switch to A. Single-project tenant → ProjectSwitcher renders the
    // static label. Wait for it to show A's project name (the listener
    // chain in TenantProvider → ProjectProvider clears the project
    // list, refetches, and auto-selects).
    await page.getByTestId(`tenant-option-${tenantA}`).click()
    await expect(page.getByTestId("project-switcher-static")).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.getByTestId("project-switcher-static")).toContainText(
      projectAName,
    )
    await expect(
      page.getByTestId("project-switcher-static"),
    ).not.toContainText(projectBName)

    // Switch to B. ProjectSwitcher should now reflect B's project name
    // and the previous tenant's project name must NOT be visible —
    // this is the core "cross-tenant data does not leak" assertion.
    await page.getByTestId("tenant-switcher-btn").click()
    await page.getByTestId(`tenant-option-${tenantB}`).click()
    await expect(page.getByTestId("project-switcher-static")).toContainText(
      projectBName,
      { timeout: 10_000 },
    )
    await expect(
      page.getByTestId("project-switcher-static"),
    ).not.toContainText(projectAName)

    // Switch BACK to A — confirm the switch is bidirectional and the
    // B-tenant's project name has fully cleared.
    await page.getByTestId("tenant-switcher-btn").click()
    await page.getByTestId(`tenant-option-${tenantA}`).click()
    await expect(page.getByTestId("project-switcher-static")).toContainText(
      projectAName,
      { timeout: 10_000 },
    )
    await expect(
      page.getByTestId("project-switcher-static"),
    ).not.toContainText(projectBName)
  })
})

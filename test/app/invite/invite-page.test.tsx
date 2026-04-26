/**
 * Y8 row 7 — /invite/<invite_id>.<token> page contract tests.
 *
 * Locks in the operator-visible behaviour of the invite-accept page:
 *
 *   1. URL parsing: missing `.`, malformed invite id, malformed
 *      token segment all short-circuit to a placeholder without
 *      hitting the backend.
 *   2. Auth-loading state shows a spinner placeholder rather than
 *      the form (avoids the brief flash of the anon UI before
 *      `useAuth` resolves).
 *   3. Anon flow: form submit → POST acceptInvite with name +
 *      password; success panel directs the user to /login.
 *   4. Authed flow: button click → POST acceptInvite with token
 *      only; success panel directs the user to the dashboard.
 *   5. Error mapping: 404 / 410 / 409-already-accepted /
 *      409-email-mismatch / 403 / 429 each render a status-specific
 *      placard with stable `data-testid` so future copy tweaks do
 *      not break tests.
 *   6. Email-mismatch on the authed flow surfaces both addresses
 *      and a "sign out and retry" action that calls `logout()`.
 */

import React, { Suspense } from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
  act,
} from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    replace: vi.fn(),
    push: vi.fn(),
    refresh: vi.fn(),
  }),
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    acceptInvite: vi.fn(),
  }
})

import InviteAcceptPage from "@/app/invite/[token]/page"
import { useAuth } from "@/lib/auth-context"
import { ApiError, acceptInvite } from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedAccept = acceptInvite as unknown as ReturnType<typeof vi.fn>

const INVITE_ID = "inv-deadbeef00ff"
const TOKEN = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG-_aa"
const COMBINED = `${INVITE_ID}.${TOKEN}`

function makeParams(token: string = COMBINED) {
  return Promise.resolve({ token })
}

async function renderPage(token: string = COMBINED) {
  await act(async () => {
    render(
      <Suspense fallback={null}>
        <InviteAcceptPage params={makeParams(token)} />
      </Suspense>,
    )
  })
}

function setAnon() {
  mockedUseAuth.mockReturnValue({
    user: null,
    authMode: "session",
    sessionId: null,
    loading: false,
    error: null,
    mfaPending: null,
    login: vi.fn(),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(),
    submitMfa: vi.fn(),
    cancelMfa: vi.fn(),
  })
}

function setLoggedIn(email = "alice@x.io") {
  return mockedUseAuth.mockReturnValue({
    user: {
      id: "u-alice",
      email,
      name: "Alice",
      role: "operator",
      enabled: true,
      tenant_id: "t-acme",
    },
    authMode: "session",
    sessionId: "sess-1",
    loading: false,
    error: null,
    mfaPending: null,
    login: vi.fn(),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(),
    submitMfa: vi.fn(),
    cancelMfa: vi.fn(),
  })
}

function setAuthLoading() {
  mockedUseAuth.mockReturnValue({
    user: null,
    authMode: null,
    sessionId: null,
    loading: true,
    error: null,
    mfaPending: null,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
    submitMfa: vi.fn(),
    cancelMfa: vi.fn(),
  })
}

beforeEach(() => {
  cleanup()
  mockedUseAuth.mockReset()
  mockedAccept.mockReset()
})

// ──────────────────────────────────────────────────────────────────
// 1. URL parsing — bad-link short-circuits before any API call.
// ──────────────────────────────────────────────────────────────────
describe("/invite/[token] — URL parsing", () => {
  it("renders bad-link placard when separator is missing", async () => {
    setAnon()
    await renderPage("inv-noseparator")
    expect(await screen.findByTestId("invite-bad-link")).toBeInTheDocument()
    expect(mockedAccept).not.toHaveBeenCalled()
  })

  it("renders bad-link when invite id segment is malformed", async () => {
    setAnon()
    await renderPage(`bogus-id.${TOKEN}`)
    expect(await screen.findByTestId("invite-bad-link")).toBeInTheDocument()
    expect(mockedAccept).not.toHaveBeenCalled()
  })

  it("renders bad-link when token segment is too short", async () => {
    setAnon()
    await renderPage(`${INVITE_ID}.short`)
    expect(await screen.findByTestId("invite-bad-link")).toBeInTheDocument()
    expect(mockedAccept).not.toHaveBeenCalled()
  })

  it("renders bad-link when token segment contains illegal chars", async () => {
    setAnon()
    await renderPage(`${INVITE_ID}.aaaa!!!aaaa!!!aaaa`)
    expect(await screen.findByTestId("invite-bad-link")).toBeInTheDocument()
    expect(mockedAccept).not.toHaveBeenCalled()
  })
})

// ──────────────────────────────────────────────────────────────────
// 2. Auth loading — spinner placeholder, no API call yet.
// ──────────────────────────────────────────────────────────────────
describe("/invite/[token] — auth gate", () => {
  it("shows spinner while auth is still loading and never calls API", async () => {
    setAuthLoading()
    await renderPage()
    expect(await screen.findByTestId("invite-loading")).toBeInTheDocument()
    expect(mockedAccept).not.toHaveBeenCalled()
  })
})

// ──────────────────────────────────────────────────────────────────
// 3. Anon flow — registration form → POST accept → success panel.
// ──────────────────────────────────────────────────────────────────
describe("/invite/[token] — anonymous flow", () => {
  it("renders the form and exposes the back-to-login link with next param", async () => {
    setAnon()
    await renderPage()
    expect(await screen.findByTestId("invite-anon-form")).toBeInTheDocument()
    const link = screen.getByTestId("invite-login-link") as HTMLAnchorElement
    expect(link.getAttribute("href")).toContain("/login")
    expect(link.getAttribute("href")).toContain(
      encodeURIComponent(`/invite/${COMBINED}`),
    )
  })

  it("submits with name + password and shows the create-account success panel", async () => {
    setAnon()
    mockedAccept.mockResolvedValueOnce({
      invite_id: INVITE_ID,
      tenant_id: "t-acme",
      user_id: "u-new",
      user_email: "new@x.io",
      role: "member",
      status: "accepted",
      user_was_created: true,
      already_member: false,
    })
    await renderPage()
    fireEvent.change(screen.getByTestId("invite-name-input"), {
      target: { value: "New User" },
    })
    fireEvent.change(screen.getByTestId("invite-password-input"), {
      target: { value: "hunter2hunter2" },
    })
    fireEvent.click(screen.getByTestId("invite-anon-submit"))
    await waitFor(() => expect(mockedAccept).toHaveBeenCalledTimes(1))
    expect(mockedAccept).toHaveBeenCalledWith(INVITE_ID, {
      token: TOKEN,
      name: "New User",
      password: "hunter2hunter2",
    })
    expect(
      await screen.findByTestId("invite-success-panel"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("invite-go-login")).toBeInTheDocument()
  })

  it("treats a blank password as null in the request body", async () => {
    setAnon()
    mockedAccept.mockResolvedValueOnce({
      invite_id: INVITE_ID,
      tenant_id: "t-acme",
      user_id: "u-new",
      user_email: "new@x.io",
      role: "viewer",
      status: "accepted",
      user_was_created: true,
      already_member: false,
    })
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-anon-submit"))
    await waitFor(() => expect(mockedAccept).toHaveBeenCalledTimes(1))
    expect(mockedAccept).toHaveBeenCalledWith(INVITE_ID, {
      token: TOKEN,
      name: "",
      password: null,
    })
  })
})

// ──────────────────────────────────────────────────────────────────
// 4. Logged-in flow — button → POST accept → success panel.
// ──────────────────────────────────────────────────────────────────
describe("/invite/[token] — authenticated flow", () => {
  it("shows the accept button and the signed-in email", async () => {
    setLoggedIn("alice@x.io")
    await renderPage()
    expect(await screen.findByTestId("invite-authed-panel")).toBeInTheDocument()
    expect(screen.getByTestId("invite-authed-email").textContent).toContain(
      "alice@x.io",
    )
  })

  it("clicking accept POSTs token only and shows the dashboard CTA on success", async () => {
    setLoggedIn("alice@x.io")
    mockedAccept.mockResolvedValueOnce({
      invite_id: INVITE_ID,
      tenant_id: "t-acme",
      user_id: "u-alice",
      user_email: "alice@x.io",
      role: "admin",
      status: "accepted",
      user_was_created: false,
      already_member: false,
    })
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    await waitFor(() => expect(mockedAccept).toHaveBeenCalledTimes(1))
    expect(mockedAccept).toHaveBeenCalledWith(INVITE_ID, { token: TOKEN })
    expect(
      await screen.findByTestId("invite-success-panel"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("invite-go-dashboard")).toBeInTheDocument()
  })

  it("renders the already-member headline when backend reports already_member=true", async () => {
    setLoggedIn("alice@x.io")
    mockedAccept.mockResolvedValueOnce({
      invite_id: INVITE_ID,
      tenant_id: "t-acme",
      user_id: "u-alice",
      user_email: "alice@x.io",
      role: "admin",
      status: "accepted",
      user_was_created: false,
      already_member: true,
    })
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    await waitFor(() =>
      expect(screen.getByTestId("invite-success-headline").textContent).toMatch(
        /already a member/i,
      ),
    )
  })
})

// ──────────────────────────────────────────────────────────────────
// 5. Error mapping — every backend status code surfaces a stable
//    placard testid so the UI copy can evolve without breaking tests.
// ──────────────────────────────────────────────────────────────────
function makeApiError(
  status: number,
  parsed: Record<string, unknown>,
): ApiError {
  return new ApiError({
    kind: "other",
    status,
    body: JSON.stringify(parsed),
    parsed,
    traceId: null,
    path: `/invites/${INVITE_ID}/accept`,
    method: "POST",
  })
}

describe("/invite/[token] — error mapping", () => {
  it("404 → not-found placard", async () => {
    setLoggedIn()
    mockedAccept.mockRejectedValueOnce(
      makeApiError(404, { detail: "invite not found" }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-not-found"),
    ).toBeInTheDocument()
  })

  it("403 → bad-token placard", async () => {
    setLoggedIn()
    mockedAccept.mockRejectedValueOnce(
      makeApiError(403, { detail: "token mismatch" }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-bad-token"),
    ).toBeInTheDocument()
  })

  it("410 → expired placard", async () => {
    setLoggedIn()
    mockedAccept.mockRejectedValueOnce(
      makeApiError(410, {
        detail: "expired",
        invite_id: INVITE_ID,
        current_status: "expired",
      }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-expired"),
    ).toBeInTheDocument()
  })

  it("409 with current_status=accepted → already-accepted placard", async () => {
    setLoggedIn()
    mockedAccept.mockRejectedValueOnce(
      makeApiError(409, {
        detail: "not pending",
        invite_id: INVITE_ID,
        current_status: "accepted",
      }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-already-accepted"),
    ).toBeInTheDocument()
  })

  it("409 with current_status=revoked → revoked placard", async () => {
    setLoggedIn()
    mockedAccept.mockRejectedValueOnce(
      makeApiError(409, {
        detail: "revoked",
        invite_id: INVITE_ID,
        current_status: "revoked",
      }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-revoked"),
    ).toBeInTheDocument()
  })

  it("409 with email mismatch surfaces both addresses + sign-out retry", async () => {
    setLoggedIn("alice@x.io")
    mockedAccept.mockRejectedValueOnce(
      makeApiError(409, {
        detail: "email mismatch",
        invite_email: "carol@x.io",
        session_email: "alice@x.io",
      }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-email-mismatch"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("invite-error-invite-email").textContent).toBe(
      "carol@x.io",
    )
    expect(screen.getByTestId("invite-error-session-email").textContent).toBe(
      "alice@x.io",
    )
    // Sign-out retry button must be present for logged-in callers
    expect(screen.getByTestId("invite-signout-retry")).toBeInTheDocument()
  })

  it("clicking sign-out-retry calls auth.logout()", async () => {
    const logoutSpy = vi.fn(async () => {})
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-1",
        email: "alice@x.io",
        name: "A",
        role: "operator",
        enabled: true,
        tenant_id: "t-acme",
      },
      authMode: "session",
      sessionId: "s",
      loading: false,
      error: null,
      mfaPending: null,
      login: vi.fn(),
      logout: logoutSpy,
      refresh: vi.fn(),
      submitMfa: vi.fn(),
      cancelMfa: vi.fn(),
    })
    mockedAccept.mockRejectedValueOnce(
      makeApiError(409, {
        detail: "email mismatch",
        invite_email: "carol@x.io",
        session_email: "alice@x.io",
      }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-email-mismatch"),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("invite-signout-retry"))
    await waitFor(() => expect(logoutSpy).toHaveBeenCalledTimes(1))
  })

  it("429 → rate-limited placard", async () => {
    setLoggedIn()
    mockedAccept.mockRejectedValueOnce(
      makeApiError(429, { detail: "too many failed attempts" }),
    )
    await renderPage()
    fireEvent.click(screen.getByTestId("invite-accept-btn"))
    expect(
      await screen.findByTestId("invite-error-rate-limited"),
    ).toBeInTheDocument()
  })
})

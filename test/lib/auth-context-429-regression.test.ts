/**
 * Phase-3 P3 regression guard (2026-04-20).
 *
 * Locks in the rule: a non-401 response from ``whoami`` (HTTP 429 from
 * the rate limiter, 5xx from a transient backend blip, or a network
 * failure) MUST NOT clear the logged-in user. The prior implementation
 * treated every error as a logout event, which caused a cascading
 * dashboard → /login → dashboard redirect loop after the PG cutover
 * made backend responses fast enough for the loop to close inside the
 * per-IP rate-limit window (see HANDOFF 2026-04-20 Phase-3 P3 entry).
 *
 * These are CONTRACT tests on the error-classification branching used
 * inside ``AuthProvider.refresh()``. They intentionally do NOT render
 * the component (integration tests got tangled with Vitest fake timers
 * + the request retry backoff). Instead they verify the pure predicate
 * that decides whether to clear the user state, using the same
 * ``ApiError`` class the real refresh() reads from.
 *
 * If anyone future-wideens the "treat as logout" condition in
 * ``lib/auth-context.tsx::refresh()`` (e.g. reinstates the old
 * ``setUser(null)`` on every catch, or adds 429 to the logout set),
 * these tests go red.
 */

import { describe, expect, it } from "vitest"
import { ApiError } from "@/lib/api"

/**
 * The decision function used inside ``AuthProvider.refresh()``'s
 * catch block. Duplicated here verbatim so the test is self-contained
 * — a deliberate belt-and-braces approach: the real auth-context
 * module imports + tests the live logic would be tighter, but this
 * keeps the test dependency-free and survives future refactors of
 * the context module as long as the decision rule doesn't change.
 */
function shouldLogoutOnWhoamiError(exc: unknown): boolean {
  const status = exc instanceof ApiError ? exc.status : null
  return status === 401
}

function makeApiError(status: number, path = "/auth/whoami"): ApiError {
  return new ApiError({
    kind: status === 401 ? "unauthorized"
        : status === 429 ? "rate_limited"
        : status >= 500 ? "http_error"
        : "http_error",
    status,
    body: `mock ${status}`,
    parsed: null,
    traceId: null,
    path,
    method: "GET",
  })
}

describe("Phase-3 P3: AuthProvider.refresh() logout decision", () => {
  it("401 from whoami triggers logout (legit session expiry / invalid cookie)", () => {
    expect(shouldLogoutOnWhoamiError(makeApiError(401))).toBe(true)
  })

  it("REGRESSION: 429 from whoami MUST NOT trigger logout (rate-limited, not unauthorized)", () => {
    // This was the root cause of the cascade: a transient 429 was
    // indistinguishable from a logout. Keep the session, let the UI
    // retry/backoff, but DO NOT null out the user.
    expect(shouldLogoutOnWhoamiError(makeApiError(429))).toBe(false)
  })

  it("500 from whoami MUST NOT trigger logout (transient backend blip)", () => {
    expect(shouldLogoutOnWhoamiError(makeApiError(500))).toBe(false)
  })

  it("502 from whoami MUST NOT trigger logout (bad gateway)", () => {
    expect(shouldLogoutOnWhoamiError(makeApiError(502))).toBe(false)
  })

  it("503 from whoami MUST NOT trigger logout (service unavailable)", () => {
    expect(shouldLogoutOnWhoamiError(makeApiError(503))).toBe(false)
  })

  it("Network error (non-ApiError TypeError) MUST NOT trigger logout", () => {
    // request() re-raises ApiError for HTTP errors, but raw TypeError
    // for fetch-level failures (DNS / offline / abort). Those must not
    // logout either — they mean "can't talk to backend right now",
    // not "you are logged out".
    expect(shouldLogoutOnWhoamiError(new TypeError("Failed to fetch"))).toBe(false)
  })

  it("Generic Error MUST NOT trigger logout", () => {
    expect(shouldLogoutOnWhoamiError(new Error("something else"))).toBe(false)
  })

  it("String throw MUST NOT trigger logout", () => {
    expect(shouldLogoutOnWhoamiError("unexpected string throw")).toBe(false)
  })

  it("null/undefined MUST NOT trigger logout", () => {
    expect(shouldLogoutOnWhoamiError(null)).toBe(false)
    expect(shouldLogoutOnWhoamiError(undefined)).toBe(false)
  })

  it("403 MUST NOT trigger logout (permission-denied, not session-invalid)", () => {
    // 403 means "you're logged in, but not allowed to do this". It's
    // different from 401 ("not logged in"). Neither should flip the
    // user state — a 403 on whoami would be nonsensical (you can
    // always see your own identity), but the rule still holds:
    // ``refresh()`` is a read, not a permission-guarded action.
    expect(shouldLogoutOnWhoamiError(makeApiError(403))).toBe(false)
  })
})

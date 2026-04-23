/**
 * Q.2 (#296) — SecurityAlertsCenter tests.
 *
 * The component subscribes to ``security.new_device_login`` SSE events
 * and renders an actionable toast for the matching user. The bus does
 * not yet enforce ``broadcast_scope=user`` server-side (Q.4 #298), so
 * this component MUST filter on ``data.user_id === currentUser.id``
 * before showing anything — those filter cases are part of the
 * contract here.
 *
 * Covered:
 *   1. Matching user_id → toast appears with IP + UA + buttons.
 *   2. Mismatched user_id → no toast (frontend scope guard).
 *   3. "這不是我 → 踢掉" → calls ``revokeSession(token_hint, {cascade: "not_me"})``
 *      and, on cascade success, navigates to ``/login`` with the
 *      ``reason=user_security_event&trigger=not_me_cascade`` banner.
 *   4. "是我" → dismisses without API call.
 *   5. Duplicate event (same token_hint + timestamp) → no second toast.
 *   6. No logged-in user → component is silent (defensive, doesn't
 *      subscribe before identity is known).
 *   7. Cascade failure → toast is still dismissed and we do NOT
 *      navigate, so the user can retry from /settings/security.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { render, screen, act } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  subscribeEvents: vi.fn(),
  revokeSession: vi.fn(),
}))

const authState: { user: { id: string; email: string } | null } = {
  user: { id: "u-self", email: "self@example.com" },
}
vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    user: authState.user,
    authMode: "local",
    sessionId: "sess-1",
    loading: false,
    error: null,
    mfaPending: null,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
    submitMfa: vi.fn(),
    cancelMfa: vi.fn(),
  }),
}))

import { SecurityAlertsCenter } from "@/components/omnisight/security-alerts-center"
import * as api from "@/lib/api"
import { primeSSE as _primeSSE } from "../helpers/sse"

const primeSSE = () => _primeSSE(api)

function newDeviceEvent(overrides: Partial<{
  user_id: string
  token_hint: string
  ip: string
  user_agent: string
  timestamp: string
}> = {}) {
  return {
    event: "security.new_device_login" as const,
    data: {
      user_id: overrides.user_id ?? "u-self",
      token_hint: overrides.token_hint ?? "abcd***wxyz",
      ip: overrides.ip ?? "203.0.113.42",
      user_agent: overrides.user_agent ?? "Mozilla/5.0 (TestDevice)",
      timestamp: overrides.timestamp ?? "2026-04-24T12:00:00",
    },
  }
}

describe("SecurityAlertsCenter — Q.2 new device login toast", () => {
  const originalLocation = window.location
  let assignSpy: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    authState.user = { id: "u-self", email: "self@example.com" }
    assignSpy = vi.fn()
    // Replace window.location with a spy-backed double so the cascade
    // path's full-page navigation is observable without JSDOM throwing
    // "not implemented: navigation". ``originalLocation`` is restored
    // in afterEach.
    Object.defineProperty(window, "location", {
      value: {
        ...originalLocation,
        pathname: "/",
        search: "",
        assign: assignSpy,
      },
      writable: true,
    })
  })

  afterEach(() => {
    Object.defineProperty(window, "location", {
      value: originalLocation,
      writable: true,
    })
  })

  it("renders a toast for a new-device-login event matching the current user", () => {
    const sse = primeSSE()
    render(<SecurityAlertsCenter />)
    act(() => { sse.emit(newDeviceEvent()) })
    expect(screen.getByTestId("security-alert-abcd***wxyz")).toBeInTheDocument()
    expect(screen.getByText(/203\.0\.113\.42/)).toBeInTheDocument()
    expect(screen.getByText(/TestDevice/)).toBeInTheDocument()
    expect(screen.getByText("是我")).toBeInTheDocument()
    expect(screen.getByText("這不是我 → 踢掉")).toBeInTheDocument()
  })

  it("ignores events for a different user_id (frontend scope guard)", () => {
    const sse = primeSSE()
    render(<SecurityAlertsCenter />)
    act(() => { sse.emit(newDeviceEvent({ user_id: "u-someone-else" })) })
    expect(screen.queryByTestId("security-alert-abcd***wxyz")).toBeNull()
    expect(screen.queryByTestId("security-alerts-center")).toBeNull()
  })

  it("'這不是我 → 踢掉' triggers the cascade path, dismisses the toast, and navigates to /login with the banner trigger", async () => {
    const user = userEvent.setup()
    ;(api.revokeSession as ReturnType<typeof vi.fn>).mockResolvedValue({
      status: "revoked",
      token_hint: "qwer***1234",
      cascade: "not_me",
      rotated_count: 2,
      must_change_password: true,
    })
    const sse = primeSSE()
    render(<SecurityAlertsCenter />)
    act(() => { sse.emit(newDeviceEvent({ token_hint: "qwer***1234" })) })
    await user.click(screen.getByTestId("security-alert-qwer***1234-not-me"))
    // Cascade opt-in is required — without ``{cascade: "not_me"}`` the
    // backend would only delete the single row, skipping the must-
    // change-password flip + peer rotation.
    expect(api.revokeSession).toHaveBeenCalledWith(
      "qwer***1234", { cascade: "not_me" },
    )
    expect(screen.queryByTestId("security-alert-qwer***1234")).toBeNull()
    expect(assignSpy).toHaveBeenCalledTimes(1)
    const dest = assignSpy.mock.calls[0][0] as string
    expect(dest.startsWith("/login")).toBe(true)
    expect(dest).toContain("reason=user_security_event")
    expect(dest).toContain("trigger=not_me_cascade")
  })

  it("cascade network / API failure dismisses the toast but does NOT navigate (user can retry from /settings/security)", async () => {
    const user = userEvent.setup()
    ;(api.revokeSession as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("network down"),
    )
    const sse = primeSSE()
    render(<SecurityAlertsCenter />)
    act(() => { sse.emit(newDeviceEvent({ token_hint: "fail***1234" })) })
    await user.click(screen.getByTestId("security-alert-fail***1234-not-me"))
    // Toast still clears so the user isn't stuck with a zombie alert
    // after the network blip.
    expect(screen.queryByTestId("security-alert-fail***1234")).toBeNull()
    // But we do NOT force-navigate — the server didn't confirm the
    // cascade so the tab is probably still validly logged in.
    expect(assignSpy).not.toHaveBeenCalled()
  })

  it("'是我' dismisses without calling the API or navigating", async () => {
    const user = userEvent.setup()
    const sse = primeSSE()
    render(<SecurityAlertsCenter />)
    act(() => { sse.emit(newDeviceEvent({ token_hint: "asdf***qwer" })) })
    await user.click(screen.getByTestId("security-alert-asdf***qwer-its-me"))
    expect(api.revokeSession).not.toHaveBeenCalled()
    expect(screen.queryByTestId("security-alert-asdf***qwer")).toBeNull()
    expect(assignSpy).not.toHaveBeenCalled()
  })

  it("dedupes a duplicate event with the same token_hint + timestamp", () => {
    const sse = primeSSE()
    render(<SecurityAlertsCenter />)
    const ev = newDeviceEvent({ token_hint: "dupe***hint" })
    act(() => { sse.emit(ev); sse.emit(ev) })
    expect(screen.getAllByTestId("security-alert-dupe***hint")).toHaveLength(1)
  })

  it("is silent when no user is logged in (does not subscribe yet)", () => {
    authState.user = null
    const sse = primeSSE()
    render(<SecurityAlertsCenter />)
    // The mock subscribeEvents would still be called if the component
    // subscribed unconditionally — assert it wasn't, so the future
    // listener doesn't accidentally show another user's alert during
    // the brief login transition.
    expect((api.subscribeEvents as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled()
    // Also nothing is rendered.
    expect(screen.queryByTestId("security-alerts-center")).toBeNull()
    // Sanity: emit is a no-op since no listener registered.
    expect(sse.listeners).toHaveLength(0)
  })
})

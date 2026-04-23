import { describe, it, expect, vi } from "vitest"
import { render, screen } from "@testing-library/react"

const routerReplace = vi.fn()
const stableRouter = { replace: routerReplace, push: vi.fn(), back: vi.fn() }

// ``useSearchParams`` is what the banner reads — the test swaps it in
// per case via the ``searchParamsHolder`` ref.
const searchParamsHolder: { current: URLSearchParams } = {
  current: new URLSearchParams(),
}

vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  useSearchParams: () => searchParamsHolder.current,
}))

vi.mock("@/lib/auth-context", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({
    user: null,
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

import LoginPage from "@/app/login/page"


describe("Q.1 UI follow-up — /login session-revoked banner", () => {
  it("renders the banner with trigger-specific copy when reason=user_security_event & trigger=password_change", () => {
    searchParamsHolder.current = new URLSearchParams({
      reason: "user_security_event",
      trigger: "password_change",
    })
    render(<LoginPage />)
    const banner = screen.getByTestId("login-session-revoked-banner")
    expect(banner).toBeInTheDocument()
    expect(banner.getAttribute("data-trigger")).toBe("password_change")
    expect(banner.textContent).toMatch(/password was changed/i)
    expect(banner.textContent).toMatch(/sign in again/i)
  })

  it("renders the banner with trigger copy for totp_disabled", () => {
    searchParamsHolder.current = new URLSearchParams({
      reason: "user_security_event",
      trigger: "totp_disabled",
    })
    render(<LoginPage />)
    const banner = screen.getByTestId("login-session-revoked-banner")
    expect(banner.textContent).toMatch(/two-factor authentication was disabled/i)
  })

  it("prefers the backend-supplied message over the frontend fallback", () => {
    searchParamsHolder.current = new URLSearchParams({
      reason: "user_security_event",
      trigger: "password_change",
      message: "Please re-authenticate — your credentials were rotated.",
    })
    render(<LoginPage />)
    const banner = screen.getByTestId("login-session-revoked-banner")
    expect(banner.textContent).toContain(
      "Please re-authenticate — your credentials were rotated.",
    )
    // Must NOT fall back to the default copy when a message is supplied.
    expect(banner.textContent).not.toMatch(/was changed on another device/i)
  })

  it("falls back to a generic copy when trigger is unknown", () => {
    searchParamsHolder.current = new URLSearchParams({
      reason: "user_security_event",
      trigger: "some_future_trigger_we_have_not_shipped_yet",
    })
    render(<LoginPage />)
    const banner = screen.getByTestId("login-session-revoked-banner")
    expect(banner.textContent).toMatch(/ended for security reasons/i)
  })

  it("does NOT render the banner when reason is absent", () => {
    searchParamsHolder.current = new URLSearchParams({ next: "/dashboard" })
    render(<LoginPage />)
    expect(
      screen.queryByTestId("login-session-revoked-banner"),
    ).not.toBeInTheDocument()
  })

  it("does NOT render the banner for non-security-event reasons", () => {
    searchParamsHolder.current = new URLSearchParams({
      reason: "something_unrelated",
      trigger: "password_change",
    })
    render(<LoginPage />)
    expect(
      screen.queryByTestId("login-session-revoked-banner"),
    ).not.toBeInTheDocument()
  })
})

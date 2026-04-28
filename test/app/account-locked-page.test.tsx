/**
 * AS.7.6 — Account-locked / suspended page integration test.
 *
 * Mocks `@/lib/auth-context` so the page renders deterministically
 * without a real auth-context state. Covers:
 *
 *   - Composition: AS.7.0 visual foundation + glass card + hero
 *   - Default temporary_lockout copy when no query / live state
 *   - Reason-hint dispatch (admin_suspended / security_hold)
 *   - Live `auth.lastLoginError` precedence over query reason
 *   - Live retryAfterSeconds drives the countdown
 *   - Query `?retry_after=N` fallback when no live state
 *   - Email hint forwarded into the recovery CTAs
 *   - Retry-sign-in CTA gated by countdown
 *   - Reset-password CTA hidden on admin_suspended
 *   - Contact-admin CTA mailto: href
 *   - Sign-out CTA only when an authenticated session exists
 *   - Security-hold banner rendered only on that kind
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

const mockState: {
  user: { email: string } | null
  lastLoginError: {
    accountLocked: boolean
    retryAfterSeconds: number | null
  } | null
  authMode: string | null
  sessionId: string | null
  loading: boolean
  error: string | null
  lastSignupError: unknown
  lastRequestResetError: unknown
  lastResetPasswordError: unknown
  lastMfaChallengeError: unknown
  lastEmailVerifyError: unknown
  lastResendVerifyEmailError: unknown
  mfaPending: unknown
  login: ReturnType<typeof vi.fn>
  signup: ReturnType<typeof vi.fn>
  requestPasswordReset: ReturnType<typeof vi.fn>
  resetPassword: ReturnType<typeof vi.fn>
  verifyEmail: ReturnType<typeof vi.fn>
  resendEmailVerification: ReturnType<typeof vi.fn>
  logout: ReturnType<typeof vi.fn>
  refresh: ReturnType<typeof vi.fn>
  submitMfa: ReturnType<typeof vi.fn>
  submitMfaStructured: ReturnType<typeof vi.fn>
  submitMfaWebauthn: ReturnType<typeof vi.fn>
  cancelMfa: ReturnType<typeof vi.fn>
} = {
  user: null,
  lastLoginError: null,
  authMode: null,
  sessionId: null,
  loading: false,
  error: null,
  lastSignupError: null,
  lastRequestResetError: null,
  lastResetPasswordError: null,
  lastMfaChallengeError: null,
  lastEmailVerifyError: null,
  lastResendVerifyEmailError: null,
  mfaPending: null,
  login: vi.fn(),
  signup: vi.fn(),
  requestPasswordReset: vi.fn(),
  resetPassword: vi.fn(),
  verifyEmail: vi.fn(),
  resendEmailVerification: vi.fn(),
  logout: vi.fn().mockResolvedValue(undefined),
  refresh: vi.fn(),
  submitMfa: vi.fn(),
  submitMfaStructured: vi.fn(),
  submitMfaWebauthn: vi.fn(),
  cancelMfa: vi.fn(),
}

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => mockState,
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}))

let searchParams = new URLSearchParams()
const replaceSpy = vi.fn()
const pushSpy = vi.fn()
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy, push: pushSpy }),
  useSearchParams: () => searchParams,
}))

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "dramatic",
  usePrefersReducedMotion: () => false,
}))

import AccountLockedPage from "@/app/account-locked/page"

beforeEach(() => {
  mockState.user = null
  mockState.lastLoginError = null
  mockState.error = null
  mockState.logout = vi.fn().mockResolvedValue(undefined)
  searchParams = new URLSearchParams()
  replaceSpy.mockReset()
  pushSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

describe("AS.7.6 AccountLockedPage — composition", () => {
  it("mounts with the AS.7.0 visual foundation + glass card + hero", () => {
    render(<AccountLockedPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
    expect(screen.getByTestId("as7-locked-hero")).toBeInTheDocument()
    expect(screen.getByTestId("as7-locked-body")).toBeInTheDocument()
  })

  it("default copy is temporary_lockout when no query/live state present", () => {
    render(<AccountLockedPage />)
    expect(screen.getByTestId("as7-locked-title").textContent).toBe(
      "Account temporarily locked",
    )
    expect(
      screen.getByTestId("as7-locked-body").getAttribute("data-as7-locked-kind"),
    ).toBe("temporary_lockout")
  })

  it("renders the back-to-sign-in link", () => {
    render(<AccountLockedPage />)
    const link = screen.getByTestId("as7-locked-back-link")
    expect(link.getAttribute("href")).toBe("/login")
  })
})

describe("AS.7.6 AccountLockedPage — reason-hint dispatch", () => {
  it("?reason=admin_suspended hides retry + reset, surfaces only contact-admin", () => {
    searchParams = new URLSearchParams("reason=admin_suspended")
    render(<AccountLockedPage />)
    expect(screen.getByTestId("as7-locked-title").textContent).toBe(
      "Account suspended",
    )
    expect(
      screen.queryByTestId("as7-locked-retry-signin"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByTestId("as7-locked-reset-password"),
    ).not.toBeInTheDocument()
    expect(
      screen.getByTestId("as7-locked-contact-admin"),
    ).toBeInTheDocument()
  })

  it("?reason=security_hold renders the security banner + reset path", () => {
    searchParams = new URLSearchParams("reason=security_hold")
    render(<AccountLockedPage />)
    expect(screen.getByTestId("as7-locked-title").textContent).toBe(
      "Account on hold",
    )
    expect(
      screen.getByTestId("as7-locked-security-banner"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-locked-reset-password"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("as7-locked-retry-signin"),
    ).not.toBeInTheDocument()
  })

  it("unknown reason hint falls back to temporary_lockout", () => {
    searchParams = new URLSearchParams("reason=zzz_unknown")
    render(<AccountLockedPage />)
    expect(
      screen.getByTestId("as7-locked-body").getAttribute("data-as7-locked-kind"),
    ).toBe("temporary_lockout")
  })
})

describe("AS.7.6 AccountLockedPage — countdown", () => {
  it("?retry_after=30 renders the countdown copy", () => {
    searchParams = new URLSearchParams("retry_after=30")
    render(<AccountLockedPage />)
    const countdown = screen.getByTestId("as7-locked-countdown")
    expect(countdown.getAttribute("data-as7-locked-countdown-active")).toBe(
      "yes",
    )
    expect(countdown.textContent).toMatch(/30s/)
  })

  it("countdown active disables the retry-sign-in CTA", () => {
    searchParams = new URLSearchParams("retry_after=30")
    render(<AccountLockedPage />)
    const cta = screen.getByTestId("as7-locked-retry-signin")
    expect(cta.getAttribute("data-as7-block-reason")).toBe("countdown_active")
    expect(cta.getAttribute("aria-disabled")).toBe("true")
  })

  it("retry_after=0 leaves the retry CTA enabled immediately", () => {
    searchParams = new URLSearchParams("retry_after=0")
    render(<AccountLockedPage />)
    const cta = screen.getByTestId("as7-locked-retry-signin")
    expect(cta.getAttribute("data-as7-block-reason")).toBe("ok")
    expect(cta.getAttribute("aria-disabled")).toBe("false")
  })

  it("no retry_after + no live state hides the countdown copy", () => {
    render(<AccountLockedPage />)
    expect(
      screen.queryByTestId("as7-locked-countdown"),
    ).not.toBeInTheDocument()
  })

  it("ticks down once per second", () => {
    vi.useFakeTimers()
    try {
      searchParams = new URLSearchParams("retry_after=3")
      render(<AccountLockedPage />)
      expect(
        screen.getByTestId("as7-locked-countdown").textContent,
      ).toMatch(/3s/)
      act(() => {
        vi.advanceTimersByTime(1000)
      })
      expect(
        screen.getByTestId("as7-locked-countdown").textContent,
      ).toMatch(/2s/)
      act(() => {
        vi.advanceTimersByTime(2000)
      })
      const txt = screen.getByTestId("as7-locked-countdown").textContent || ""
      expect(txt.includes("now") || txt.includes("0s")).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe("AS.7.6 AccountLockedPage — live auth state precedence", () => {
  it("live lastLoginError.accountLocked drives temporary_lockout when no hint", () => {
    mockState.lastLoginError = {
      accountLocked: true,
      retryAfterSeconds: 45,
    }
    render(<AccountLockedPage />)
    expect(
      screen.getByTestId("as7-locked-body").getAttribute("data-as7-locked-kind"),
    ).toBe("temporary_lockout")
    expect(
      screen.getByTestId("as7-locked-countdown").textContent,
    ).toMatch(/45s/)
  })

  it("query reason hint wins over live lastLoginError", () => {
    mockState.lastLoginError = {
      accountLocked: true,
      retryAfterSeconds: 45,
    }
    searchParams = new URLSearchParams("reason=admin_suspended")
    render(<AccountLockedPage />)
    expect(
      screen.getByTestId("as7-locked-body").getAttribute("data-as7-locked-kind"),
    ).toBe("admin_suspended")
  })

  it("live retryAfterSeconds wins over query retry_after", () => {
    mockState.lastLoginError = {
      accountLocked: true,
      retryAfterSeconds: 45,
    }
    searchParams = new URLSearchParams("retry_after=120")
    render(<AccountLockedPage />)
    expect(
      screen.getByTestId("as7-locked-countdown").textContent,
    ).toMatch(/45s/)
  })
})

describe("AS.7.6 AccountLockedPage — recovery CTAs", () => {
  it("retry-sign-in href forwards email + next when both present", () => {
    searchParams = new URLSearchParams(
      "retry_after=0&email=user%40example.com&next=%2Fworkspace%2F1",
    )
    render(<AccountLockedPage />)
    const cta = screen.getByTestId("as7-locked-retry-signin")
    expect(cta.getAttribute("href")).toBe(
      "/login?next=%2Fworkspace%2F1&email=user%40example.com",
    )
  })

  it("reset-password href forwards email when present", () => {
    searchParams = new URLSearchParams("email=user%40example.com")
    render(<AccountLockedPage />)
    const cta = screen.getByTestId("as7-locked-reset-password")
    expect(cta.getAttribute("href")).toBe(
      "/forgot-password?email=user%40example.com",
    )
  })

  it("contact-admin href is a mailto with the canonical default address", () => {
    render(<AccountLockedPage />)
    const cta = screen.getByTestId("as7-locked-contact-admin")
    expect(cta.getAttribute("href")?.startsWith("mailto:")).toBe(true)
    expect(cta.getAttribute("data-as7-admin-email")).toBeTruthy()
  })
})

describe("AS.7.6 AccountLockedPage — sign-out CTA", () => {
  it("hides sign-out when no user session", () => {
    render(<AccountLockedPage />)
    expect(
      screen.queryByTestId("as7-locked-sign-out"),
    ).not.toBeInTheDocument()
  })

  it("shows sign-out when an authenticated session exists", () => {
    mockState.user = { email: "user@example.com" }
    render(<AccountLockedPage />)
    expect(
      screen.getByTestId("as7-locked-sign-out"),
    ).toBeInTheDocument()
  })

  it("clicking sign-out logs out + replaces to /login", async () => {
    mockState.user = { email: "user@example.com" }
    searchParams = new URLSearchParams("next=%2Fworkspace%2F1")
    render(<AccountLockedPage />)
    await userEvent.click(screen.getByTestId("as7-locked-sign-out"))
    await waitFor(() => {
      expect(mockState.logout).toHaveBeenCalled()
    })
    await waitFor(() => {
      expect(replaceSpy).toHaveBeenCalledWith(
        "/login?next=%2Fworkspace%2F1",
      )
    })
  })
})

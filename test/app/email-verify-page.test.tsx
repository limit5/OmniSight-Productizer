/**
 * AS.7.5 — Email verification page integration test.
 *
 * Mocks `@/lib/auth-context` so the page renders deterministically
 * without a real backend round-trip. Covers:
 *
 *   - Composition: AS.7.0 visual foundation + glass card
 *   - Idle stage (no token): renders the resend form
 *   - Verifying stage: shown immediately when ?token= present
 *   - Verified stage: VerifiedCard rendered on auth.verifyEmail ok
 *   - Already-verified branch routes to AlreadyVerifiedCard
 *   - Expired-token failure: ResendForm with expired-banner
 *   - Invalid-token failure: ResendForm with invalid-banner
 *   - Resend submit threads email + bumps to LinkResentCard
 *   - Resend gate disabled until email is plausible
 *   - Pre-fill ?email= drives the resend form's initial input
 *   - Envelope idle motion data attribute is on
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  cleanup,
  render,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react"
import userEvent from "@testing-library/user-event"

const mockState: {
  user: unknown
  authMode: string | null
  sessionId: string | null
  loading: boolean
  error: string | null
  lastLoginError: unknown
  lastSignupError: unknown
  lastRequestResetError: unknown
  lastResetPasswordError: unknown
  lastMfaChallengeError: unknown
  lastEmailVerifyError:
    | { kind: string; message: string; retryAfterSeconds: number | null }
    | null
  lastResendVerifyEmailError:
    | { kind: string; message: string; retryAfterSeconds: number | null }
    | null
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
  authMode: null,
  sessionId: null,
  loading: false,
  error: null,
  lastLoginError: null,
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
  verifyEmail: vi.fn().mockResolvedValue({
    status: "ok",
    error: null,
    email: "user@example.com",
  }),
  resendEmailVerification: vi.fn().mockResolvedValue({
    status: "linkSent",
    error: null,
    email: "user@example.com",
  }),
  logout: vi.fn(),
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
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy, push: vi.fn() }),
  useSearchParams: () => searchParams,
}))

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "dramatic",
  usePrefersReducedMotion: () => false,
}))

import EmailVerifyPage from "@/app/email-verify/page"

beforeEach(() => {
  mockState.user = null
  mockState.error = null
  mockState.lastEmailVerifyError = null
  mockState.lastResendVerifyEmailError = null
  mockState.verifyEmail = vi.fn().mockResolvedValue({
    status: "ok",
    error: null,
    email: "user@example.com",
  })
  mockState.resendEmailVerification = vi.fn().mockResolvedValue({
    status: "linkSent",
    error: null,
    email: "user@example.com",
  })
  searchParams = new URLSearchParams()
  replaceSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

describe("AS.7.5 EmailVerifyPage — composition (idle)", () => {
  it("mounts with the AS.7.0 visual foundation + glass card", () => {
    render(<EmailVerifyPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
  })

  it("renders the resend form when no token is present", () => {
    render(<EmailVerifyPage />)
    expect(
      screen.getByTestId("as7-verify-resend-form"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("as7-verify-envelope")).toBeInTheDocument()
  })

  it("envelope icon emits idle motion data attribute on", () => {
    render(<EmailVerifyPage />)
    const envs = screen.getAllByTestId("as7-verify-envelope")
    expect(envs.length).toBeGreaterThan(0)
    expect(envs[0].getAttribute("data-as7-envelope-idle")).toBe("on")
  })

  it("pre-fills the email from ?email= when present", () => {
    searchParams = new URLSearchParams("email=hello%40example.com")
    render(<EmailVerifyPage />)
    const input = screen.getByLabelText("EMAIL") as HTMLInputElement
    expect(input.value).toBe("hello@example.com")
  })

  it("includes a back-to-sign-in link", () => {
    render(<EmailVerifyPage />)
    const link = screen.getByTestId("as7-verify-back-link")
    expect(link.getAttribute("href")).toBe("/login")
  })
})

describe("AS.7.5 EmailVerifyPage — verifying stage (?token= present)", () => {
  it("renders VerifyingCard before verifyEmail resolves", async () => {
    searchParams = new URLSearchParams("token=abc.def.ghi")
    let resolve!: (v: unknown) => void
    mockState.verifyEmail = vi.fn().mockReturnValue(
      new Promise((r) => {
        resolve = r
      }),
    )
    render(<EmailVerifyPage />)
    expect(screen.getByTestId("as7-verify-verifying")).toBeInTheDocument()

    // Resolve so the test cleanup finishes; we don't assert outcome here.
    resolve({
      status: "ok",
      error: null,
      email: "user@example.com",
    })
    await waitFor(() => {
      expect(
        screen.queryByTestId("as7-verify-verifying"),
      ).not.toBeInTheDocument()
    })
  })

  it("calls auth.verifyEmail with the token from the URL on mount", async () => {
    searchParams = new URLSearchParams("token=abc.def.ghi")
    render(<EmailVerifyPage />)
    await waitFor(() => {
      expect(mockState.verifyEmail).toHaveBeenCalled()
    })
    const [body] = mockState.verifyEmail.mock.calls[0] as [
      { token: string },
    ]
    expect(body.token).toBe("abc.def.ghi")
  })
})

describe("AS.7.5 EmailVerifyPage — verified stage (ok outcome)", () => {
  it("swaps in VerifiedCard with the verified email", async () => {
    searchParams = new URLSearchParams("token=abc.def.ghi")
    mockState.verifyEmail = vi.fn().mockResolvedValue({
      status: "ok",
      error: null,
      email: "user@example.com",
    })
    render(<EmailVerifyPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-verify-success"),
      ).toBeInTheDocument()
    })
    expect(screen.getByText("user@example.com")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-verify-signin-link").getAttribute("href"),
    ).toBe("/login")
  })

  it("forwards ?next= into the sign-in CTA when present", async () => {
    searchParams = new URLSearchParams(
      "token=abc.def.ghi&next=%2Fworkspace%2F1",
    )
    mockState.verifyEmail = vi.fn().mockResolvedValue({
      status: "ok",
      error: null,
      email: "user@example.com",
    })
    render(<EmailVerifyPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-verify-success")).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("as7-verify-signin-link").getAttribute("href"),
    ).toBe("/login?next=%2Fworkspace%2F1")
  })
})

describe("AS.7.5 EmailVerifyPage — failed stages", () => {
  it("expired_token branch surfaces the resend form with expired banner", async () => {
    searchParams = new URLSearchParams("token=abc.def.ghi")
    const failure = {
      kind: "expired_token",
      message:
        "This verification link has expired. Request a fresh link below — we'll send a new one to your email.",
      retryAfterSeconds: null,
    }
    mockState.verifyEmail = vi.fn().mockImplementation(async () => {
      mockState.lastEmailVerifyError = failure
      return { status: "failed", error: failure, email: null }
    })
    render(<EmailVerifyPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-verify-resend-form"),
      ).toBeInTheDocument()
    })
    const banner = screen.getByTestId("as7-verify-failure-banner")
    expect(banner.getAttribute("data-as7-failure-kind")).toBe(
      "expired_token",
    )
    expect(banner.textContent).toMatch(/expired/i)
  })

  it("invalid_token branch surfaces the resend form with invalid banner", async () => {
    searchParams = new URLSearchParams("token=abc.def.ghi")
    const failure = {
      kind: "invalid_token",
      message:
        "This verification link is no longer valid. Request a fresh link below to continue.",
      retryAfterSeconds: null,
    }
    mockState.verifyEmail = vi.fn().mockImplementation(async () => {
      mockState.lastEmailVerifyError = failure
      return { status: "failed", error: failure, email: null }
    })
    render(<EmailVerifyPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-verify-resend-form"),
      ).toBeInTheDocument()
    })
    const banner = screen.getByTestId("as7-verify-failure-banner")
    expect(banner.getAttribute("data-as7-failure-kind")).toBe(
      "invalid_token",
    )
    expect(banner.textContent).toMatch(/no longer valid/i)
  })

  it("already_verified branch routes to the AlreadyVerifiedCard", async () => {
    searchParams = new URLSearchParams("token=abc.def.ghi")
    const failure = {
      kind: "already_verified",
      message: "This email is already verified. You can sign in now.",
      retryAfterSeconds: null,
    }
    mockState.verifyEmail = vi.fn().mockImplementation(async () => {
      mockState.lastEmailVerifyError = failure
      return { status: "failed", error: failure, email: null }
    })
    render(<EmailVerifyPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-verify-already-verified"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("as7-verify-signin-link").getAttribute("href"),
    ).toBe("/login")
  })
})

describe("AS.7.5 EmailVerifyPage — resend submit gate", () => {
  it("submit is disabled until email is plausible", async () => {
    render(<EmailVerifyPage />)
    const submit = screen.getByTestId(
      "as7-verify-resend-submit",
    ) as HTMLButtonElement
    expect(submit).toBeDisabled()
    expect(submit.getAttribute("data-as7-block-reason")).toBe(
      "email_invalid",
    )
    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )
    await waitFor(() => {
      expect(submit.getAttribute("data-as7-block-reason")).toBe("ok")
      expect(submit).not.toBeDisabled()
    })
  })
})

describe("AS.7.5 EmailVerifyPage — resend submit flow", () => {
  it("submit calls auth.resendEmailVerification with the typed email", async () => {
    render(<EmailVerifyPage />)
    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )
    fireEvent.submit(screen.getByTestId("as7-verify-resend-form"))
    await waitFor(() =>
      expect(mockState.resendEmailVerification).toHaveBeenCalled(),
    )
    const [emailArg] = mockState.resendEmailVerification.mock.calls[0] as [
      string,
    ]
    expect(emailArg).toBe("user@example.com")
  })

  it("linkSent outcome swaps in the LinkResentCard", async () => {
    mockState.resendEmailVerification = vi.fn().mockResolvedValue({
      status: "linkSent",
      error: null,
      email: "user@example.com",
    })
    render(<EmailVerifyPage />)
    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )
    fireEvent.submit(screen.getByTestId("as7-verify-resend-form"))
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-verify-link-resent"),
      ).toBeInTheDocument()
    })
    expect(screen.getByText("user@example.com")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-verify-back-to-login").getAttribute("href"),
    ).toBe("/login")
  })

  it("failed outcome surfaces the canonical resend error banner", async () => {
    const failure = {
      kind: "rate_limited",
      message:
        "Too many requests. Please wait a few minutes and retry.",
      retryAfterSeconds: 30,
    }
    mockState.resendEmailVerification = vi.fn().mockImplementation(async () => {
      mockState.lastResendVerifyEmailError = failure
      return { status: "failed", error: failure, email: null }
    })
    render(<EmailVerifyPage />)
    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )
    fireEvent.submit(screen.getByTestId("as7-verify-resend-form"))
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-verify-resend-error"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText(
        "Too many requests. Please wait a few minutes and retry.",
      ),
    ).toBeInTheDocument()
  })
})

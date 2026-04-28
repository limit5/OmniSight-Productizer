/**
 * AS.7.3 — Forgot-password page integration test.
 *
 * Mocks `@/lib/auth-context` so the page renders deterministically
 * without a real backend round-trip. Covers:
 *
 *   - Page mounts with the AS.7.0 visual foundation + glass card
 *   - Brand wordmark + email field render
 *   - Honeypot field renders + pending → ready transition
 *   - Submit gate disabled until the gate cascade clears
 *   - Submit threads honeypot key into auth.requestPasswordReset extras
 *   - linkSent outcome swaps in the LinkSentCard
 *   - failed outcome surfaces the canonical error banner
 *   - OAuth-only branch surfaces the OAuth-only specific message
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
  lastLoginError: { accountLocked: boolean; retryAfterSeconds: number | null } | null
  lastSignupError: { kind: string; message: string; retryAfterSeconds: number | null } | null
  lastRequestResetError: { kind: string; message: string; retryAfterSeconds: number | null } | null
  lastResetPasswordError: { kind: string; message: string; retryAfterSeconds: number | null } | null
  mfaPending: { mfa_token: string; mfa_methods: string[]; email: string } | null
  login: ReturnType<typeof vi.fn>
  signup: ReturnType<typeof vi.fn>
  requestPasswordReset: ReturnType<typeof vi.fn>
  resetPassword: ReturnType<typeof vi.fn>
  logout: ReturnType<typeof vi.fn>
  refresh: ReturnType<typeof vi.fn>
  submitMfa: ReturnType<typeof vi.fn>
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
  mfaPending: null,
  login: vi.fn().mockResolvedValue(true),
  signup: vi.fn(),
  requestPasswordReset: vi.fn().mockResolvedValue({
    status: "linkSent",
    error: null,
    email: "user@example.com",
  }),
  resetPassword: vi.fn(),
  logout: vi.fn(),
  refresh: vi.fn(),
  submitMfa: vi.fn(),
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

import ForgotPasswordPage from "@/app/forgot-password/page"

beforeEach(() => {
  mockState.user = null
  mockState.error = null
  mockState.lastRequestResetError = null
  mockState.requestPasswordReset = vi.fn().mockResolvedValue({
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

describe("AS.7.3 ForgotPasswordPage — composition", () => {
  it("mounts with the AS.7.0 visual foundation root", () => {
    render(<ForgotPasswordPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
  })

  it("renders the brand wordmark + email field", () => {
    render(<ForgotPasswordPage />)
    expect(screen.getByTestId("as7-wordmark")).toBeInTheDocument()
    expect(screen.getByTestId("as7-field-email")).toBeInTheDocument()
  })

  it("renders the honeypot field initially pending then ready", async () => {
    render(<ForgotPasswordPage />)
    const f = screen.getByTestId("as7-honeypot-field")
    expect(f).toBeInTheDocument()
    await waitFor(() => {
      expect(f.getAttribute("data-as7-honeypot")).toBe("ready")
    })
  })

  it("Turnstile widget only renders when NEXT_PUBLIC_TURNSTILE_SITE_KEY is set", () => {
    render(<ForgotPasswordPage />)
    expect(screen.queryByTestId("as7-turnstile-widget")).toBeNull()
  })

  it("pre-fills email when ?email= is present", () => {
    searchParams = new URLSearchParams("email=user%40example.com")
    render(<ForgotPasswordPage />)
    const input = screen.getByLabelText("EMAIL") as HTMLInputElement
    expect(input.value).toBe("user@example.com")
  })

  it("includes a Back-to-sign-in link", () => {
    render(<ForgotPasswordPage />)
    const link = screen.getByTestId("as7-forgot-back-link")
    expect(link.getAttribute("href")).toBe("/login")
  })
})

describe("AS.7.3 ForgotPasswordPage — submit gate", () => {
  it("submit is disabled until honeypot resolves and email is valid", async () => {
    render(<ForgotPasswordPage />)
    const submit = screen.getByTestId("as7-forgot-submit") as HTMLButtonElement
    expect(submit).toBeDisabled()

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    // Empty email — gate should now read email_invalid.
    expect(submit.getAttribute("data-as7-block-reason")).toBe("email_invalid")

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

describe("AS.7.3 ForgotPasswordPage — submit flow", () => {
  it("submit threads honeypot key into requestPasswordReset extras", async () => {
    render(<ForgotPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )

    fireEvent.submit(screen.getByTestId("as7-forgot-form"))

    await waitFor(() =>
      expect(mockState.requestPasswordReset).toHaveBeenCalled(),
    )

    const [emailArg, extras] = mockState.requestPasswordReset.mock.calls[0] as [
      string,
      Record<string, string>,
    ]
    expect(emailArg).toBe("user@example.com")
    const honeypotKey = Object.keys(extras).find((k) => k.startsWith("pr_"))
    expect(honeypotKey).toBeDefined()
    expect(extras[honeypotKey!]).toBe("")
  })

  it("linkSent outcome swaps in the LinkSentCard", async () => {
    mockState.requestPasswordReset = vi.fn().mockResolvedValue({
      status: "linkSent",
      error: null,
      email: "user@example.com",
    })
    render(<ForgotPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )
    fireEvent.submit(screen.getByTestId("as7-forgot-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-forgot-link-sent")).toBeInTheDocument()
    })
    expect(screen.getByText("user@example.com")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-forgot-back-to-login").getAttribute("href"),
    ).toBe("/login")
  })

  it("failed outcome surfaces the canonical error banner", async () => {
    mockState.requestPasswordReset = vi.fn().mockResolvedValue({
      status: "failed",
      error: {
        kind: "rate_limited",
        message: "Too many requests. Please wait a few minutes and retry.",
        retryAfterSeconds: 30,
      },
      email: null,
    })
    mockState.lastRequestResetError = {
      kind: "rate_limited",
      message: "Too many requests. Please wait a few minutes and retry.",
      retryAfterSeconds: 30,
    }
    render(<ForgotPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )
    fireEvent.submit(screen.getByTestId("as7-forgot-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-forgot-error")).toBeInTheDocument()
    })
    expect(
      screen.getByText(
        "Too many requests. Please wait a few minutes and retry.",
      ),
    ).toBeInTheDocument()
  })

  it("OAuth-only outcome surfaces the OAuth-specific message", async () => {
    mockState.requestPasswordReset = vi.fn().mockResolvedValue({
      status: "failed",
      error: {
        kind: "email_oauth_only",
        message:
          "This account signs in with a connected provider (Google, GitHub, etc.). Password reset does not apply — open the sign-in page and click your provider button.",
        retryAfterSeconds: null,
      },
      email: null,
    })
    mockState.lastRequestResetError = {
      kind: "email_oauth_only",
      message:
        "This account signs in with a connected provider (Google, GitHub, etc.). Password reset does not apply — open the sign-in page and click your provider button.",
      retryAfterSeconds: null,
    }
    render(<ForgotPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    await userEvent.type(
      screen.getByLabelText("EMAIL") as HTMLInputElement,
      "user@example.com",
    )
    fireEvent.submit(screen.getByTestId("as7-forgot-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-forgot-error")).toBeInTheDocument()
    })
    expect(
      screen.getByText(/connected provider \(Google, GitHub/i),
    ).toBeInTheDocument()
  })
})

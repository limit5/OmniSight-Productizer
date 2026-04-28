/**
 * AS.7.3 — Reset-password page integration test.
 *
 * Mocks `@/lib/auth-context` so the page renders deterministically
 * without a real backend round-trip. Covers:
 *
 *   - Token-missing branch renders the TokenMissingCard
 *   - Token-present branch renders the form with all primitives
 *   - Auto-fills a generated password on first mount
 *   - Submit gate disabled until every gate clears
 *   - Submit threads token + honeypot key into auth.resetPassword
 *   - Success outcome swaps in the ResetSuccessCard
 *   - invalid_token / expired_token outcomes swap in TokenFailureCard
 *   - weak_password outcome surfaces inline error banner (form stays)
 *   - Style toggle re-rolls password
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
  login: vi.fn(),
  signup: vi.fn(),
  requestPasswordReset: vi.fn(),
  resetPassword: vi.fn().mockResolvedValue({
    status: "ok",
    error: null,
    email: "user@example.com",
  }),
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

import ResetPasswordPage from "@/app/reset-password/page"

beforeEach(() => {
  mockState.user = null
  mockState.error = null
  mockState.lastResetPasswordError = null
  mockState.resetPassword = vi.fn().mockResolvedValue({
    status: "ok",
    error: null,
    email: "user@example.com",
  })
  // Default = token present. Individual tests can override.
  searchParams = new URLSearchParams("token=valid.reset.token.here")
  replaceSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

describe("AS.7.3 ResetPasswordPage — token-missing branch", () => {
  it("renders the TokenMissingCard when ?token= is absent", () => {
    searchParams = new URLSearchParams()
    render(<ResetPasswordPage />)
    expect(screen.getByTestId("as7-reset-token-missing")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-reset-request-fresh-link").getAttribute("href"),
    ).toBe("/forgot-password")
  })

  it("renders the TokenMissingCard when ?token= is empty / whitespace", () => {
    searchParams = new URLSearchParams("token=   ")
    render(<ResetPasswordPage />)
    expect(screen.getByTestId("as7-reset-token-missing")).toBeInTheDocument()
  })
})

describe("AS.7.3 ResetPasswordPage — composition", () => {
  it("mounts with the AS.7.0 visual foundation root + form", () => {
    render(<ResetPasswordPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
    expect(screen.getByTestId("as7-reset-form")).toBeInTheDocument()
  })

  it("renders the password block + style toggle + strength meter", () => {
    render(<ResetPasswordPage />)
    expect(screen.getByTestId("as7-reset-password-block")).toBeInTheDocument()
    expect(screen.getByTestId("as7-password-style-toggle")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-password-strength-meter"),
    ).toBeInTheDocument()
  })

  it("renders the honeypot field with pending → ready transition", async () => {
    render(<ResetPasswordPage />)
    const f = screen.getByTestId("as7-honeypot-field")
    expect(f).toBeInTheDocument()
    await waitFor(() => {
      expect(f.getAttribute("data-as7-honeypot")).toBe("ready")
    })
  })

  it("auto-fills a generated password on first mount", () => {
    render(<ResetPasswordPage />)
    expect(screen.getByTestId("as7-style-random")).toHaveAttribute(
      "data-as7-style-active",
      "yes",
    )
    const passwordInput = screen.getByLabelText(
      "PASSWORD",
    ) as HTMLInputElement
    expect(passwordInput.value.length).toBeGreaterThanOrEqual(12)
  })
})

describe("AS.7.3 ResetPasswordPage — submit gate", () => {
  it("submit is disabled until every gate clears", () => {
    render(<ResetPasswordPage />)
    const submit = screen.getByTestId("as7-reset-submit") as HTMLButtonElement
    // honeypot still pending OR save-ack not checked → blocked.
    expect(submit).toBeDisabled()
  })

  it("clears block reasons in order: honeypot → password_not_saved → ok", async () => {
    render(<ResetPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    const submit = screen.getByTestId("as7-reset-submit") as HTMLButtonElement

    // Auto-filled password should pass strength → blocking on save-ack.
    await waitFor(() => {
      expect(submit.getAttribute("data-as7-block-reason")).toBe(
        "password_not_saved",
      )
    })

    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    await waitFor(() => {
      expect(submit.getAttribute("data-as7-block-reason")).toBe("ok")
      expect(submit).not.toBeDisabled()
    })
  })
})

describe("AS.7.3 ResetPasswordPage — submit flow", () => {
  it("submit threads token + honeypot key into auth.resetPassword extras", async () => {
    render(<ResetPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.submit(screen.getByTestId("as7-reset-form"))

    await waitFor(() => expect(mockState.resetPassword).toHaveBeenCalled())

    const [body, extras] = mockState.resetPassword.mock.calls[0] as [
      Record<string, unknown>,
      Record<string, string>,
    ]
    expect(body.token).toBe("valid.reset.token.here")
    expect(typeof body.password).toBe("string")
    expect(String(body.password).length).toBeGreaterThanOrEqual(12)
    const honeypotKey = Object.keys(extras).find((k) => k.startsWith("pr_"))
    expect(honeypotKey).toBeDefined()
    expect(extras[honeypotKey!]).toBe("")
  })

  it("ok outcome swaps in the ResetSuccessCard", async () => {
    mockState.resetPassword = vi.fn().mockResolvedValue({
      status: "ok",
      error: null,
      email: "user@example.com",
    })
    render(<ResetPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.submit(screen.getByTestId("as7-reset-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-reset-success")).toBeInTheDocument()
    })
    expect(screen.getByText("user@example.com")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-reset-go-login").getAttribute("href"),
    ).toBe("/login")
  })

  it("invalid_token outcome swaps in TokenFailureCard", async () => {
    // Mutate mockState.lastResetPasswordError inside the mock so the
    // page's first render shows the form (no pre-set error), then
    // submit transitions to the failure card on the next render
    // triggered by setErrorKey in the page's submit handler.
    const errorPayload = {
      kind: "invalid_token",
      message:
        "This reset link is no longer valid. Please request a fresh link from the sign-in page.",
      retryAfterSeconds: null,
    }
    mockState.resetPassword = vi.fn().mockImplementation(async () => {
      mockState.lastResetPasswordError = errorPayload
      return {
        status: "failed",
        error: errorPayload,
        email: null,
      }
    })
    render(<ResetPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.submit(screen.getByTestId("as7-reset-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-reset-token-failure")).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("as7-reset-request-fresh-link").getAttribute("href"),
    ).toBe("/forgot-password")
  })

  it("expired_token outcome also swaps in TokenFailureCard", async () => {
    const errorPayload = {
      kind: "expired_token",
      message:
        "This reset link has expired. Please request a fresh link from the sign-in page.",
      retryAfterSeconds: null,
    }
    mockState.resetPassword = vi.fn().mockImplementation(async () => {
      mockState.lastResetPasswordError = errorPayload
      return {
        status: "failed",
        error: errorPayload,
        email: null,
      }
    })
    render(<ResetPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.submit(screen.getByTestId("as7-reset-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-reset-token-failure")).toBeInTheDocument()
    })
    expect(screen.getByText(/has expired/i)).toBeInTheDocument()
  })

  it("weak_password outcome surfaces inline error (form stays mounted)", async () => {
    const errorPayload = {
      kind: "weak_password",
      message:
        "This password does not meet the strength requirements. Try a longer or more random one.",
      retryAfterSeconds: null,
    }
    mockState.resetPassword = vi.fn().mockImplementation(async () => {
      mockState.lastResetPasswordError = errorPayload
      return {
        status: "failed",
        error: errorPayload,
        email: null,
      }
    })
    render(<ResetPasswordPage />)

    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.submit(screen.getByTestId("as7-reset-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-reset-error")).toBeInTheDocument()
    })
    expect(screen.getByTestId("as7-reset-form")).toBeInTheDocument()
  })
})

describe("AS.7.3 ResetPasswordPage — style toggle re-rolls", () => {
  it("toggling style fires a re-roll (password input value changes)", async () => {
    render(<ResetPasswordPage />)
    const passwordInput = screen.getByLabelText(
      "PASSWORD",
    ) as HTMLInputElement
    const initial = passwordInput.value
    expect(initial.length).toBeGreaterThanOrEqual(12)

    fireEvent.click(screen.getByTestId("as7-style-diceware"))

    await waitFor(() => {
      expect(passwordInput.value).not.toBe(initial)
    })
    expect(passwordInput.value).toMatch(/-/)
    // The "saved" ack is also reset on re-roll.
    expect(
      (screen.getByTestId("as7-save-ack-input") as HTMLInputElement).checked,
    ).toBe(false)
  })
})

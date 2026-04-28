/**
 * AS.7.4 — MFA challenge dedicated page integration test.
 *
 * Mocks `@/lib/auth-context` so the page renders deterministically
 * without a real backend round-trip. Covers:
 *
 *   - No-challenge branch redirects to /login
 *   - mfaPending → page renders form + tabs + TOTP cell pulse
 *   - Backend offered ["totp"] → 2 tabs (totp + backup_code)
 *   - Backend offered ["totp", "webauthn"] → 3 tabs
 *   - TOTP submit threads the digit string into auth.submitMfaStructured
 *   - TOTP success outcome plays passed-check + navigates
 *   - WebAuthn tab calls auth.submitMfaWebauthn on click
 *   - expired_challenge outcome bounces back to /login + cancels
 *   - Cancel button cancels mfa state + navigates back to /login
 *   - Submit button gated on TOTP code format
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
  lastMfaChallengeError: { kind: string; message: string; retryAfterSeconds: number | null } | null
  mfaPending: { mfa_token: string; mfa_methods: string[]; email: string } | null
  login: ReturnType<typeof vi.fn>
  signup: ReturnType<typeof vi.fn>
  requestPasswordReset: ReturnType<typeof vi.fn>
  resetPassword: ReturnType<typeof vi.fn>
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
  mfaPending: null,
  login: vi.fn(),
  signup: vi.fn(),
  requestPasswordReset: vi.fn(),
  resetPassword: vi.fn(),
  logout: vi.fn(),
  refresh: vi.fn(),
  submitMfa: vi.fn(),
  submitMfaStructured: vi.fn().mockResolvedValue({ status: "ok", error: null }),
  submitMfaWebauthn: vi.fn().mockResolvedValue({ status: "ok", error: null }),
  cancelMfa: vi.fn(),
}

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => mockState,
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}))

const replaceSpy = vi.fn()
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}))

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "dramatic",
  usePrefersReducedMotion: () => false,
}))

import MfaChallengePage from "@/app/mfa-challenge/page"

beforeEach(() => {
  mockState.user = null
  mockState.error = null
  mockState.lastMfaChallengeError = null
  mockState.mfaPending = null
  mockState.submitMfaStructured = vi
    .fn()
    .mockResolvedValue({ status: "ok", error: null })
  mockState.submitMfaWebauthn = vi
    .fn()
    .mockResolvedValue({ status: "ok", error: null })
  mockState.cancelMfa = vi.fn()
  replaceSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

describe("AS.7.4 MfaChallengePage — composition", () => {
  it("mounts the AS.7.0 visual foundation root + glass card", () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp", "webauthn"],
      email: "ops@example.com",
    }
    render(<MfaChallengePage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
    expect(screen.getByTestId("as7-mfa-challenge-form")).toBeInTheDocument()
  })

  it("renders 3 tabs when backend offered totp + webauthn", () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp", "webauthn"],
      email: "ops@example.com",
    }
    render(<MfaChallengePage />)
    expect(screen.getByTestId("as7-mfa-tab-totp")).toBeInTheDocument()
    expect(screen.getByTestId("as7-mfa-tab-webauthn")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-mfa-tab-backup_code"),
    ).toBeInTheDocument()
  })

  it("renders 2 tabs when backend only offered totp (totp + backup_code)", () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    render(<MfaChallengePage />)
    expect(screen.getByTestId("as7-mfa-tab-totp")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-mfa-tab-backup_code"),
    ).toBeInTheDocument()
    expect(screen.queryByTestId("as7-mfa-tab-webauthn")).toBeNull()
  })

  it("hides the tab pill entirely when only one method is offered", () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["webauthn"],
      email: "ops@example.com",
    }
    render(<MfaChallengePage />)
    expect(screen.queryByTestId("as7-mfa-method-tabs")).toBeNull()
    expect(screen.getByTestId("as7-mfa-panel-webauthn")).toBeInTheDocument()
  })

  it("shows the email of the pending challenge", () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    render(<MfaChallengePage />)
    expect(screen.getByText("ops@example.com")).toBeInTheDocument()
  })
})

describe("AS.7.4 MfaChallengePage — no-challenge branch", () => {
  it("redirects to /login when mfaPending is null", async () => {
    mockState.mfaPending = null
    render(<MfaChallengePage />)
    await waitFor(() => {
      expect(replaceSpy).toHaveBeenCalledWith("/login?next=%2F")
    })
  })

  it("renders a holding state body before the redirect fires", () => {
    mockState.mfaPending = null
    render(<MfaChallengePage />)
    expect(screen.getByTestId("as7-mfa-no-challenge")).toBeInTheDocument()
  })
})

describe("AS.7.4 MfaChallengePage — submit gate", () => {
  it("submit button disabled until TOTP value is 6 digits", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    render(<MfaChallengePage />)
    const submit = screen.getByTestId("as7-mfa-submit") as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    expect(submit.getAttribute("data-as7-block-reason")).toBe("code_invalid")
    const input = screen.getByTestId(
      "as7-mfa-totp-input",
    ) as HTMLInputElement
    await userEvent.type(input, "123456")
    expect(submit.disabled).toBe(false)
    expect(submit.getAttribute("data-as7-block-reason")).toBe("ok")
  })
})

describe("AS.7.4 MfaChallengePage — TOTP submit", () => {
  it("submit threads the typed code into auth.submitMfaStructured", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    mockState.submitMfaStructured = vi
      .fn()
      .mockResolvedValue({ status: "ok", error: null })
    render(<MfaChallengePage />)
    const input = screen.getByTestId("as7-mfa-totp-input")
    await userEvent.type(input, "123456")
    fireEvent.submit(screen.getByTestId("as7-mfa-challenge-form"))
    await waitFor(() => {
      expect(mockState.submitMfaStructured).toHaveBeenCalledWith("123456")
    })
  })

  it("non-digit characters are stripped from the TOTP value", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    mockState.submitMfaStructured = vi
      .fn()
      .mockResolvedValue({ status: "ok", error: null })
    render(<MfaChallengePage />)
    const input = screen.getByTestId(
      "as7-mfa-totp-input",
    ) as HTMLInputElement
    await userEvent.type(input, "1a2b3c4d5e6f")
    expect(input.value).toBe("123456")
  })

  it("expired_challenge outcome bounces to /login + cancels", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    const cancelSpy = vi.fn()
    mockState.cancelMfa = cancelSpy
    mockState.submitMfaStructured = vi.fn().mockResolvedValue({
      status: "failed",
      error: {
        kind: "expired_challenge",
        message: "expired",
        retryAfterSeconds: null,
      },
    })
    render(<MfaChallengePage />)
    const input = screen.getByTestId("as7-mfa-totp-input")
    await userEvent.type(input, "123456")
    fireEvent.submit(screen.getByTestId("as7-mfa-challenge-form"))
    await waitFor(() => {
      expect(cancelSpy).toHaveBeenCalled()
      expect(replaceSpy).toHaveBeenCalledWith("/login?next=%2F")
    })
  })

  it("invalid_code outcome stays on page + clears input + surfaces banner", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    mockState.submitMfaStructured = vi.fn().mockResolvedValue({
      status: "failed",
      error: {
        kind: "invalid_code",
        message: "bad code",
        retryAfterSeconds: null,
      },
    })
    mockState.lastMfaChallengeError = {
      kind: "invalid_code",
      message: "That code is not valid. Double-check the digits and try again.",
      retryAfterSeconds: null,
    }
    render(<MfaChallengePage />)
    const input = screen.getByTestId(
      "as7-mfa-totp-input",
    ) as HTMLInputElement
    await userEvent.type(input, "123456")
    fireEvent.submit(screen.getByTestId("as7-mfa-challenge-form"))
    await waitFor(() => {
      expect(input.value).toBe("")
    })
    expect(screen.getByTestId("as7-mfa-error")).toHaveTextContent(
      "That code is not valid",
    )
    expect(replaceSpy).not.toHaveBeenCalled()
  })
})

describe("AS.7.4 MfaChallengePage — WebAuthn", () => {
  it("clicking webauthn tab + button calls auth.submitMfaWebauthn", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["webauthn"],
      email: "ops@example.com",
    }
    mockState.submitMfaWebauthn = vi
      .fn()
      .mockResolvedValue({ status: "ok", error: null })
    render(<MfaChallengePage />)
    fireEvent.click(screen.getByTestId("as7-mfa-webauthn-go"))
    await waitFor(() => {
      expect(mockState.submitMfaWebauthn).toHaveBeenCalled()
    })
  })

  it("webauthn_failed outcome stays on page + surfaces banner", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["webauthn"],
      email: "ops@example.com",
    }
    mockState.submitMfaWebauthn = vi.fn().mockResolvedValue({
      status: "failed",
      error: {
        kind: "webauthn_failed",
        message: "wa fail",
        retryAfterSeconds: null,
      },
    })
    mockState.lastMfaChallengeError = {
      kind: "webauthn_failed",
      message:
        "Security-key verification did not complete. Please try again or pick another method.",
      retryAfterSeconds: null,
    }
    render(<MfaChallengePage />)
    fireEvent.click(screen.getByTestId("as7-mfa-webauthn-go"))
    await waitFor(() => {
      expect(screen.getByTestId("as7-mfa-error")).toHaveTextContent(
        "Security-key verification did not complete",
      )
    })
    expect(replaceSpy).not.toHaveBeenCalled()
  })
})

describe("AS.7.4 MfaChallengePage — cancel", () => {
  it("cancel button calls auth.cancelMfa + redirects to /login", async () => {
    mockState.mfaPending = {
      mfa_token: "tok",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    const cancelSpy = vi.fn()
    mockState.cancelMfa = cancelSpy
    render(<MfaChallengePage />)
    fireEvent.click(screen.getByTestId("as7-mfa-cancel"))
    expect(cancelSpy).toHaveBeenCalled()
    expect(replaceSpy).toHaveBeenCalledWith("/login?next=%2F")
  })
})

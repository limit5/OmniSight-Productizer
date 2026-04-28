/**
 * AS.7.2 — Signup page integration test.
 *
 * Mocks `@/lib/auth-context` so the page renders deterministically
 * without a real backend round-trip. Covers:
 *
 *   - Page mounts with the AS.7.0 visual foundation + glass card
 *   - Brand wordmark + email field + password block + style toggle
 *     + strength meter render
 *   - 5 primary OAuth spheres render as anchors (alt-path)
 *   - Submit gate: disabled until every gate clears (email valid +
 *     password passes + saved + tos + honeypot resolved)
 *   - Submit threads honeypot key + tos_accepted_at into auth.signup
 *   - verifyEmail outcome flips to the EmailVerifyCard branch
 *   - failed outcome surfaces the canonical error banner
 *   - Style toggle re-rolls the password
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
  mfaPending: { mfa_token: string; mfa_methods: string[]; email: string } | null
  login: ReturnType<typeof vi.fn>
  signup: ReturnType<typeof vi.fn>
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
  mfaPending: null,
  login: vi.fn().mockResolvedValue(true),
  signup: vi.fn().mockResolvedValue({
    status: "ok",
    error: null,
    emailVerificationRequired: false,
    email: "user@example.com",
  }),
  logout: vi.fn(),
  refresh: vi.fn(),
  submitMfa: vi.fn().mockResolvedValue(true),
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

import SignupPage from "@/app/signup/page"

beforeEach(() => {
  mockState.user = null
  mockState.error = null
  mockState.lastSignupError = null
  mockState.mfaPending = null
  mockState.signup = vi.fn().mockResolvedValue({
    status: "ok",
    error: null,
    emailVerificationRequired: false,
    email: "user@example.com",
  })
  replaceSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

describe("AS.7.2 SignupPage — composition", () => {
  it("mounts with the AS.7.0 visual foundation root", () => {
    render(<SignupPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
  })

  it("renders the brand wordmark + email + password block", () => {
    render(<SignupPage />)
    expect(screen.getByTestId("as7-wordmark")).toBeInTheDocument()
    expect(screen.getByTestId("as7-field-email")).toBeInTheDocument()
    expect(screen.getByTestId("as7-signup-password-block")).toBeInTheDocument()
    expect(screen.getByTestId("as7-password-style-toggle")).toBeInTheDocument()
    expect(screen.getByTestId("as7-password-strength-meter")).toBeInTheDocument()
  })

  it("renders the 5 primary OAuth spheres in the alt-path row", () => {
    render(<SignupPage />)
    expect(screen.getByTestId("as7-signup-oauth-row-primary")).toBeInTheDocument()
    for (const id of ["google", "github", "microsoft", "apple", "discord"]) {
      const sphere = screen.getByTestId(`as7-oauth-${id}`)
      expect(sphere).toBeInTheDocument()
      expect(sphere.tagName.toLowerCase()).toBe("a")
      expect(sphere.getAttribute("href")).toContain(
        `/api/v1/auth/oauth/${id}/authorize`,
      )
    }
  })

  it("More toggle reveals the secondary 6 spheres", async () => {
    render(<SignupPage />)
    expect(screen.queryByTestId("as7-signup-oauth-row-secondary")).toBeNull()
    await userEvent.click(screen.getByTestId("as7-signup-oauth-more-toggle"))
    expect(
      screen.getByTestId("as7-signup-oauth-row-secondary"),
    ).toBeInTheDocument()
  })

  it("renders the honeypot field initially pending then ready", async () => {
    render(<SignupPage />)
    const f = screen.getByTestId("as7-honeypot-field")
    expect(f).toBeInTheDocument()
    await waitFor(() => {
      expect(f.getAttribute("data-as7-honeypot")).toBe("ready")
    })
  })

  it("Turnstile widget only renders when NEXT_PUBLIC_TURNSTILE_SITE_KEY is set", () => {
    render(<SignupPage />)
    expect(screen.queryByTestId("as7-turnstile-widget")).toBeNull()
  })

  it("auto-fills a generated password on first mount", () => {
    render(<SignupPage />)
    // Style is `random` by default; password is non-empty after the
    // initial useEffect.
    expect(screen.getByTestId("as7-style-random")).toHaveAttribute(
      "data-as7-style-active",
      "yes",
    )
    // The password input is rendered inside the password block.
    const passwordInput = screen.getByLabelText(
      "PASSWORD",
    ) as HTMLInputElement
    expect(passwordInput.value.length).toBeGreaterThanOrEqual(12)
  })
})

describe("AS.7.2 SignupPage — submit gate", () => {
  it("submit is disabled until every gate clears", async () => {
    render(<SignupPage />)
    const submit = screen.getByTestId("as7-signup-submit") as HTMLButtonElement
    // Email is empty + saved + tos checkboxes off + honeypot may
    // still be pending — submit must be disabled.
    expect(submit).toBeDisabled()
  })

  it("clears block reasons in order: email → saved → tos", async () => {
    render(<SignupPage />)

    // Wait for honeypot to resolve.
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-honeypot-field").getAttribute(
          "data-as7-honeypot",
        ),
      ).toBe("ready")
    })

    const submit = screen.getByTestId("as7-signup-submit") as HTMLButtonElement

    // Step 1: invalid email → blocked.
    expect(submit.getAttribute("data-as7-block-reason")).toMatch(
      /email_invalid|password_not_saved|tos_not_accepted|honeypot_pending/,
    )

    // Step 2: valid email → next block reason should NOT be email_invalid.
    const emailInput = screen.getByLabelText("EMAIL") as HTMLInputElement
    await userEvent.type(emailInput, "user@example.com")
    expect(submit.getAttribute("data-as7-block-reason")).not.toBe(
      "email_invalid",
    )

    // Step 3: check "saved" — block reason should switch to tos_not_accepted.
    const saveAck = screen.getByTestId("as7-save-ack-input") as HTMLInputElement
    fireEvent.click(saveAck)
    await waitFor(() => {
      expect(submit.getAttribute("data-as7-block-reason")).toBe(
        "tos_not_accepted",
      )
    })

    // Step 4: accept ToS → no block reason.
    const tosInput = screen.getByTestId("as7-signup-tos-input") as HTMLInputElement
    fireEvent.click(tosInput)
    await waitFor(() => {
      expect(submit).not.toBeDisabled()
      expect(submit.getAttribute("data-as7-block-reason")).toBe("ok")
    })
  })
})

describe("AS.7.2 SignupPage — submit flow", () => {
  it("submit threads honeypot + tos timestamp into auth.signup extras", async () => {
    mockState.signup = vi.fn().mockResolvedValue({
      status: "ok",
      error: null,
      emailVerificationRequired: false,
      email: "user@example.com",
    })
    render(<SignupPage />)

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
    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.click(screen.getByTestId("as7-signup-tos-input"))

    fireEvent.submit(screen.getByTestId("as7-signup-form"))

    await waitFor(() => expect(mockState.signup).toHaveBeenCalled())

    const [body, extras] = mockState.signup.mock.calls[0] as [
      Record<string, unknown>,
      Record<string, string>,
    ]
    expect(body.email).toBe("user@example.com")
    expect(typeof body.password).toBe("string")
    expect(String(body.password).length).toBeGreaterThanOrEqual(12)
    expect(typeof body.tos_accepted_at).toBe("string")
    // The honeypot dynamic key starts with "sg_".
    const honeypotKey = Object.keys(extras).find((k) => k.startsWith("sg_"))
    expect(honeypotKey).toBeDefined()
    expect(extras[honeypotKey!]).toBe("")
  })

  it("verifyEmail outcome swaps in the EmailVerifyCard", async () => {
    mockState.signup = vi.fn().mockResolvedValue({
      status: "verifyEmail",
      error: null,
      emailVerificationRequired: true,
      email: "user@example.com",
    })
    render(<SignupPage />)

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
    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.click(screen.getByTestId("as7-signup-tos-input"))
    fireEvent.submit(screen.getByTestId("as7-signup-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-signup-verify-card")).toBeInTheDocument()
    })
    expect(screen.getByText("user@example.com")).toBeInTheDocument()
  })

  it("failed outcome surfaces the canonical error banner", async () => {
    mockState.signup = vi.fn().mockResolvedValue({
      status: "failed",
      error: {
        kind: "registration_failed",
        message: "Sign-up could not be completed. Please try again.",
        retryAfterSeconds: null,
      },
      emailVerificationRequired: false,
      email: null,
    })
    mockState.lastSignupError = {
      kind: "registration_failed",
      message: "Sign-up could not be completed. Please try again.",
      retryAfterSeconds: null,
    }
    render(<SignupPage />)

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
    fireEvent.click(screen.getByTestId("as7-save-ack-input"))
    fireEvent.click(screen.getByTestId("as7-signup-tos-input"))
    fireEvent.submit(screen.getByTestId("as7-signup-form"))

    await waitFor(() => {
      expect(screen.getByTestId("as7-signup-error")).toBeInTheDocument()
    })
    expect(
      screen.getByText("Sign-up could not be completed. Please try again."),
    ).toBeInTheDocument()
  })
})

describe("AS.7.2 SignupPage — style toggle re-rolls", () => {
  it("toggling style fires a re-roll (password input value changes)", async () => {
    render(<SignupPage />)
    const passwordInput = screen.getByLabelText(
      "PASSWORD",
    ) as HTMLInputElement
    const initial = passwordInput.value
    expect(initial.length).toBeGreaterThanOrEqual(12)

    fireEvent.click(screen.getByTestId("as7-style-diceware"))

    // The new value comes from the diceware generator, which produces
    // word1-word2-word3-word4-NN — different shape.
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

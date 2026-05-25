/**
 * AS.7.1 — Login page integration test.
 *
 * Mocks `@/lib/auth-context` so the page renders deterministically
 * without a real backend round-trip. Covers:
 *
 *   - Page mounts with the AS.7.0 visual foundation + glass card
 *   - 5 primary OAuth spheres render as anchors
 *   - More toggle reveals the 6 secondary spheres
 *   - Email + password fields are AuthFieldElectric instances
 *   - Honeypot field renders (initially pending, then ready)
 *   - Submit threads turnstile_token + honeypot field into auth.login extras
 *   - Account-locked overlay shown when auth.lastLoginError.accountLocked
 *   - MFA branch swaps in <MfaChallengeForm> when auth.mfaPending is set
 *
 * Mocking strategy: `vi.mock('@/lib/auth-context')` replaces the
 * module with a stub `useAuth()` hook. Per-test we set the mock's
 * return state via a top-level mutable object so individual tests
 * can simulate "not signed in", "MFA pending", "locked" etc.
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
  mfaPending: { mfa_token: string; mfa_methods: string[]; email: string } | null
  login: ReturnType<typeof vi.fn>
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
  mfaPending: null,
  login: vi.fn().mockResolvedValue(true),
  logout: vi.fn(),
  refresh: vi.fn(),
  submitMfa: vi.fn().mockResolvedValue(true),
  cancelMfa: vi.fn(),
}

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => mockState,
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}))

// Mock next/navigation so router.replace is a spy.
const replaceSpy = vi.fn()
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}))

// Mock the motion-level resolver so render is deterministic.
vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "dramatic",
  usePrefersReducedMotion: () => false,
}))

// OP-1726 — the Turnstile widget fetches its config at runtime. Pin it
// "off" (no key) by default so the page renders the disabled widget and
// the submit flow works without a token, like internal staging.
const mockBotChallengeConfig = vi.fn().mockResolvedValue({
  provider: null,
  siteKey: null,
  enabled: false,
})
vi.mock("@/lib/api", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api")>()
  return { ...actual, fetchBotChallengeConfig: () => mockBotChallengeConfig() }
})

import LoginPage from "@/app/login/page"

beforeEach(() => {
  mockState.user = null
  mockState.error = null
  mockState.lastLoginError = null
  mockState.mfaPending = null
  mockState.login = vi.fn().mockResolvedValue(true)
  mockState.submitMfa = vi.fn().mockResolvedValue(true)
  replaceSpy.mockReset()
})

afterEach(() => {
  cleanup()
})

describe("AS.7.1 LoginPage — composition", () => {
  it("mounts with the AS.7.0 visual foundation root", () => {
    render(<LoginPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
  })

  it("renders the brand wordmark + email + password fields", () => {
    render(<LoginPage />)
    expect(screen.getByTestId("as7-wordmark")).toBeInTheDocument()
    expect(screen.getByTestId("as7-field-email")).toBeInTheDocument()
    expect(screen.getByTestId("as7-field-password")).toBeInTheDocument()
  })

  it("renders unconfigured primary OAuth spheres as disabled settings CTAs", () => {
    render(<LoginPage />)
    for (const id of ["google", "github", "microsoft", "apple", "discord"]) {
      const sphere = screen.getByTestId(`as7-oauth-${id}`)
      expect(sphere).toBeInTheDocument()
      expect(sphere.tagName.toLowerCase()).toBe("span")
      expect(sphere).toHaveAttribute("aria-disabled", "true")
      expect(
        screen.getByTestId(`as7-oauth-${id}-disabled-state`),
      ).toHaveTextContent("Configure in Settings")
    }
  })

  it("builds the OAuth authorize path for configured providers", async () => {
    vi.resetModules()
    vi.stubEnv("NEXT_PUBLIC_OMNISIGHT_OAUTH_GOOGLE_CONFIGURED", "true")
    const { buildOAuthAuthorizeUrl, getProvider } = await import(
      "@/lib/auth/oauth-providers"
    )
    expect(getProvider("google").configured).toBe(true)
    expect(buildOAuthAuthorizeUrl("google")).toBe(
      "/api/v1/auth/oauth/google/authorize",
    )
    vi.unstubAllEnvs()
  })

  it("OAuth URL builder keeps backend route shape", async () => {
    const { buildOAuthAuthorizeUrl } = await import("@/lib/auth/oauth-providers")
    for (const id of ["google", "github", "microsoft", "apple", "discord"]) {
      expect(buildOAuthAuthorizeUrl(id as never)).toContain(
        `/api/v1/auth/oauth/${id}/authorize`,
      )
    }
  })

  it("More toggle reveals the secondary 6 disabled settings CTAs", async () => {
    render(<LoginPage />)
    expect(screen.queryByTestId("as7-oauth-row-secondary")).toBeNull()
    await userEvent.click(screen.getByTestId("as7-oauth-more-toggle"))
    expect(screen.getByTestId("as7-oauth-row-secondary")).toBeInTheDocument()
    for (const id of [
      "gitlab",
      "bitbucket",
      "slack",
      "notion",
      "salesforce",
      "hubspot",
    ]) {
      expect(screen.getByTestId(`as7-oauth-${id}`)).toHaveAttribute(
        "aria-disabled",
        "true",
      )
      expect(
        screen.getByTestId(`as7-oauth-${id}-disabled-state`),
      ).toHaveTextContent("Configure in Settings")
    }
  })

  it("renders the honeypot field (initially pending)", () => {
    render(<LoginPage />)
    const field = screen.getByTestId("as7-honeypot-field")
    expect(field).toBeInTheDocument()
  })

  it("Turnstile surface stays disabled when the runtime config serves no key", async () => {
    // OP-1726 — the widget always mounts but resolves to the disabled
    // surface when the backend serves no site key (internal staging).
    render(<LoginPage />)
    await waitFor(() => expect(mockBotChallengeConfig).toHaveBeenCalled())
    expect(
      screen.getByTestId("as7-turnstile-widget"),
    ).toHaveAttribute("data-as7-turnstile", "disabled")
  })

  it("Turnstile mounts + submit requires a token when the runtime config serves a key", async () => {
    mockBotChallengeConfig.mockResolvedValueOnce({
      provider: "turnstile",
      siteKey: "runtime-key",
      enabled: true,
    })
    mockState.login = vi.fn().mockResolvedValue(true)
    render(<LoginPage />)

    const emailInput = screen.getByLabelText("EMAIL") as HTMLInputElement
    const passwordInput = screen.getByLabelText("PASSWORD") as HTMLInputElement
    await userEvent.type(emailInput, "ops@example.com")
    await userEvent.type(passwordInput, "hunter2pw")

    // The widget upgrades out of the disabled surface once the key lands.
    await waitFor(() =>
      expect(["loading", "ready"]).toContain(
        screen
          .getByTestId("as7-turnstile-widget")
          .getAttribute("data-as7-turnstile"),
      ),
    )

    // No turnstile token yet → the submit button is gated.
    const submitBtn = screen
      .getByText("Sign in")
      .closest("button") as HTMLButtonElement
    expect(submitBtn.disabled).toBe(true)

    // Submitting the form anyway must not call auth.login (token required).
    fireEvent.submit(screen.getByTestId("as7-login-form"))
    expect(mockState.login).not.toHaveBeenCalled()
  })
})

describe("AS.7.1 LoginPage — submit flow", () => {
  it("submits with email/password + threads honeypot key into extras", async () => {
    mockState.login = vi.fn().mockResolvedValue(true)
    render(<LoginPage />)

    const emailInput = screen.getByLabelText("EMAIL") as HTMLInputElement
    const passwordInput = screen.getByLabelText("PASSWORD") as HTMLInputElement
    await userEvent.type(emailInput, "ops@example.com")
    await userEvent.type(passwordInput, "hunter2pw")

    // Wait for the honeypot field to resolve so the form can include it.
    await waitFor(() => {
      const f = screen.getByTestId("as7-honeypot-field")
      expect(f.getAttribute("data-as7-honeypot")).toBe("ready")
    })

    fireEvent.submit(screen.getByTestId("as7-login-form"))

    await waitFor(() => expect(mockState.login).toHaveBeenCalled())
    const args = mockState.login.mock.calls[0]
    expect(args[0]).toBe("ops@example.com")
    expect(args[1]).toBe("hunter2pw")
    const extras = args[2] as Record<string, string>
    expect(extras).toBeTruthy()
    // The honeypot key should be present (lg_<word>) with empty value.
    const keys = Object.keys(extras)
    const honeypotKey = keys.find((k) => k.startsWith("lg_"))
    expect(honeypotKey).toBeDefined()
    expect(extras[honeypotKey!]).toBe("")
  })

  it("shows the canonical error banner when login fails", async () => {
    mockState.login = vi.fn().mockResolvedValue(false)
    mockState.error = "Invalid email or password."
    render(<LoginPage />)
    expect(screen.getByTestId("as7-login-error")).toHaveTextContent(
      "Invalid email or password.",
    )
  })
})

describe("AS.7.1 LoginPage — locked overlay", () => {
  it("renders the AccountLockedOverlay when lastLoginError.accountLocked", () => {
    mockState.error = "Account locked."
    mockState.lastLoginError = {
      accountLocked: true,
      retryAfterSeconds: 30,
    }
    render(<LoginPage />)
    expect(screen.getByTestId("as7-account-locked-overlay")).toBeInTheDocument()
  })

  it("submit button is disabled when accountLocked is true", () => {
    mockState.lastLoginError = {
      accountLocked: true,
      retryAfterSeconds: 30,
    }
    render(<LoginPage />)
    const btn = screen.getByText("Sign in").closest("button") as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("OAuth buttons are disabled when accountLocked is true", () => {
    mockState.lastLoginError = {
      accountLocked: true,
      retryAfterSeconds: 30,
    }
    render(<LoginPage />)
    const sphere = screen.getByTestId("as7-oauth-google")
    expect(sphere.tagName.toLowerCase()).toBe("span")
    expect(sphere).toHaveAttribute("aria-disabled", "true")
  })
})

describe("AS.7.1 LoginPage — MFA branch", () => {
  it("renders MFA challenge form when auth.mfaPending is set", () => {
    mockState.mfaPending = {
      mfa_token: "tok-x",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    render(<LoginPage />)
    expect(screen.getByTestId("as7-mfa-form")).toBeInTheDocument()
    expect(
      screen.getByText("Two-Factor Authentication"),
    ).toBeInTheDocument()
  })

  it("MFA cancel calls auth.cancelMfa", async () => {
    mockState.mfaPending = {
      mfa_token: "tok-x",
      mfa_methods: ["totp"],
      email: "ops@example.com",
    }
    render(<LoginPage />)
    await userEvent.click(screen.getByText("Back to login"))
    expect(mockState.cancelMfa).toHaveBeenCalled()
  })
})

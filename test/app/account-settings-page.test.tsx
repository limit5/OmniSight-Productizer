/**
 * AS.7.7 — Account settings page integration tests.
 *
 * Mocks `@/lib/auth-context` + the lib/api wrappers so the page
 * renders deterministically. Covers:
 *
 *   - Composition: AS.7.0 visual foundation + glass card + body
 *   - Header copy + back-to-dashboard link
 *   - Sections: connected accounts (orbital), auth methods,
 *     auth providers, MFA setup, sessions, password change,
 *     API keys, GDPR
 *   - Unauthenticated guard redirects to /login
 *   - Password-change submit gate clears in cascade
 *   - Sessions revoke + revoke-all click handlers
 *   - Delete-account typed-confirmation gate
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

const mockAuthState = {
  user: { id: "u-1", email: "operator@example.com", role: "admin" } as
    | { id: string; email: string; role: string }
    | null,
  authMode: "password" as string | null,
  sessionId: "sid-1" as string | null,
  loading: false,
  error: null as string | null,
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
  useAuth: () => mockAuthState,
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

const mockApi = {
  listOAuthIdentities: vi.fn().mockResolvedValue({ items: [], count: 0 }),
  disconnectOAuthIdentity: vi.fn().mockResolvedValue({
    status: "ok",
    provider: "google",
  }),
  listSessions: vi.fn().mockResolvedValue({
    items: [
      {
        token_hint: "this-device",
        created_at: 1_000_000,
        expires_at: 2_000_000,
        last_seen_at: 1_500_000,
        ip: "10.0.0.1",
        user_agent: "Mozilla/5.0 (this-device)",
        is_current: true,
      },
      {
        token_hint: "other-device",
        created_at: 1_000_000,
        expires_at: 2_000_000,
        last_seen_at: 1_500_000,
        ip: "10.0.0.2",
        user_agent: "Mozilla/5.0 (other-device)",
        is_current: false,
      },
    ],
    count: 2,
  }),
  revokeSession: vi.fn().mockResolvedValue({ status: "ok" }),
  revokeAllOtherSessions: vi
    .fn()
    .mockResolvedValue({ status: "ok", revoked_count: 1 }),
  changePassword: vi.fn().mockResolvedValue({
    status: "password_changed",
    must_change_password: false,
  }),
  mfaStatus: vi.fn().mockResolvedValue({
    methods: [
      {
        id: "mfa-totp-1",
        method: "totp",
        name: "default",
        verified: true,
        created_at: "",
        last_used: null,
      },
    ],
    has_mfa: true,
    require_mfa: false,
  }),
  mfaTotpDisable: vi.fn().mockResolvedValue({ status: "ok" }),
  mfaWebauthnRemove: vi.fn().mockResolvedValue({ status: "ok" }),
  mfaBackupCodesStatus: vi.fn().mockResolvedValue({ total: 10, remaining: 8 }),
  mfaBackupCodesRegenerate: vi.fn().mockResolvedValue({
    codes: [],
    count: 10,
  }),
  exportAccountData: vi.fn().mockResolvedValue({
    status: "queued",
    download_url: null,
    expires_at: null,
  }),
  requestAccountDeletion: vi.fn().mockResolvedValue({
    status: "scheduled",
    scheduled_for: "2026-05-28T00:00:00Z",
  }),
}

vi.mock("@/lib/api", () => ({
  // Re-export ApiError as a plain class so the page's `instanceof ApiError`
  // checks compile.
  ApiError: class ApiError extends Error {
    status: number
    parsed: Record<string, unknown> | null
    constructor(status: number, parsed: Record<string, unknown> | null = null) {
      super(`api error ${status}`)
      this.status = status
      this.parsed = parsed
    }
  },
  listOAuthIdentities: (...args: unknown[]) =>
    mockApi.listOAuthIdentities(...args),
  disconnectOAuthIdentity: (...args: unknown[]) =>
    mockApi.disconnectOAuthIdentity(...args),
  listSessions: (...args: unknown[]) => mockApi.listSessions(...args),
  revokeSession: (...args: unknown[]) => mockApi.revokeSession(...args),
  revokeAllOtherSessions: (...args: unknown[]) =>
    mockApi.revokeAllOtherSessions(...args),
  changePassword: (...args: unknown[]) => mockApi.changePassword(...args),
  mfaStatus: (...args: unknown[]) => mockApi.mfaStatus(...args),
  mfaTotpDisable: (...args: unknown[]) => mockApi.mfaTotpDisable(...args),
  mfaWebauthnRemove: (...args: unknown[]) => mockApi.mfaWebauthnRemove(...args),
  mfaBackupCodesStatus: (...args: unknown[]) =>
    mockApi.mfaBackupCodesStatus(...args),
  mfaBackupCodesRegenerate: (...args: unknown[]) =>
    mockApi.mfaBackupCodesRegenerate(...args),
  exportAccountData: (...args: unknown[]) => mockApi.exportAccountData(...args),
  requestAccountDeletion: (...args: unknown[]) =>
    mockApi.requestAccountDeletion(...args),
}))

import AccountSettingsPage from "@/app/settings/account/page"

beforeEach(() => {
  mockAuthState.user = {
    id: "u-1",
    email: "operator@example.com",
    role: "admin",
  }
  mockAuthState.loading = false
  mockAuthState.logout = vi.fn().mockResolvedValue(undefined)
  replaceSpy.mockReset()
  for (const fn of Object.values(mockApi)) {
    if (typeof fn === "function" && "mockClear" in fn) {
      ;(fn as ReturnType<typeof vi.fn>).mockClear()
    }
  }
})

afterEach(() => {
  cleanup()
})

describe("AS.7.7 AccountSettingsPage — composition", () => {
  it("mounts with the AS.7.0 visual foundation + glass card + body", async () => {
    render(<AccountSettingsPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
    expect(screen.getByTestId("as7-account-body")).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId("as7-account-body")).toHaveAttribute(
        "data-as7-bootstrapped",
        "yes",
      )
    })
  })

  it("renders the page header + back-to-dashboard link", () => {
    render(<AccountSettingsPage />)
    expect(screen.getByTestId("as7-account-header")).toBeInTheDocument()
    expect(screen.getByTestId("as7-account-title")).toHaveTextContent(
      /Profile.*account settings/i,
    )
    expect(screen.getByTestId("as7-account-back-link")).toHaveAttribute(
      "href",
      "/",
    )
  })

  it("renders all 8 sections", async () => {
    render(<AccountSettingsPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-section-connected-accounts"),
      ).toBeInTheDocument()
    })
    expect(screen.getByTestId("as7-section-auth-methods")).toBeInTheDocument()
    expect(screen.getByTestId("as7-section-auth-providers")).toBeInTheDocument()
    expect(screen.getByTestId("as7-section-mfa-setup")).toBeInTheDocument()
    expect(screen.getByTestId("as7-section-sessions")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-section-password-change"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("as7-section-api-keys")).toBeInTheDocument()
    expect(screen.getByTestId("as7-section-data-privacy")).toBeInTheDocument()
  })
})

describe("AS.7.7 AccountSettingsPage — unauthenticated guard", () => {
  it("redirects to /login when no user is loaded", async () => {
    mockAuthState.user = null
    mockAuthState.loading = false
    render(<AccountSettingsPage />)
    expect(screen.getByTestId("as7-account-unauth")).toBeInTheDocument()
    await waitFor(() => {
      expect(replaceSpy).toHaveBeenCalledWith(
        "/login?next=" + encodeURIComponent("/settings/account"),
      )
    })
  })
})

describe("AS.7.7 AccountSettingsPage — connected accounts", () => {
  it("renders the orbital stage in dramatic mode", async () => {
    render(<AccountSettingsPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-orbit-stage")).toHaveAttribute(
        "data-as7-orbit-mode",
        "rotating",
      )
    })
  })
})

describe("AS.7.7 AccountSettingsPage — auth providers", () => {
  it("renders the 11-provider configured-state panel", async () => {
    render(<AccountSettingsPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-section-auth-providers"),
      ).toHaveAttribute("data-as7-provider-count", "11")
    })
    expect(screen.getByTestId("as7-section-auth-providers")).toHaveAttribute(
      "data-as7-provider-configured-count",
      "0",
    )
    for (const id of [
      "google",
      "github",
      "microsoft",
      "apple",
      "discord",
      "gitlab",
      "bitbucket",
      "slack",
      "notion",
      "salesforce",
      "hubspot",
    ]) {
      expect(screen.getByTestId(`as7-auth-provider-${id}`)).toHaveAttribute(
        "data-as7-provider-configured",
        "no",
      )
      expect(
        screen.getByTestId(`as7-auth-provider-docs-${id}`),
      ).toHaveAttribute("href", expect.stringMatching(/^https:\/\//))
    }
  })
})

describe("AS.7.7 AccountSettingsPage — sessions section", () => {
  it("renders the sessions list once bootstrapped + tags current device", async () => {
    render(<AccountSettingsPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-session-this-device"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("as7-session-this-device"),
    ).toHaveAttribute("data-as7-session-current", "yes")
    expect(
      screen.getByTestId("as7-session-other-device"),
    ).toHaveAttribute("data-as7-session-current", "no")
  })

  it("revoke button calls listSessions reload", async () => {
    const user = userEvent.setup()
    render(<AccountSettingsPage />)
    await waitFor(() =>
      expect(
        screen.getByTestId("as7-session-revoke-other-device"),
      ).toBeInTheDocument(),
    )
    await user.click(screen.getByTestId("as7-session-revoke-other-device"))
    expect(mockApi.revokeSession).toHaveBeenCalledWith("other-device")
  })

  it("revoke-all button surfaces when at least one peer exists", async () => {
    const user = userEvent.setup()
    render(<AccountSettingsPage />)
    await waitFor(() =>
      expect(
        screen.getByTestId("as7-sessions-revoke-all"),
      ).toBeInTheDocument(),
    )
    await user.click(screen.getByTestId("as7-sessions-revoke-all"))
    expect(mockApi.revokeAllOtherSessions).toHaveBeenCalledTimes(1)
  })
})

describe("AS.7.7 AccountSettingsPage — password change", () => {
  it("submit gate cycles through every block reason on its way to ok", async () => {
    const user = userEvent.setup()
    render(<AccountSettingsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("as7-account-body")).toHaveAttribute(
        "data-as7-bootstrapped",
        "yes",
      ),
    )
    const form = screen.getByTestId("as7-password-change-form")
    expect(form).toHaveAttribute(
      "data-as7-block-reason",
      "current_password_missing",
    )
    await user.type(
      screen.getByTestId("as7-password-change-current"),
      "oldpassword",
    )
    expect(form).toHaveAttribute(
      "data-as7-block-reason",
      "new_password_too_short",
    )
    await user.type(
      screen.getByTestId("as7-password-change-new"),
      "very-long-new-password",
    )
    expect(form).toHaveAttribute(
      "data-as7-block-reason",
      "password_not_saved",
    )
    await user.click(screen.getByTestId("as7-password-change-saved"))
    expect(form).toHaveAttribute("data-as7-block-reason", "ok")
  })

  it("submit calls api.changePassword and surfaces the success banner", async () => {
    const user = userEvent.setup()
    render(<AccountSettingsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("as7-account-body")).toHaveAttribute(
        "data-as7-bootstrapped",
        "yes",
      ),
    )
    await user.type(
      screen.getByTestId("as7-password-change-current"),
      "oldpassword",
    )
    await user.type(
      screen.getByTestId("as7-password-change-new"),
      "very-long-new-password",
    )
    await user.click(screen.getByTestId("as7-password-change-saved"))
    await user.click(screen.getByTestId("as7-password-change-submit"))
    await waitFor(() => {
      expect(mockApi.changePassword).toHaveBeenCalledWith(
        "oldpassword",
        "very-long-new-password",
      )
    })
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-password-change-success"),
      ).toBeInTheDocument()
    })
  })
})

describe("AS.7.7 AccountSettingsPage — API keys section", () => {
  it("admin role gets the visible card", async () => {
    render(<AccountSettingsPage />)
    await waitFor(() => {
      const section = screen.getByTestId("as7-section-api-keys")
      expect(section).toHaveAttribute("data-as7-api-keys-visible", "yes")
    })
    expect(
      screen.getByTestId("as7-api-keys-admin-stub"),
    ).toBeInTheDocument()
  })

  it("non-admin role gets the disabled card", async () => {
    mockAuthState.user = {
      id: "u-2",
      email: "member@example.com",
      role: "member",
    }
    render(<AccountSettingsPage />)
    await waitFor(() => {
      const section = screen.getByTestId("as7-section-api-keys")
      expect(section).toHaveAttribute("data-as7-api-keys-visible", "no")
      expect(section).toHaveAttribute("data-as7-api-keys-reason", "not_admin")
    })
  })
})

describe("AS.7.7 AccountSettingsPage — data & privacy", () => {
  it("delete-account form starts blocked by confirmation_mismatch", async () => {
    render(<AccountSettingsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("as7-data-delete-form")).toHaveAttribute(
        "data-as7-block-reason",
        "confirmation_mismatch",
      ),
    )
  })

  it("typing DELETE + ack clears the gate", async () => {
    const user = userEvent.setup()
    render(<AccountSettingsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("as7-data-delete-form")).toBeInTheDocument(),
    )
    await user.type(
      screen.getByTestId("as7-data-delete-confirmation"),
      "DELETE",
    )
    expect(screen.getByTestId("as7-data-delete-form")).toHaveAttribute(
      "data-as7-block-reason",
      "irreversible_unacknowledged",
    )
    await user.click(screen.getByTestId("as7-data-delete-acknowledge"))
    expect(screen.getByTestId("as7-data-delete-form")).toHaveAttribute(
      "data-as7-block-reason",
      "ok",
    )
  })

  it("clicking export calls api.exportAccountData", async () => {
    const user = userEvent.setup()
    render(<AccountSettingsPage />)
    await waitFor(() =>
      expect(
        screen.getByTestId("as7-data-export-submit"),
      ).toBeInTheDocument(),
    )
    await user.click(screen.getByTestId("as7-data-export-submit"))
    expect(mockApi.exportAccountData).toHaveBeenCalledTimes(1)
  })
})

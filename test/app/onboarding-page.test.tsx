/**
 * AS.7.8 — Onboarding page integration tests.
 *
 * Mocks `@/lib/auth-context` + the lib/api wrappers so the page
 * renders deterministically. Covers:
 *
 *   - Composition: AS.7.0 visual foundation + glass card + body
 *   - Header copy + step indicator
 *   - Unauthenticated guard redirects to /login with onboarding next
 *   - Step cascade (tenant → profile → project → celebrate)
 *   - Tenant step: admin can rename; non-admin sees the "locked"
 *     reason but can still continue
 *   - Profile step: persists display name to localStorage
 *   - Project step: submit-gate cascade + slug preview + product-line
 *     selection
 *   - Celebrate step: mounts the burst + redirects to "/"
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

const mockAuthState = {
  user: { id: "u-1", email: "u@example.com", name: "", role: "admin", tenant_id: "t-1", enabled: true } as
    | {
        id: string
        email: string
        name: string
        role: string
        tenant_id: string
        enabled: boolean
      }
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
const searchParamsValue = { get: (_k: string) => null as string | null }

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceSpy, push: vi.fn() }),
  useSearchParams: () => searchParamsValue,
}))

vi.mock("@/hooks/use-effective-motion-level", () => ({
  useEffectiveMotionLevel: () => "dramatic",
  usePrefersReducedMotion: () => false,
}))

const mockApi = {
  listUserTenants: vi.fn().mockResolvedValue([
    { id: "t-1", name: "Acme Cameras", plan: "starter", enabled: true },
  ]),
  listTenantProjects: vi.fn().mockResolvedValue([] as unknown[]),
  adminPatchTenant: vi.fn().mockResolvedValue({
    id: "t-1",
    name: "Acme Renamed",
    plan: "starter",
    enabled: true,
    created_at: "",
  }),
  createTenantProject: vi.fn().mockResolvedValue({
    project_id: "p-1",
    tenant_id: "t-1",
    product_line: "embedded",
    name: "Lobby cameras",
    slug: "lobby-cameras",
    parent_id: null,
    plan_override: null,
    disk_budget_bytes: null,
    llm_budget_tokens: null,
    created_by: "u-1",
    created_at: "",
    archived_at: null,
  }),
}

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number
    parsed: Record<string, unknown> | null
    constructor(status: number, parsed: Record<string, unknown> | null = null) {
      super(`api error ${status}`)
      this.status = status
      this.parsed = parsed
    }
  },
  listUserTenants: (...args: unknown[]) => mockApi.listUserTenants(...args),
  listTenantProjects: (...args: unknown[]) =>
    mockApi.listTenantProjects(...args),
  adminPatchTenant: (...args: unknown[]) => mockApi.adminPatchTenant(...args),
  createTenantProject: (...args: unknown[]) =>
    mockApi.createTenantProject(...args),
}))

import OnboardingPage from "@/app/onboarding/page"

beforeEach(() => {
  mockAuthState.user = {
    id: "u-1",
    email: "u@example.com",
    name: "",
    role: "admin",
    tenant_id: "t-1",
    enabled: true,
  }
  mockAuthState.loading = false
  replaceSpy.mockReset()
  searchParamsValue.get = (_k: string) => null
  for (const fn of Object.values(mockApi)) {
    if (typeof fn === "function" && "mockClear" in fn) {
      ;(fn as ReturnType<typeof vi.fn>).mockClear()
    }
  }
  // Reset stored display name so the test starts at the tenant step.
  try {
    window.localStorage.removeItem("omnisight:onboarding:displayName")
  } catch {
    /* jsdom may not have full localStorage in some configs */
  }
})

afterEach(() => {
  cleanup()
})

describe("AS.7.8 OnboardingPage — composition", () => {
  it("mounts with the AS.7.0 visual foundation + glass card + body", async () => {
    render(<OnboardingPage />)
    expect(screen.getByTestId("as7-root")).toBeInTheDocument()
    expect(screen.getByTestId("as7-glass-card")).toBeInTheDocument()
    expect(screen.getByTestId("as7-onboarding-body")).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
        "data-as7-onboarding-bootstrapped",
        "yes",
      )
    })
  })

  it("renders the page header + 4 step indicator pills", async () => {
    render(<OnboardingPage />)
    expect(screen.getByTestId("as7-onboarding-header")).toBeInTheDocument()
    expect(
      screen.getByTestId("as7-onboarding-step-indicator"),
    ).toBeInTheDocument()
    for (const kind of ["tenant", "profile", "project", "celebrate"]) {
      expect(
        screen.getByTestId(`as7-onboarding-step-${kind}`),
      ).toBeInTheDocument()
    }
  })

  it("renders the skip-for-now link", () => {
    render(<OnboardingPage />)
    expect(screen.getByTestId("as7-onboarding-skip")).toHaveAttribute(
      "href",
      "/",
    )
  })
})

describe("AS.7.8 OnboardingPage — unauthenticated guard", () => {
  it("redirects to /login with the onboarding destination when no user", async () => {
    mockAuthState.user = null
    mockAuthState.loading = false
    render(<OnboardingPage />)
    expect(screen.getByTestId("as7-onboarding-unauth")).toBeInTheDocument()
    await waitFor(() => {
      expect(replaceSpy).toHaveBeenCalledWith(
        "/login?next=" + encodeURIComponent("/onboarding"),
      )
    })
  })
})

describe("AS.7.8 OnboardingPage — tenant step", () => {
  it("starts on the tenant step with the workspace name pre-filled", async () => {
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-onboarding-tenant-input"),
      ).toBeInTheDocument()
    })
    const input = screen.getByTestId(
      "as7-onboarding-tenant-input",
    ) as HTMLInputElement
    expect(input.value).toBe("Acme Cameras")
  })

  it("admin can rename → adminPatchTenant called → advances to profile step", async () => {
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    const input = screen.getByTestId(
      "as7-onboarding-tenant-input",
    ) as HTMLInputElement
    await user.clear(input)
    await user.type(input, "Acme Renamed")
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    await waitFor(() => {
      expect(mockApi.adminPatchTenant).toHaveBeenCalledWith("t-1", {
        name: "Acme Renamed",
      })
    })
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
        "data-as7-onboarding-step",
        "profile",
      )
    })
  })

  it("non-admin role sees the locked hint and can still continue without a PATCH", async () => {
    mockAuthState.user = {
      id: "u-1",
      email: "u@example.com",
      name: "",
      role: "viewer",
      tenant_id: "t-1",
      enabled: true,
    }
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-onboarding-tenant-locked"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("as7-onboarding-tenant-submit"),
    ).toHaveAttribute("data-as7-block-reason", "locked")
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    expect(mockApi.adminPatchTenant).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
        "data-as7-onboarding-step",
        "profile",
      )
    })
  })

  it("admin advances without a PATCH when the name is unchanged", async () => {
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    expect(mockApi.adminPatchTenant).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
        "data-as7-onboarding-step",
        "profile",
      )
    })
  })

  it("backend 409 surfaces canonical conflict copy", async () => {
    class ApiError extends Error {
      status: number
      parsed: Record<string, unknown> | null
      constructor(status: number) {
        super(String(status))
        this.status = status
        this.parsed = null
      }
    }
    mockApi.adminPatchTenant.mockRejectedValueOnce(new ApiError(409))
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    const input = screen.getByTestId(
      "as7-onboarding-tenant-input",
    ) as HTMLInputElement
    await user.clear(input)
    await user.type(input, "Existing Name")
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-error")).toHaveAttribute(
        "data-as7-error-kind",
        "conflict",
      )
    })
    // Still on tenant step.
    expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
      "data-as7-onboarding-step",
      "tenant",
    )
  })
})

describe("AS.7.8 OnboardingPage — profile step", () => {
  it("submit gate cascades through display_name_too_short → ok", async () => {
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    // Advance through tenant step (unchanged name = no PATCH)
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-onboarding-profile-input"),
      ).toBeInTheDocument()
    })
    const input = screen.getByTestId(
      "as7-onboarding-profile-input",
    ) as HTMLInputElement
    expect(
      screen.getByTestId("as7-onboarding-profile-submit"),
    ).toHaveAttribute("data-as7-block-reason", "display_name_too_short")
    await user.type(input, "Casey")
    expect(
      screen.getByTestId("as7-onboarding-profile-submit"),
    ).toHaveAttribute("data-as7-block-reason", "ok")
  })

  it("submitting persists the display name to localStorage and advances to project", async () => {
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-onboarding-profile-input"),
      ).toBeInTheDocument()
    })
    await user.type(
      screen.getByTestId("as7-onboarding-profile-input"),
      "Casey",
    )
    await user.click(screen.getByTestId("as7-onboarding-profile-submit"))
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
        "data-as7-onboarding-step",
        "project",
      )
    })
    expect(
      window.localStorage.getItem("omnisight:onboarding:displayName"),
    ).toBe("Casey")
  })
})

describe("AS.7.8 OnboardingPage — project step", () => {
  async function advanceToProject(user: ReturnType<typeof userEvent.setup>) {
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-onboarding-profile-input"),
      ).toBeInTheDocument()
    })
    await user.type(
      screen.getByTestId("as7-onboarding-profile-input"),
      "Casey",
    )
    await user.click(screen.getByTestId("as7-onboarding-profile-submit"))
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-onboarding-project-input"),
      ).toBeInTheDocument()
    })
  }

  it("submit gate starts with name_too_short, then product_line_required, then ok", async () => {
    const user = userEvent.setup()
    await advanceToProject(user)
    expect(
      screen.getByTestId("as7-onboarding-project-submit"),
    ).toHaveAttribute("data-as7-block-reason", "name_too_short")
    await user.type(
      screen.getByTestId("as7-onboarding-project-input"),
      "Lobby cameras",
    )
    expect(
      screen.getByTestId("as7-onboarding-project-submit"),
    ).toHaveAttribute("data-as7-block-reason", "product_line_required")
    await user.click(screen.getByTestId("as7-onboarding-project-pl-embedded"))
    expect(
      screen.getByTestId("as7-onboarding-project-submit"),
    ).toHaveAttribute("data-as7-block-reason", "ok")
  })

  it("renders the slug preview reactively", async () => {
    const user = userEvent.setup()
    await advanceToProject(user)
    await user.type(
      screen.getByTestId("as7-onboarding-project-input"),
      "Hello World",
    )
    expect(
      screen.getByTestId("as7-onboarding-project-slug-value"),
    ).toHaveTextContent("hello-world")
  })

  it("submitting calls createTenantProject with the resolved slug + product_line + advances to celebrate", async () => {
    const user = userEvent.setup()
    await advanceToProject(user)
    await user.type(
      screen.getByTestId("as7-onboarding-project-input"),
      "Lobby Cameras",
    )
    await user.click(
      screen.getByTestId("as7-onboarding-project-pl-embedded"),
    )
    await user.click(screen.getByTestId("as7-onboarding-project-submit"))
    await waitFor(() => {
      expect(mockApi.createTenantProject).toHaveBeenCalledWith("t-1", {
        product_line: "embedded",
        name: "Lobby Cameras",
        slug: "lobby-cameras",
      })
    })
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
        "data-as7-onboarding-step",
        "celebrate",
      )
    })
  })

  it("backend 409 surfaces canonical conflict copy and stays on the project step", async () => {
    class ApiError extends Error {
      status: number
      parsed: Record<string, unknown> | null
      constructor(status: number) {
        super(String(status))
        this.status = status
        this.parsed = null
      }
    }
    mockApi.createTenantProject.mockRejectedValueOnce(new ApiError(409))
    const user = userEvent.setup()
    await advanceToProject(user)
    await user.type(
      screen.getByTestId("as7-onboarding-project-input"),
      "Lobby Cameras",
    )
    await user.click(
      screen.getByTestId("as7-onboarding-project-pl-embedded"),
    )
    await user.click(screen.getByTestId("as7-onboarding-project-submit"))
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-error")).toHaveAttribute(
        "data-as7-error-kind",
        "conflict",
      )
    })
    expect(screen.getByTestId("as7-onboarding-body")).toHaveAttribute(
      "data-as7-onboarding-step",
      "project",
    )
  })
})

describe("AS.7.8 OnboardingPage — celebrate step", () => {
  it("starts on celebrate when the user already has a project + display name + tenant", async () => {
    mockApi.listTenantProjects.mockResolvedValueOnce([
      {
        project_id: "p-1",
        tenant_id: "t-1",
        product_line: "embedded",
        name: "existing",
        slug: "existing",
        parent_id: null,
        plan_override: null,
        disk_budget_bytes: null,
        llm_budget_tokens: null,
        created_by: "u-1",
        created_at: "",
        archived_at: null,
      },
    ])
    window.localStorage.setItem(
      "omnisight:onboarding:displayName",
      "Casey",
    )
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    // Advance through tenant unchanged.
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-celebrate")).toBeInTheDocument()
    })
    // Burst stage mounts dramatically (matches the test's mocked motion level).
    await waitFor(() => {
      expect(screen.getByTestId("as7-burst-stage")).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("as7-onboarding-celebrate-summary"),
    ).toHaveTextContent(/dropping you in the dashboard/i)
  })

  it("celebrate CTA fires router.replace to the redirect target", async () => {
    mockApi.listTenantProjects.mockResolvedValueOnce([
      {
        project_id: "p-1",
        tenant_id: "t-1",
        product_line: "embedded",
        name: "x",
        slug: "x",
        parent_id: null,
        plan_override: null,
        disk_budget_bytes: null,
        llm_budget_tokens: null,
        created_by: "u-1",
        created_at: "",
        archived_at: null,
      },
    ])
    window.localStorage.setItem(
      "omnisight:onboarding:displayName",
      "Casey",
    )
    const user = userEvent.setup()
    render(<OnboardingPage />)
    await waitFor(() => {
      expect(screen.getByTestId("as7-onboarding-tenant-input")).toBeInTheDocument()
    })
    await user.click(screen.getByTestId("as7-onboarding-tenant-submit"))
    await waitFor(() => {
      expect(
        screen.getByTestId("as7-onboarding-celebrate-cta"),
      ).toBeInTheDocument()
    })
    await user.click(screen.getByTestId("as7-onboarding-celebrate-cta"))
    await waitFor(() => {
      expect(replaceSpy).toHaveBeenCalledWith("/")
    })
  })
})

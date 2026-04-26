/**
 * Y8 row 2 — ProjectSwitcher dashboard-header dropdown contract.
 *
 * Locks in the operator-visible behaviour:
 *   1. Hidden in auth_mode=open (anon dev environment).
 *   2. Hidden when no user is logged in.
 *   3. Hidden when no current tenant is selected — the dropdown
 *      cannot exist outside a tenant scope.
 *   4. Hidden when loading or when the tenant has zero projects —
 *      no actionable choice for the operator.
 *   5. Renders as a static label (no dropdown) when the tenant has
 *      exactly one project — same noise filter as TenantSwitcher's
 *      single-tenant case.
 *   6. Renders the project list when the tenant has 2+ projects and
 *      the dropdown is opened.
 *   7. Clicking another option calls switchProject(<id>).
 *   8. Archived projects are not switchable (visually muted, button
 *      disabled).
 */

import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))
vi.mock("@/lib/tenant-context", () => ({
  useTenant: vi.fn(),
}))
vi.mock("@/lib/project-context", () => ({
  useProject: vi.fn(),
}))

import { ProjectSwitcher } from "@/components/omnisight/project-switcher"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { useProject } from "@/lib/project-context"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedUseTenant = useTenant as unknown as ReturnType<typeof vi.fn>
const mockedUseProject = useProject as unknown as ReturnType<typeof vi.fn>

const baseUser = {
  id: "user-1",
  email: "u@x.io",
  name: "U",
  role: "admin" as const,
  enabled: true,
  tenant_id: "t-acme",
}

function makeProject(overrides: Partial<{
  project_id: string
  name: string
  archived_at: string | null
  product_line: "embedded" | "web" | "mobile" | "software" | "custom"
}> = {}) {
  return {
    project_id: overrides.project_id ?? "p-1",
    tenant_id: "t-acme",
    product_line: overrides.product_line ?? "embedded",
    name: overrides.name ?? "Project 1",
    slug: "project-1",
    parent_id: null,
    plan_override: null,
    disk_budget_bytes: null,
    llm_budget_tokens: null,
    created_by: "u-1",
    created_at: "2026-04-26 00:00:00",
    archived_at: overrides.archived_at ?? null,
  }
}

function defaultTenantCtx(currentTenantId: string | null = "t-acme") {
  return {
    currentTenantId,
    tenants: [],
    loading: false,
    tenantChangeEpoch: 0,
    switchTenant: vi.fn(),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("ProjectSwitcher visibility", () => {
  it("renders nothing when authMode is open", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "open" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: null,
      projects: [],
      loading: false,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    const { container } = render(<ProjectSwitcher />)
    expect(container.firstChild).toBeNull()
  })

  it("renders nothing when no user is logged in", () => {
    mockedUseAuth.mockReturnValue({ user: null, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: null,
      projects: [],
      loading: false,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    const { container } = render(<ProjectSwitcher />)
    expect(container.firstChild).toBeNull()
  })

  it("renders nothing when there is no active tenant", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx(null))
    mockedUseProject.mockReturnValue({
      currentProjectId: null,
      projects: [],
      loading: false,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    const { container } = render(<ProjectSwitcher />)
    expect(container.firstChild).toBeNull()
  })

  it("renders nothing while the project list is loading", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: null,
      projects: [],
      loading: true,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    const { container } = render(<ProjectSwitcher />)
    expect(container.firstChild).toBeNull()
  })

  it("renders nothing when the tenant has zero projects", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: null,
      projects: [],
      loading: false,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    const { container } = render(<ProjectSwitcher />)
    expect(container.firstChild).toBeNull()
  })

  it("renders a static label (no dropdown) for a single-project tenant", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: "p-1",
      projects: [makeProject({ project_id: "p-1", name: "Solo" })],
      loading: false,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    render(<ProjectSwitcher />)
    expect(screen.getByTestId("project-switcher-static")).toHaveTextContent("Solo")
    expect(screen.queryByTestId("project-switcher-btn")).toBeNull()
  })
})

describe("ProjectSwitcher multi-project dropdown", () => {
  it("shows a button labelled with the current project when 2+ projects are available", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: "p-1",
      projects: [
        makeProject({ project_id: "p-1", name: "ISP Tuning" }),
        makeProject({ project_id: "p-2", name: "Storefront", product_line: "web" }),
      ],
      loading: false,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    render(<ProjectSwitcher />)
    const btn = screen.getByTestId("project-switcher-btn")
    expect(btn).toHaveTextContent("ISP Tuning")
  })

  it("opens the listbox on click and lists every project", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: "p-1",
      projects: [
        makeProject({ project_id: "p-1", name: "ISP Tuning" }),
        makeProject({ project_id: "p-2", name: "Storefront", product_line: "web" }),
        makeProject({ project_id: "p-3", name: "Old Stuff", archived_at: "2026-01-01 00:00:00" }),
      ],
      loading: false,
      projectChangeEpoch: 0,
      switchProject: vi.fn(),
      refetch: vi.fn(),
    })
    render(<ProjectSwitcher />)
    fireEvent.click(screen.getByTestId("project-switcher-btn"))
    expect(screen.getByTestId("project-switcher-list")).toBeInTheDocument()
    expect(screen.getByTestId("project-option-p-1")).toBeInTheDocument()
    expect(screen.getByTestId("project-option-p-2")).toBeInTheDocument()
    expect(screen.getByTestId("project-option-p-3")).toBeInTheDocument()
  })

  it("calls switchProject with the chosen id when the operator picks another project", () => {
    const switchProject = vi.fn()
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: "p-1",
      projects: [
        makeProject({ project_id: "p-1", name: "ISP Tuning" }),
        makeProject({ project_id: "p-2", name: "Storefront", product_line: "web" }),
      ],
      loading: false,
      projectChangeEpoch: 0,
      switchProject,
      refetch: vi.fn(),
    })
    render(<ProjectSwitcher />)
    fireEvent.click(screen.getByTestId("project-switcher-btn"))
    fireEvent.click(screen.getByTestId("project-option-p-2"))

    expect(switchProject).toHaveBeenCalledTimes(1)
    expect(switchProject).toHaveBeenCalledWith("p-2")
  })

  it("does not call switchProject when picking the already-active project", () => {
    const switchProject = vi.fn()
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: "p-1",
      projects: [
        makeProject({ project_id: "p-1", name: "ISP Tuning" }),
        makeProject({ project_id: "p-2", name: "Storefront", product_line: "web" }),
      ],
      loading: false,
      projectChangeEpoch: 0,
      switchProject,
      refetch: vi.fn(),
    })
    render(<ProjectSwitcher />)
    fireEvent.click(screen.getByTestId("project-switcher-btn"))
    fireEvent.click(screen.getByTestId("project-option-p-1"))

    expect(switchProject).not.toHaveBeenCalled()
  })

  it("disables a switch to an archived project", () => {
    const switchProject = vi.fn()
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue(defaultTenantCtx())
    mockedUseProject.mockReturnValue({
      currentProjectId: "p-1",
      projects: [
        makeProject({ project_id: "p-1", name: "ISP Tuning" }),
        makeProject({ project_id: "p-3", name: "Old Stuff", archived_at: "2026-01-01 00:00:00" }),
      ],
      loading: false,
      projectChangeEpoch: 0,
      switchProject,
      refetch: vi.fn(),
    })
    render(<ProjectSwitcher />)
    fireEvent.click(screen.getByTestId("project-switcher-btn"))
    const archivedOpt = screen.getByTestId("project-option-p-3") as HTMLButtonElement
    expect(archivedOpt.disabled).toBe(true)
    fireEvent.click(archivedOpt)
    expect(switchProject).not.toHaveBeenCalled()
  })
})

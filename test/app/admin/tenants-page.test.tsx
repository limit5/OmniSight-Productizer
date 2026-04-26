/**
 * Y8 row 3 — /admin/tenants page contract tests.
 *
 * Locks in the operator-visible behaviour of the new super-admin-only
 * page that tops the existing Y2 (#278) backend admin REST surface:
 *
 *   1. Access gate: non-super_admin sees the 403 placeholder and never
 *      issues a list request. ``authMode === "open"`` (synthetic anon
 *      admin) is treated as super_admin for dev-loop ergonomics.
 *   2. Happy-path list: fetched rows render with id / name / plan /
 *      status badges and per-row enabled-toggle buttons.
 *   3. Plan dropdown change → adminPatchTenant({plan:newPlan}) called.
 *   4. Enable/Disable toggle → adminPatchTenant({enabled: !current}).
 *   5. Server 409 (plan-downgrade refused) surfaces a per-row error
 *      message rather than a global toast.
 *   6. Create dialog rejects locally-invalid id pattern, then on
 *      success refreshes the list.
 */

import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    adminListTenants: vi.fn(),
    adminCreateTenant: vi.fn(),
    adminPatchTenant: vi.fn(),
  }
})

import AdminTenantsPage from "@/app/admin/tenants/page"
import { useAuth } from "@/lib/auth-context"
import {
  adminListTenants,
  adminCreateTenant,
  adminPatchTenant,
  ApiError,
  type AdminTenantRow,
} from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedList = adminListTenants as unknown as ReturnType<typeof vi.fn>
const mockedCreate = adminCreateTenant as unknown as ReturnType<typeof vi.fn>
const mockedPatch = adminPatchTenant as unknown as ReturnType<typeof vi.fn>

const sampleRows: AdminTenantRow[] = [
  {
    id: "t-default",
    name: "Default",
    plan: "free",
    enabled: true,
    created_at: "2026-01-01 00:00:00",
    usage: {
      user_count: 1,
      project_count: 1,
      disk_used_bytes: 1024,
      llm_tokens_30d: 0,
      rate_limit_hits_7d: 0,
      last_activity_at: null,
    },
  },
  {
    id: "t-acme",
    name: "Acme Corp",
    plan: "pro",
    enabled: true,
    created_at: "2026-02-01 00:00:00",
    usage: {
      user_count: 7,
      project_count: 3,
      disk_used_bytes: 5_368_709_120,
      llm_tokens_30d: 4_500_000,
      rate_limit_hits_7d: 0,
      last_activity_at: Math.floor(Date.now() / 1000) - 3600,
    },
  },
  {
    id: "t-beta",
    name: "Beta",
    plan: "starter",
    enabled: false,
    created_at: "2026-03-01 00:00:00",
    usage: {
      user_count: 0,
      project_count: 0,
      disk_used_bytes: 0,
      llm_tokens_30d: 0,
      rate_limit_hits_7d: 0,
      last_activity_at: null,
    },
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  cleanup()
})

describe("/admin/tenants — access gate", () => {
  it("renders the 403 placeholder when the caller is not a super_admin", () => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-1",
        email: "tenant-admin@x.io",
        name: "T",
        role: "admin",
        enabled: true,
        tenant_id: "t-acme",
      },
      authMode: "session",
      loading: false,
    })

    render(<AdminTenantsPage />)
    expect(screen.getByTestId("admin-tenants-forbidden")).toBeInTheDocument()
    expect(mockedList).not.toHaveBeenCalled()
  })

  it("shows a verifying placeholder while auth is loading", () => {
    mockedUseAuth.mockReturnValue({
      user: null,
      authMode: null,
      loading: true,
    })
    render(<AdminTenantsPage />)
    expect(screen.queryByTestId("admin-tenants-page")).not.toBeInTheDocument()
    expect(mockedList).not.toHaveBeenCalled()
  })

  it("treats authMode=open (dev anon admin) as super-admin for the page", async () => {
    mockedUseAuth.mockReturnValue({
      user: null,
      authMode: "open",
      loading: false,
    })
    mockedList.mockResolvedValue({ tenants: [] })
    render(<AdminTenantsPage />)
    await waitFor(() => expect(mockedList).toHaveBeenCalled())
    expect(screen.getByTestId("admin-tenants-page")).toBeInTheDocument()
    expect(screen.getByTestId("admin-tenants-empty")).toBeInTheDocument()
  })
})

describe("/admin/tenants — happy-path list", () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-1",
        email: "root@x.io",
        name: "R",
        role: "super_admin",
        enabled: true,
        tenant_id: "t-default",
      },
      authMode: "session",
      loading: false,
    })
  })

  it("renders one row per tenant with id, name, plan, and status", async () => {
    mockedList.mockResolvedValue({ tenants: sampleRows })
    render(<AdminTenantsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("admin-tenant-row-t-default")).toBeInTheDocument(),
    )
    expect(screen.getByTestId("admin-tenant-row-t-acme")).toBeInTheDocument()
    expect(screen.getByTestId("admin-tenant-row-t-beta")).toBeInTheDocument()
    expect(screen.getByText("Acme Corp")).toBeInTheDocument()
    expect(screen.getByTestId("admin-tenant-status-t-beta")).toHaveTextContent(
      "disabled",
    )
  })

  it("changing plan dropdown calls adminPatchTenant({plan})", async () => {
    mockedList.mockResolvedValue({ tenants: sampleRows })
    mockedPatch.mockResolvedValue({
      ...sampleRows[1],
      plan: "enterprise",
    })
    render(<AdminTenantsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("admin-tenant-row-t-acme")).toBeInTheDocument(),
    )

    const select = screen.getByTestId(
      "admin-tenant-plan-t-acme",
    ) as HTMLSelectElement
    fireEvent.change(select, { target: { value: "enterprise" } })

    await waitFor(() => expect(mockedPatch).toHaveBeenCalledTimes(1))
    expect(mockedPatch).toHaveBeenCalledWith("t-acme", { plan: "enterprise" })
  })

  it("clicking the enable/disable toggle calls adminPatchTenant({enabled: !cur})", async () => {
    mockedList.mockResolvedValue({ tenants: sampleRows })
    mockedPatch.mockResolvedValue({
      ...sampleRows[1],
      enabled: false,
    })
    render(<AdminTenantsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("admin-tenant-toggle-t-acme")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("admin-tenant-toggle-t-acme"))
    await waitFor(() => expect(mockedPatch).toHaveBeenCalledTimes(1))
    expect(mockedPatch).toHaveBeenCalledWith("t-acme", { enabled: false })
  })

  it("server 409 (plan-downgrade refused) is surfaced inline on the row", async () => {
    mockedList.mockResolvedValue({ tenants: sampleRows })
    mockedPatch.mockRejectedValue(
      new ApiError({
        kind: "conflict",
        status: 409,
        body: '{"detail":"plan change refused: tenant uses too much disk"}',
        parsed: { detail: "plan change refused: tenant uses too much disk" },
        traceId: null,
        path: "/admin/tenants/t-acme",
        method: "PATCH",
      }),
    )
    render(<AdminTenantsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("admin-tenant-row-t-acme")).toBeInTheDocument(),
    )
    const select = screen.getByTestId(
      "admin-tenant-plan-t-acme",
    ) as HTMLSelectElement
    fireEvent.change(select, { target: { value: "free" } })
    await waitFor(() =>
      expect(
        screen.getByTestId("admin-tenant-row-error-t-acme"),
      ).toBeInTheDocument(),
    )
    expect(
      screen.getByTestId("admin-tenant-row-error-t-acme"),
    ).toHaveTextContent("plan change refused")
  })
})

describe("/admin/tenants — create dialog", () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-1",
        email: "root@x.io",
        name: "R",
        role: "super_admin",
        enabled: true,
        tenant_id: "t-default",
      },
      authMode: "session",
      loading: false,
    })
  })

  it("rejects a locally-invalid tenant id pattern before calling the API", async () => {
    mockedList.mockResolvedValue({ tenants: [] })
    render(<AdminTenantsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("admin-tenants-create-btn")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId("admin-tenants-create-btn"))
    expect(
      screen.getByTestId("admin-tenants-create-dialog"),
    ).toBeInTheDocument()

    fireEvent.change(screen.getByTestId("admin-tenants-create-id"), {
      target: { value: "BadId!" },
    })
    fireEvent.change(screen.getByTestId("admin-tenants-create-name"), {
      target: { value: "Anything" },
    })
    fireEvent.click(screen.getByTestId("admin-tenants-create-submit"))

    await waitFor(() =>
      expect(
        screen.getByTestId("admin-tenants-create-error"),
      ).toBeInTheDocument(),
    )
    expect(mockedCreate).not.toHaveBeenCalled()
  })

  it("on successful create, refreshes the list and closes the dialog", async () => {
    mockedList.mockResolvedValueOnce({ tenants: [] })
    mockedCreate.mockResolvedValue({
      id: "t-newco",
      name: "Newco",
      plan: "free",
      enabled: true,
      created_at: "2026-04-26 12:00:00",
    })
    mockedList.mockResolvedValueOnce({
      tenants: [
        {
          id: "t-newco",
          name: "Newco",
          plan: "free",
          enabled: true,
          created_at: "2026-04-26 12:00:00",
          usage: {
            user_count: 0,
            project_count: 0,
            disk_used_bytes: 0,
            llm_tokens_30d: 0,
            rate_limit_hits_7d: 0,
            last_activity_at: null,
          },
        },
      ],
    })

    render(<AdminTenantsPage />)
    await waitFor(() =>
      expect(screen.getByTestId("admin-tenants-create-btn")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("admin-tenants-create-btn"))
    fireEvent.change(screen.getByTestId("admin-tenants-create-id"), {
      target: { value: "t-newco" },
    })
    fireEvent.change(screen.getByTestId("admin-tenants-create-name"), {
      target: { value: "Newco" },
    })
    fireEvent.click(screen.getByTestId("admin-tenants-create-submit"))

    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1))
    expect(mockedCreate).toHaveBeenCalledWith({
      id: "t-newco",
      name: "Newco",
      plan: "free",
      enabled: true,
    })

    await waitFor(() =>
      expect(
        screen.queryByTestId("admin-tenants-create-dialog"),
      ).not.toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(screen.getByTestId("admin-tenant-row-t-newco")).toBeInTheDocument(),
    )
    expect(mockedList).toHaveBeenCalledTimes(2)
  })
})

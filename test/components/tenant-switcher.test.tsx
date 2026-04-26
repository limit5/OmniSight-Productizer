/**
 * Y8 row 1 — TenantSwitcher dashboard-header dropdown contract.
 *
 * Locks in the operator-visible behaviour:
 *   1. Hidden in auth_mode=open (anon dev environment) — single tenant
 *      means the picker is noise.
 *   2. Hidden when the user only has one membership and it is the
 *      seed `t-default` — same noise rule.
 *   3. Renders the membership list when the user has 2+ tenants and
 *      the dropdown is opened.
 *   4. Clicking another option calls switchTenant(<id>) — which (per
 *      tenant-context contract tests) flips X-Tenant-Id, clears the
 *      active project, increments tenantChangeEpoch, and notifies
 *      onTenantChange listeners (i.e. provider list / workflow list
 *      refetch).
 *   5. Disabled tenants are not switchable (even though they appear
 *      in the list) — backend already rejects them but the UI must
 *      not pretend the switch happened.
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

import { TenantSwitcher } from "@/components/omnisight/tenant-switcher"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedUseTenant = useTenant as unknown as ReturnType<typeof vi.fn>

const baseUser = {
  id: "user-1",
  email: "u@x.io",
  name: "U",
  role: "admin" as const,
  enabled: true,
  tenant_id: "t-acme",
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("TenantSwitcher visibility", () => {
  it("renders nothing when authMode is open", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "open" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-acme",
      tenants: [],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant: vi.fn(),
    })
    const { container } = render(<TenantSwitcher />)
    expect(container.firstChild).toBeNull()
  })

  it("renders nothing when no user is logged in", () => {
    mockedUseAuth.mockReturnValue({ user: null, authMode: "session" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: null,
      tenants: [],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant: vi.fn(),
    })
    const { container } = render(<TenantSwitcher />)
    expect(container.firstChild).toBeNull()
  })

  it("hides on the t-default-only single-tenant case (single-user dev install)", () => {
    mockedUseAuth.mockReturnValue({ user: { ...baseUser, tenant_id: "t-default" }, authMode: "session" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-default",
      tenants: [{ id: "t-default", name: "Default", plan: "free", enabled: true }],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant: vi.fn(),
    })
    const { container } = render(<TenantSwitcher />)
    expect(container.firstChild).toBeNull()
  })
})

describe("TenantSwitcher multi-tenant dropdown", () => {
  it("shows a button labelled with the current tenant when 2+ tenants are available", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-acme",
      tenants: [
        { id: "t-acme", name: "Acme", plan: "free", enabled: true },
        { id: "t-beta", name: "Beta", plan: "pro", enabled: true },
      ],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant: vi.fn(),
    })
    render(<TenantSwitcher />)
    const btn = screen.getByTestId("tenant-switcher-btn")
    expect(btn).toHaveTextContent("Acme")
  })

  it("opens the listbox on click and lists every membership", () => {
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-acme",
      tenants: [
        { id: "t-acme", name: "Acme", plan: "free", enabled: true },
        { id: "t-beta", name: "Beta", plan: "pro", enabled: true },
        { id: "t-gamma", name: "Gamma", plan: "enterprise", enabled: false },
      ],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant: vi.fn(),
    })
    render(<TenantSwitcher />)
    fireEvent.click(screen.getByTestId("tenant-switcher-btn"))
    expect(screen.getByTestId("tenant-switcher-list")).toBeInTheDocument()
    expect(screen.getByTestId("tenant-option-t-acme")).toBeInTheDocument()
    expect(screen.getByTestId("tenant-option-t-beta")).toBeInTheDocument()
    expect(screen.getByTestId("tenant-option-t-gamma")).toBeInTheDocument()
  })

  it("calls switchTenant with the chosen id when the operator picks another tenant", () => {
    const switchTenant = vi.fn()
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-acme",
      tenants: [
        { id: "t-acme", name: "Acme", plan: "free", enabled: true },
        { id: "t-beta", name: "Beta", plan: "pro", enabled: true },
      ],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant,
    })
    render(<TenantSwitcher />)
    fireEvent.click(screen.getByTestId("tenant-switcher-btn"))
    fireEvent.click(screen.getByTestId("tenant-option-t-beta"))

    expect(switchTenant).toHaveBeenCalledTimes(1)
    expect(switchTenant).toHaveBeenCalledWith("t-beta")
  })

  it("does not call switchTenant when picking the already-active tenant", () => {
    const switchTenant = vi.fn()
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-acme",
      tenants: [
        { id: "t-acme", name: "Acme", plan: "free", enabled: true },
        { id: "t-beta", name: "Beta", plan: "pro", enabled: true },
      ],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant,
    })
    render(<TenantSwitcher />)
    fireEvent.click(screen.getByTestId("tenant-switcher-btn"))
    fireEvent.click(screen.getByTestId("tenant-option-t-acme"))

    expect(switchTenant).not.toHaveBeenCalled()
  })

  it("disables a switch to a tenant where enabled=false", () => {
    const switchTenant = vi.fn()
    mockedUseAuth.mockReturnValue({ user: baseUser, authMode: "session" })
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-acme",
      tenants: [
        { id: "t-acme", name: "Acme", plan: "free", enabled: true },
        { id: "t-gamma", name: "Gamma", plan: "enterprise", enabled: false },
      ],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant,
    })
    render(<TenantSwitcher />)
    fireEvent.click(screen.getByTestId("tenant-switcher-btn"))
    const disabledOpt = screen.getByTestId("tenant-option-t-gamma") as HTMLButtonElement
    expect(disabledOpt.disabled).toBe(true)
    fireEvent.click(disabledOpt)
    expect(switchTenant).not.toHaveBeenCalled()
  })
})

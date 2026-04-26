/**
 * Y8 row 1 — TenantProvider switchTenant contract tests.
 *
 * Locks in the three things the dashboard's TenantSwitcher relies on
 * when the operator flips tenants:
 *   1. The X-Tenant-Id header (api._currentTenantId module-global) is
 *      flipped to the new tenant BEFORE any subscriber is notified —
 *      so a refetch fired by the listener lands on the new tenant.
 *   2. The X-Project-Id header (api._currentProjectId) is cleared at
 *      the same instant — the previous tenant's project id has no
 *      authority under the new tenant and would otherwise leak
 *      across the boundary via lib/api.ts:::request() header injection.
 *   3. tenantChangeEpoch increments and onTenantChange listeners fire,
 *      so React effects (useEngine / useWorkflows / app/page.tsx
 *      provider list) and non-React subscribers re-fetch.
 *
 * If anyone future-changes switchTenant() to defer setCurrentTenantId
 * until AFTER notifying listeners, these tests go red — that ordering
 * mistake is exactly what would cause refetches to race the header
 * update and hit the wrong tenant.
 */

import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, act, waitFor } from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))
vi.mock("@/lib/api", () => ({
  listUserTenants: vi.fn(),
  setCurrentTenantId: vi.fn(),
  setCurrentProjectId: vi.fn(),
}))

import { TenantProvider, useTenant, onTenantChange } from "@/lib/tenant-context"
import { useAuth } from "@/lib/auth-context"
import * as api from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedListUserTenants = api.listUserTenants as unknown as ReturnType<typeof vi.fn>
const mockedSetCurrentTenantId = api.setCurrentTenantId as unknown as ReturnType<typeof vi.fn>
const mockedSetCurrentProjectId = api.setCurrentProjectId as unknown as ReturnType<typeof vi.fn>

interface CapturedCtx {
  currentTenantId: string | null
  tenants: Array<{ id: string; name: string; plan: string; enabled: boolean }>
  loading: boolean
  tenantChangeEpoch: number
  switchTenant: (tid: string) => void
}

let capturedCtx: CapturedCtx | null = null

function CtxCapture(): null {
  capturedCtx = useTenant()
  return null
}

beforeEach(() => {
  vi.clearAllMocks()
  capturedCtx = null
  mockedUseAuth.mockReturnValue({
    user: { id: "user-1", email: "u@x.io", name: "U", role: "admin", enabled: true, tenant_id: "t-acme" },
  })
  mockedListUserTenants.mockResolvedValue([
    { id: "t-acme", name: "Acme", plan: "free", enabled: true },
    { id: "t-beta", name: "Beta", plan: "pro", enabled: true },
  ])
})

describe("Y8 — switchTenant ordering contract", () => {
  it("flips X-Tenant-Id header BEFORE notifying onTenantChange listeners", async () => {
    const callOrder: string[] = []
    mockedSetCurrentTenantId.mockImplementation((tid: string | null) => {
      callOrder.push(`setCurrentTenantId:${tid}`)
    })
    const unsub = onTenantChange((tid) => {
      callOrder.push(`onTenantChange:${tid}`)
    })

    render(
      <TenantProvider>
        <CtxCapture />
      </TenantProvider>
    )
    await waitFor(() => expect(capturedCtx?.tenants.length).toBe(2))

    // The provider's mount effect already called setCurrentTenantId once
    // for the seed tenant; reset so we only measure the switch ordering.
    callOrder.length = 0

    act(() => { capturedCtx?.switchTenant("t-beta") })

    expect(callOrder).toEqual([
      "setCurrentTenantId:t-beta",
      "onTenantChange:t-beta",
    ])
    unsub()
  })

  it("clears the active project id on tenant switch", async () => {
    render(
      <TenantProvider>
        <CtxCapture />
      </TenantProvider>
    )
    await waitFor(() => expect(capturedCtx?.tenants.length).toBe(2))
    mockedSetCurrentProjectId.mockClear()

    act(() => { capturedCtx?.switchTenant("t-beta") })

    expect(mockedSetCurrentProjectId).toHaveBeenCalledWith(null)
  })

  it("increments tenantChangeEpoch on every switch", async () => {
    render(
      <TenantProvider>
        <CtxCapture />
      </TenantProvider>
    )
    await waitFor(() => expect(capturedCtx?.tenants.length).toBe(2))

    const e0 = capturedCtx!.tenantChangeEpoch
    act(() => { capturedCtx?.switchTenant("t-beta") })
    const e1 = capturedCtx!.tenantChangeEpoch
    act(() => { capturedCtx?.switchTenant("t-acme") })
    const e2 = capturedCtx!.tenantChangeEpoch

    expect(e1).toBe(e0 + 1)
    expect(e2).toBe(e1 + 1)
  })

  it("is a no-op when switching to the already-active tenant", async () => {
    render(
      <TenantProvider>
        <CtxCapture />
      </TenantProvider>
    )
    await waitFor(() => expect(capturedCtx?.currentTenantId).toBe("t-acme"))
    const e0 = capturedCtx!.tenantChangeEpoch
    mockedSetCurrentTenantId.mockClear()
    mockedSetCurrentProjectId.mockClear()
    const seen: Array<string | null> = []
    const unsub = onTenantChange((tid) => { seen.push(tid) })

    act(() => { capturedCtx?.switchTenant("t-acme") })

    expect(capturedCtx!.tenantChangeEpoch).toBe(e0)
    expect(mockedSetCurrentTenantId).not.toHaveBeenCalled()
    expect(mockedSetCurrentProjectId).not.toHaveBeenCalled()
    expect(seen).toEqual([])
    unsub()
  })

  it("a thrown listener does not break sibling listeners", async () => {
    render(
      <TenantProvider>
        <CtxCapture />
      </TenantProvider>
    )
    await waitFor(() => expect(capturedCtx?.tenants.length).toBe(2))

    const seen: Array<string | null> = []
    const unsubA = onTenantChange(() => { throw new Error("boom") })
    const unsubB = onTenantChange((tid) => { seen.push(tid) })

    // The console.warn inside the catch is expected — silence it so
    // the test output stays clean without weakening the assertion.
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {})
    act(() => { capturedCtx?.switchTenant("t-beta") })
    warnSpy.mockRestore()

    expect(seen).toEqual(["t-beta"])
    unsubA()
    unsubB()
  })

  it("unsubscribed listeners are not called on subsequent switches", async () => {
    render(
      <TenantProvider>
        <CtxCapture />
      </TenantProvider>
    )
    await waitFor(() => expect(capturedCtx?.tenants.length).toBe(2))

    const seen: Array<string | null> = []
    const unsub = onTenantChange((tid) => { seen.push(tid) })
    act(() => { capturedCtx?.switchTenant("t-beta") })
    unsub()
    act(() => { capturedCtx?.switchTenant("t-acme") })

    expect(seen).toEqual(["t-beta"])
  })
})

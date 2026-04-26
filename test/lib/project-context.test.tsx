/**
 * Y8 row 2 — ProjectProvider switchProject + tenant-coupling contract tests.
 *
 * Locks in the four things ProjectSwitcher relies on:
 *   1. On mount with an active tenant, the provider fetches that
 *      tenant's project list via listTenantProjects(currentTenantId)
 *      and auto-selects the first live project (so X-Project-Id is
 *      populated from t=0 without the operator clicking).
 *   2. When the active tenant flips, the provider clears the local
 *      currentProjectId immediately, then refetches the project list
 *      for the new tenant — i.e. dropdown content tracks the tenant
 *      without a manual reload.
 *   3. switchProject(pid) flips lib/api._currentProjectId BEFORE
 *      notifying onProjectChange listeners — same ordering rule as
 *      tenant-context, otherwise downstream refetches race the
 *      X-Project-Id header.
 *   4. switchProject() is a no-op when picking the already-active
 *      project (no epoch bump, no listener fire, no api call).
 */

import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, act, waitFor } from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))
vi.mock("@/lib/tenant-context", () => ({
  useTenant: vi.fn(),
  onTenantChange: vi.fn(() => () => {}),
}))
vi.mock("@/lib/api", () => ({
  listTenantProjects: vi.fn(),
  setCurrentProjectId: vi.fn(),
}))

import { ProjectProvider, useProject, onProjectChange } from "@/lib/project-context"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import * as api from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedUseTenant = useTenant as unknown as ReturnType<typeof vi.fn>
const mockedListProjects = api.listTenantProjects as unknown as ReturnType<typeof vi.fn>
const mockedSetCurrentProjectId = api.setCurrentProjectId as unknown as ReturnType<typeof vi.fn>

interface CapturedCtx {
  currentProjectId: string | null
  projects: Array<{ project_id: string; name: string; archived_at: string | null }>
  loading: boolean
  projectChangeEpoch: number
  switchProject: (pid: string | null) => void
  refetch: () => void
}

let capturedCtx: CapturedCtx | null = null

function CtxCapture(): null {
  capturedCtx = useProject() as unknown as CapturedCtx
  return null
}

const sampleProjects = [
  {
    project_id: "p-acme-isp",
    tenant_id: "t-acme",
    product_line: "embedded" as const,
    name: "ISP Tuning",
    slug: "isp-tuning",
    parent_id: null,
    plan_override: null,
    disk_budget_bytes: null,
    llm_budget_tokens: null,
    created_by: "u-1",
    created_at: "2026-04-26 00:00:00",
    archived_at: null,
  },
  {
    project_id: "p-acme-web",
    tenant_id: "t-acme",
    product_line: "web" as const,
    name: "Storefront",
    slug: "storefront",
    parent_id: null,
    plan_override: null,
    disk_budget_bytes: null,
    llm_budget_tokens: null,
    created_by: "u-1",
    created_at: "2026-04-26 00:00:00",
    archived_at: null,
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  capturedCtx = null
  mockedUseAuth.mockReturnValue({
    user: { id: "user-1", email: "u@x.io", name: "U", role: "admin", enabled: true, tenant_id: "t-acme" },
  })
  mockedUseTenant.mockReturnValue({
    currentTenantId: "t-acme",
    tenants: [],
    loading: false,
    tenantChangeEpoch: 0,
    switchTenant: vi.fn(),
  })
  mockedListProjects.mockResolvedValue(sampleProjects)
})

describe("Y8 row 2 — ProjectProvider initial fetch", () => {
  it("fetches the project list for the active tenant on mount", async () => {
    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.projects.length).toBe(2))
    expect(mockedListProjects).toHaveBeenCalledWith("t-acme")
  })

  it("auto-selects the first live project so X-Project-Id is populated by default", async () => {
    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-acme-isp"))
    expect(mockedSetCurrentProjectId).toHaveBeenCalledWith("p-acme-isp")
  })

  it("renders an empty projects list and null currentProjectId when there is no active tenant", async () => {
    mockedUseTenant.mockReturnValue({
      currentTenantId: null,
      tenants: [],
      loading: false,
      tenantChangeEpoch: 0,
      switchTenant: vi.fn(),
    })
    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx).not.toBeNull())
    expect(capturedCtx?.projects).toEqual([])
    expect(capturedCtx?.currentProjectId).toBeNull()
    expect(mockedListProjects).not.toHaveBeenCalled()
  })

  it("auto-selects nothing and clears X-Project-Id when the tenant has zero projects", async () => {
    mockedListProjects.mockResolvedValueOnce([])
    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.loading).toBe(false))
    expect(capturedCtx?.currentProjectId).toBeNull()
    expect(mockedSetCurrentProjectId).toHaveBeenLastCalledWith(null)
  })
})

describe("Y8 row 2 — ProjectProvider switchProject ordering contract", () => {
  it("flips _currentProjectId BEFORE notifying onProjectChange listeners", async () => {
    const callOrder: string[] = []
    mockedSetCurrentProjectId.mockImplementation((pid: string | null) => {
      callOrder.push(`setCurrentProjectId:${pid}`)
    })
    const unsub = onProjectChange((pid) => {
      callOrder.push(`onProjectChange:${pid}`)
    })

    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-acme-isp"))
    callOrder.length = 0

    act(() => { capturedCtx?.switchProject("p-acme-web") })

    expect(callOrder).toEqual([
      "setCurrentProjectId:p-acme-web",
      "onProjectChange:p-acme-web",
    ])
    unsub()
  })

  it("increments projectChangeEpoch on every switch", async () => {
    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-acme-isp"))

    const e0 = capturedCtx!.projectChangeEpoch
    act(() => { capturedCtx?.switchProject("p-acme-web") })
    const e1 = capturedCtx!.projectChangeEpoch
    act(() => { capturedCtx?.switchProject("p-acme-isp") })
    const e2 = capturedCtx!.projectChangeEpoch

    expect(e1).toBe(e0 + 1)
    expect(e2).toBe(e1 + 1)
  })

  it("is a no-op when switching to the already-active project", async () => {
    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-acme-isp"))
    const e0 = capturedCtx!.projectChangeEpoch
    mockedSetCurrentProjectId.mockClear()
    const seen: Array<string | null> = []
    const unsub = onProjectChange((pid) => { seen.push(pid) })

    act(() => { capturedCtx?.switchProject("p-acme-isp") })

    expect(capturedCtx!.projectChangeEpoch).toBe(e0)
    expect(mockedSetCurrentProjectId).not.toHaveBeenCalled()
    expect(seen).toEqual([])
    unsub()
  })

  it("a thrown listener does not break sibling listeners", async () => {
    render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-acme-isp"))

    const seen: Array<string | null> = []
    const unsubA = onProjectChange(() => { throw new Error("boom") })
    const unsubB = onProjectChange((pid) => { seen.push(pid) })

    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {})
    act(() => { capturedCtx?.switchProject("p-acme-web") })
    warnSpy.mockRestore()

    expect(seen).toEqual(["p-acme-web"])
    unsubA()
    unsubB()
  })
})

describe("Y8 row 2 — ProjectProvider tenant coupling", () => {
  it("refetches the project list when the active tenant changes", async () => {
    const { rerender } = render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.projects.length).toBe(2))
    expect(mockedListProjects).toHaveBeenCalledTimes(1)
    expect(mockedListProjects).toHaveBeenLastCalledWith("t-acme")

    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-beta",
      tenants: [],
      loading: false,
      tenantChangeEpoch: 1,
      switchTenant: vi.fn(),
    })
    mockedListProjects.mockResolvedValueOnce([
      {
        ...sampleProjects[0],
        project_id: "p-beta-only",
        tenant_id: "t-beta",
        name: "Beta-Only",
      },
    ])

    rerender(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )

    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-beta-only"))
    expect(mockedListProjects).toHaveBeenCalledTimes(2)
    expect(mockedListProjects).toHaveBeenLastCalledWith("t-beta")
  })

  it("clears the local currentProjectId immediately on tenant change (no leak window)", async () => {
    const { rerender } = render(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-acme-isp"))

    let resolveBeta: (v: typeof sampleProjects) => void = () => {}
    const beta = new Promise<typeof sampleProjects>((r) => { resolveBeta = r })
    mockedListProjects.mockReturnValueOnce(beta)
    mockedUseTenant.mockReturnValue({
      currentTenantId: "t-beta",
      tenants: [],
      loading: false,
      tenantChangeEpoch: 1,
      switchTenant: vi.fn(),
    })

    rerender(
      <ProjectProvider>
        <CtxCapture />
      </ProjectProvider>
    )

    // Before the new fetch resolves: currentProjectId must already
    // be null so X-Project-Id can't carry the previous tenant's value.
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBeNull())

    await act(async () => {
      resolveBeta([
        {
          ...sampleProjects[0],
          project_id: "p-beta-1",
          tenant_id: "t-beta",
          name: "Beta-1",
        },
      ])
      await beta
    })
    await waitFor(() => expect(capturedCtx?.currentProjectId).toBe("p-beta-1"))
  })
})

/**
 * Y8 row 6 — IntegrationSettings modal scope label.
 *
 * Locks the contract that the modal header surfaces the active
 * scope (tenant + project) so operators can see which environment
 * a save will land against. Specifically:
 *
 *   1. With both tenant and project mounted, the strip renders
 *      `tenant: <name> (<id>) · project: <name> (<id>)` so the
 *      operator can disambiguate at a glance.
 *   2. With tenant only (no active project — pre-Y8-row-2 install
 *      / archived-only tenant / freshly-switched tenant before the
 *      project picker auto-seeds), the strip degrades to
 *      `tenant: ... · no project selected` so the absence of a
 *      project scope is explicit rather than hidden.
 *   3. Without a TenantProvider mounted (test harness, pre-login
 *      shell), the strip is omitted entirely — the scope hint is
 *      only meaningful when a real session has resolved a tenant.
 *   4. When the tenant id is not in the membership list (post-bootstrap
 *      single-tenant install where TenantProvider seeds a fallback
 *      one-element list with id-as-name), the id alone is rendered —
 *      no `(name)` parenthetical confusion.
 */

import React from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen } from "@testing-library/react"

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    getSettings: vi.fn().mockResolvedValue({}),
    getProviders: vi.fn().mockResolvedValue({ providers: [] }),
    subscribeEvents: vi.fn(() => ({ close: vi.fn() })),
    updateSettings: vi.fn(),
    testIntegration: vi.fn(),
    testGitForgeToken: vi.fn(),
    getGitTokenMap: vi.fn().mockResolvedValue({
      github: { instances: [] },
      gitlab: { instances: [] },
    }),
    updateGitTokenMap: vi.fn(),
    getGitForgeSshPubkey: vi.fn(),
    verifyGerritMergerBot: vi.fn(),
    verifyGerritSubmitRule: vi.fn(),
    getGerritWebhookInfo: vi.fn(),
    generateGerritWebhookSecret: vi.fn(),
    finalizeGerritIntegration: vi.fn(),
  }
})

vi.mock("@/lib/tenant-context", () => ({
  useTenantOptional: vi.fn(),
}))
vi.mock("@/lib/project-context", () => ({
  useProjectOptional: vi.fn(),
}))

import { IntegrationSettings } from "@/components/omnisight/integration-settings"
import { useTenantOptional } from "@/lib/tenant-context"
import { useProjectOptional } from "@/lib/project-context"

const mockedUseTenantOptional = useTenantOptional as unknown as ReturnType<typeof vi.fn>
const mockedUseProjectOptional = useProjectOptional as unknown as ReturnType<typeof vi.fn>

// Use `in` checks rather than `??` so explicit null overrides aren't
// collapsed to defaults — the "no current project" / "no current tenant"
// cases below rely on null surviving the merge.
function makeTenantCtx(overrides: Partial<{
  currentTenantId: string | null
  tenants: Array<{ id: string; name: string; plan: string; enabled: boolean }>
}> = {}) {
  return {
    currentTenantId: "currentTenantId" in overrides
      ? overrides.currentTenantId as string | null
      : "t-acme",
    tenants: "tenants" in overrides
      ? overrides.tenants!
      : [{ id: "t-acme", name: "Acme Robotics", plan: "pro", enabled: true }],
    loading: false,
    tenantChangeEpoch: 0,
    switchTenant: vi.fn(),
  }
}

function makeProjectCtx(overrides: Partial<{
  currentProjectId: string | null
  projects: Array<{ project_id: string; name: string; archived_at: string | null }>
}> = {}) {
  return {
    currentProjectId: "currentProjectId" in overrides
      ? overrides.currentProjectId as string | null
      : "p-firmware-alpha",
    // The component only reads project_id + name, so the fixture is
    // intentionally minimal vs the full TenantProjectInfo shape.
    projects: ("projects" in overrides
      ? overrides.projects!
      : [{ project_id: "p-firmware-alpha", name: "Firmware Alpha", archived_at: null }]
    ) as unknown as ReturnType<typeof useProjectOptional> extends infer T
      ? T extends { projects: infer P } ? P : never : never,
    loading: false,
    projectChangeEpoch: 0,
    switchProject: vi.fn(),
    refetch: vi.fn(),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("IntegrationSettings — Y8 row 6 scope label", () => {
  it("renders tenant + project names with ids when both are active", () => {
    mockedUseTenantOptional.mockReturnValue(makeTenantCtx())
    mockedUseProjectOptional.mockReturnValue(makeProjectCtx())

    render(<IntegrationSettings open={true} onClose={() => {}} />)

    const label = screen.getByTestId("integration-settings-scope-label")
    expect(label).toBeTruthy()

    const tenantSpan = screen.getByTestId("integration-settings-scope-tenant")
    expect(tenantSpan.textContent).toContain("Acme Robotics")
    expect(tenantSpan.textContent).toContain("t-acme")

    const projectSpan = screen.getByTestId("integration-settings-scope-project")
    expect(projectSpan.textContent).toContain("Firmware Alpha")
    expect(projectSpan.textContent).toContain("p-firmware-alpha")

    // The "no project" fallback must NOT render when a project is selected.
    expect(screen.queryByTestId("integration-settings-scope-project-empty"))
      .toBeNull()
  })

  it("falls back to 'no project selected' when project context is mounted but currentProjectId is null", () => {
    mockedUseTenantOptional.mockReturnValue(makeTenantCtx())
    mockedUseProjectOptional.mockReturnValue(
      makeProjectCtx({ currentProjectId: null, projects: [] }),
    )

    render(<IntegrationSettings open={true} onClose={() => {}} />)

    expect(screen.getByTestId("integration-settings-scope-label")).toBeTruthy()
    expect(screen.getByTestId("integration-settings-scope-tenant").textContent)
      .toContain("Acme Robotics")
    expect(screen.queryByTestId("integration-settings-scope-project")).toBeNull()
    const empty = screen.getByTestId("integration-settings-scope-project-empty")
    expect(empty.textContent).toContain("no project selected")
  })

  it("falls back to 'no project selected' when no <ProjectProvider> is mounted", () => {
    mockedUseTenantOptional.mockReturnValue(makeTenantCtx())
    mockedUseProjectOptional.mockReturnValue(null)

    render(<IntegrationSettings open={true} onClose={() => {}} />)

    expect(screen.getByTestId("integration-settings-scope-label")).toBeTruthy()
    expect(screen.queryByTestId("integration-settings-scope-project")).toBeNull()
    expect(screen.getByTestId("integration-settings-scope-project-empty")).toBeTruthy()
  })

  it("omits the scope strip entirely when no <TenantProvider> is mounted", () => {
    mockedUseTenantOptional.mockReturnValue(null)
    mockedUseProjectOptional.mockReturnValue(null)

    render(<IntegrationSettings open={true} onClose={() => {}} />)

    expect(screen.queryByTestId("integration-settings-scope-label")).toBeNull()
    // Title still renders — we only omit the scope row, not the modal.
    expect(screen.getByText("SYSTEM INTEGRATIONS")).toBeTruthy()
  })

  it("omits the scope strip when tenant context is mounted but currentTenantId is null (pre-login)", () => {
    mockedUseTenantOptional.mockReturnValue(
      makeTenantCtx({ currentTenantId: null, tenants: [] }),
    )
    mockedUseProjectOptional.mockReturnValue(null)

    render(<IntegrationSettings open={true} onClose={() => {}} />)

    expect(screen.queryByTestId("integration-settings-scope-label")).toBeNull()
  })

  it("renders the bare tenant id when the membership list does not include the active tenant", () => {
    // Mirrors the bootstrap-fallback behaviour where TenantProvider seeds
    // a one-element membership list with id-as-name on `listUserTenants`
    // failure — and the post-switch state where the active id may briefly
    // not appear in the list. Either way, no parenthetical name should
    // sneak in to confuse the operator.
    mockedUseTenantOptional.mockReturnValue(
      makeTenantCtx({ currentTenantId: "t-mystery", tenants: [] }),
    )
    mockedUseProjectOptional.mockReturnValue(
      makeProjectCtx({ currentProjectId: null, projects: [] }),
    )

    render(<IntegrationSettings open={true} onClose={() => {}} />)

    const tenantSpan = screen.getByTestId("integration-settings-scope-tenant")
    expect(tenantSpan.textContent).toContain("t-mystery")
    expect(tenantSpan.textContent).not.toContain("(t-mystery)")
  })

  it("renders the bare project id when the project list does not include the active project", () => {
    mockedUseTenantOptional.mockReturnValue(makeTenantCtx())
    mockedUseProjectOptional.mockReturnValue(
      makeProjectCtx({ currentProjectId: "p-orphan", projects: [] }),
    )

    render(<IntegrationSettings open={true} onClose={() => {}} />)

    const projectSpan = screen.getByTestId("integration-settings-scope-project")
    expect(projectSpan.textContent).toContain("p-orphan")
    expect(projectSpan.textContent).not.toContain("(p-orphan)")
  })
})

/**
 * Y8 row 5 — /projects/{pid}/settings page contract tests.
 *
 * Locks in the operator-visible behaviour of the new project-owner
 * page on top of the Y4 row 5 / row 6 + Y8 row 5 backend surfaces:
 *
 *   1. Access gate: viewer → 403 placeholder + no API call;
 *      invalid pid → bad-id placeholder + no API call;
 *      no current tenant → "select tenant first" placeholder;
 *      project not in current tenant → "not found" placeholder.
 *   2. Members tab: list rows, role-dropdown PATCH, remove (DELETE),
 *      add-member dialog with USER_ID_PATTERN local-validate, server
 *      409 surfaced inline.
 *   3. Budget tab: read current values + PATCH plan/disk/llm budgets;
 *      empty input → null = inherit from tenant.
 *   4. Shares tab: list rows, grant cross-tenant share (TENANT_ID
 *      regex + self-share local-block), revoke (DELETE).
 */

import React, { Suspense } from "react"
import { describe, expect, it, vi, beforeEach } from "vitest"
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
  act,
} from "@testing-library/react"

vi.mock("@/lib/auth-context", () => ({
  useAuth: vi.fn(),
}))

vi.mock("@/lib/tenant-context", () => ({
  useTenant: vi.fn(),
  onTenantChange: vi.fn(() => () => {}),
}))

vi.mock("@/lib/project-context", () => ({
  useProject: vi.fn(),
}))

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    listProjectMembers: vi.fn(),
    createProjectMember: vi.fn(),
    patchProjectMember: vi.fn(),
    deleteProjectMember: vi.fn(),
    patchProjectBudget: vi.fn(),
    listProjectShares: vi.fn(),
    createProjectShare: vi.fn(),
    deleteProjectShare: vi.fn(),
  }
})

import ProjectSettingsPage from "@/app/projects/[pid]/settings/page"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { useProject } from "@/lib/project-context"
import {
  ApiError,
  createProjectMember,
  createProjectShare,
  deleteProjectMember,
  deleteProjectShare,
  listProjectMembers,
  listProjectShares,
  patchProjectBudget,
  patchProjectMember,
} from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedUseTenant = useTenant as unknown as ReturnType<typeof vi.fn>
const mockedUseProject = useProject as unknown as ReturnType<typeof vi.fn>
const mockedListMembers = listProjectMembers as unknown as ReturnType<typeof vi.fn>
const mockedCreateMember = createProjectMember as unknown as ReturnType<typeof vi.fn>
const mockedPatchMember = patchProjectMember as unknown as ReturnType<typeof vi.fn>
const mockedDeleteMember = deleteProjectMember as unknown as ReturnType<typeof vi.fn>
const mockedPatchBudget = patchProjectBudget as unknown as ReturnType<typeof vi.fn>
const mockedListShares = listProjectShares as unknown as ReturnType<typeof vi.fn>
const mockedCreateShare = createProjectShare as unknown as ReturnType<typeof vi.fn>
const mockedDeleteShare = deleteProjectShare as unknown as ReturnType<typeof vi.fn>

const TID = "t-acme"
const PID = "p-fw0001"

const sampleProject = {
  project_id: PID,
  tenant_id: TID,
  product_line: "embedded" as const,
  name: "Firmware",
  slug: "firmware",
  parent_id: null,
  plan_override: null,
  disk_budget_bytes: null,
  llm_budget_tokens: null,
  created_by: "u-alice0001",
  created_at: "2026-04-01 12:00:00",
  archived_at: null,
}

const sampleMembers = [
  {
    user_id: "u-alice0001",
    email: "alice@x.io",
    name: "alice",
    project_id: PID,
    role: "owner" as const,
    created_at: "2026-04-01 12:00:00",
    user_enabled: true,
  },
  {
    user_id: "u-bob0001",
    email: "bob@x.io",
    name: "bob",
    project_id: PID,
    role: "contributor" as const,
    created_at: "2026-04-10 12:00:00",
    user_enabled: true,
  },
]

const sampleShare = {
  share_id: "psh-aaaabbbbcccc1111",
  project_id: PID,
  guest_tenant_id: "t-bob",
  role: "viewer" as const,
  granted_by: "u-alice0001",
  created_at: "2026-04-15 12:00:00",
  expires_at: null,
}

function makeParams(pid: string = PID) {
  return Promise.resolve({ pid })
}

async function renderPage(pid: string = PID) {
  await act(async () => {
    render(
      <Suspense fallback={null}>
        <ProjectSettingsPage params={makeParams(pid)} />
      </Suspense>,
    )
  })
}

function setAdmin() {
  mockedUseAuth.mockReturnValue({
    user: {
      id: "u-1",
      email: "admin@x.io",
      name: "A",
      role: "admin",
      enabled: true,
      tenant_id: TID,
    },
    authMode: "session",
    loading: false,
  })
}

function setTenantAndProject(opts?: {
  currentTenantId?: string | null
  projects?: typeof sampleProject[]
  loading?: boolean
}) {
  const tenantId = "currentTenantId" in (opts ?? {})
    ? (opts!.currentTenantId as string | null)
    : TID
  mockedUseTenant.mockReturnValue({
    currentTenantId: tenantId,
    tenants: [],
    loading: false,
    tenantChangeEpoch: 0,
    switchTenant: vi.fn(),
    refetch: vi.fn(),
  })
  mockedUseProject.mockReturnValue({
    currentProjectId: PID,
    projects: opts?.projects ?? [sampleProject],
    loading: opts?.loading ?? false,
    projectChangeEpoch: 0,
    switchProject: vi.fn(),
    refetch: vi.fn(),
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  cleanup()
  mockedListMembers.mockResolvedValue({
    tenant_id: TID,
    project_id: PID,
    count: sampleMembers.length,
    members: sampleMembers,
  })
  mockedListShares.mockResolvedValue({
    tenant_id: TID,
    project_id: PID,
    count: 1,
    shares: [sampleShare],
  })
})

// ─── Access gate ────────────────────────────────────────────────

describe("/projects/{pid}/settings — access gate", () => {
  it("renders 403 placeholder for a viewer + no API call", async () => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-v",
        email: "v@x.io",
        name: "V",
        role: "viewer",
        enabled: true,
        tenant_id: TID,
      },
      authMode: "session",
      loading: false,
    })
    setTenantAndProject()
    await renderPage()
    expect(
      await screen.findByTestId("project-settings-forbidden"),
    ).toBeInTheDocument()
    expect(mockedListMembers).not.toHaveBeenCalled()
  })

  it("rejects an invalid pid path param without calling the API", async () => {
    setAdmin()
    setTenantAndProject()
    await renderPage("not-a-pid")
    expect(
      await screen.findByTestId("project-settings-bad-id"),
    ).toBeInTheDocument()
    expect(mockedListMembers).not.toHaveBeenCalled()
  })

  it("shows 'select tenant first' when no current tenant", async () => {
    setAdmin()
    setTenantAndProject({ currentTenantId: null, projects: [] })
    await renderPage()
    expect(
      await screen.findByTestId("project-settings-no-tenant"),
    ).toBeInTheDocument()
    expect(mockedListMembers).not.toHaveBeenCalled()
  })

  it("shows 'project not found' when pid is not in the current tenant", async () => {
    setAdmin()
    setTenantAndProject({ projects: [] })
    await renderPage()
    expect(
      await screen.findByTestId("project-settings-not-found"),
    ).toBeInTheDocument()
    expect(mockedListMembers).not.toHaveBeenCalled()
  })

  it("treats authMode=open (dev anon admin) as allowed", async () => {
    mockedUseAuth.mockReturnValue({
      user: null,
      authMode: "open",
      loading: false,
    })
    setTenantAndProject()
    await renderPage()
    await waitFor(() => expect(mockedListMembers).toHaveBeenCalled())
    expect(screen.getByTestId("project-settings-page")).toBeInTheDocument()
  })
})

// ─── Members tab ────────────────────────────────────────────────

describe("/projects/{pid}/settings — Members tab", () => {
  beforeEach(() => {
    setAdmin()
    setTenantAndProject()
  })

  it("renders rows with email + name from the GET response", async () => {
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("project-member-row-u-alice0001")).toBeInTheDocument(),
    )
    expect(screen.getByText("alice@x.io")).toBeInTheDocument()
    expect(screen.getByText("bob@x.io")).toBeInTheDocument()
    expect(mockedListMembers).toHaveBeenCalledWith(TID, PID)
  })

  it("changing role dropdown calls patchProjectMember", async () => {
    mockedPatchMember.mockResolvedValue({
      ...sampleMembers[1],
      role: "owner",
      tenant_id: TID,
      no_change: false,
    })
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("project-member-role-u-bob0001")).toBeInTheDocument(),
    )
    fireEvent.change(screen.getByTestId("project-member-role-u-bob0001"), {
      target: { value: "owner" },
    })
    await waitFor(() => expect(mockedPatchMember).toHaveBeenCalledTimes(1))
    expect(mockedPatchMember).toHaveBeenCalledWith(TID, PID, "u-bob0001", {
      role: "owner",
    })
  })

  it("Remove button calls deleteProjectMember and refetches", async () => {
    mockedDeleteMember.mockResolvedValue({
      tenant_id: TID,
      project_id: PID,
      user_id: "u-bob0001",
      already_removed: false,
      role: "contributor",
    })
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("project-member-remove-u-bob0001")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId("project-member-remove-u-bob0001"))
    await waitFor(() => expect(mockedDeleteMember).toHaveBeenCalledTimes(1))
    expect(mockedDeleteMember).toHaveBeenCalledWith(TID, PID, "u-bob0001")
    await waitFor(() => expect(mockedListMembers).toHaveBeenCalledTimes(2))
  })

  it("server 409 on PATCH is surfaced inline on the row", async () => {
    mockedPatchMember.mockRejectedValue(
      new ApiError({
        kind: "conflict",
        status: 409,
        body: '{"detail":"refuses to demote last admin"}',
        parsed: { detail: "refuses to demote last admin" },
        traceId: null,
        path: `/tenants/${TID}/projects/${PID}/members/u-alice0001`,
        method: "PATCH",
      }),
    )
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("project-member-row-u-alice0001")).toBeInTheDocument(),
    )
    fireEvent.change(screen.getByTestId("project-member-role-u-alice0001"), {
      target: { value: "viewer" },
    })
    await waitFor(() =>
      expect(screen.getByTestId("project-member-row-error-u-alice0001")).toBeInTheDocument(),
    )
    expect(screen.getByTestId("project-member-row-error-u-alice0001"))
      .toHaveTextContent("last admin")
  })

  it("add-member dialog rejects an invalid user_id locally", async () => {
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("project-members-create-btn")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId("project-members-create-btn"))
    fireEvent.change(screen.getByTestId("project-members-create-user-id"), {
      target: { value: "not-a-uid" },
    })
    fireEvent.click(screen.getByTestId("project-members-create-submit"))

    expect(
      await screen.findByTestId("project-members-create-error"),
    ).toBeInTheDocument()
    expect(mockedCreateMember).not.toHaveBeenCalled()
  })

  it("add-member happy path POSTs and refetches", async () => {
    mockedCreateMember.mockResolvedValue({
      ...sampleMembers[1],
      user_id: "u-newuser1234",
      email: "new@x.io",
      tenant_id: TID,
    })
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("project-members-create-btn")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId("project-members-create-btn"))
    fireEvent.change(screen.getByTestId("project-members-create-user-id"), {
      target: { value: "u-newuser1234" },
    })
    fireEvent.change(screen.getByTestId("project-members-create-role"), {
      target: { value: "owner" },
    })
    fireEvent.click(screen.getByTestId("project-members-create-submit"))

    await waitFor(() => expect(mockedCreateMember).toHaveBeenCalledTimes(1))
    expect(mockedCreateMember).toHaveBeenCalledWith(TID, PID, {
      user_id: "u-newuser1234",
      role: "owner",
    })
    await waitFor(() => expect(mockedListMembers).toHaveBeenCalledTimes(2))
  })
})

// ─── Budget tab ─────────────────────────────────────────────────

describe("/projects/{pid}/settings — Budget tab", () => {
  beforeEach(() => {
    setAdmin()
    setTenantAndProject()
  })

  it("renders current values from the project (inherit when null)", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-budget"))
    await waitFor(() =>
      expect(screen.getByTestId("project-budget-form")).toBeInTheDocument(),
    )
    expect(screen.getByTestId("project-budget-current-plan")).toHaveTextContent(
      "(inherit from tenant)",
    )
    expect(screen.getByTestId("project-budget-current-disk")).toHaveTextContent(
      "(inherit from tenant)",
    )
    expect(screen.getByTestId("project-budget-current-llm")).toHaveTextContent(
      "(inherit from tenant)",
    )
  })

  it("submits override values to patchProjectBudget", async () => {
    mockedPatchBudget.mockResolvedValue({
      ...sampleProject,
      plan_override: "pro",
      disk_budget_bytes: 1073741824,
      llm_budget_tokens: 1000000,
    })
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-budget"))
    await waitFor(() =>
      expect(screen.getByTestId("project-budget-submit")).toBeInTheDocument(),
    )

    fireEvent.change(screen.getByTestId("project-budget-plan"), {
      target: { value: "pro" },
    })
    fireEvent.change(screen.getByTestId("project-budget-disk"), {
      target: { value: "1073741824" },
    })
    fireEvent.change(screen.getByTestId("project-budget-llm"), {
      target: { value: "1000000" },
    })
    fireEvent.click(screen.getByTestId("project-budget-submit"))

    await waitFor(() => expect(mockedPatchBudget).toHaveBeenCalledTimes(1))
    expect(mockedPatchBudget).toHaveBeenCalledWith(TID, PID, {
      plan_override: "pro",
      disk_budget_bytes: 1073741824,
      llm_budget_tokens: 1000000,
    })
    expect(
      await screen.findByTestId("project-budget-success"),
    ).toBeInTheDocument()
  })

  it("empty inputs translate to null (clear = inherit from tenant)", async () => {
    mockedPatchBudget.mockResolvedValue({
      ...sampleProject,
      no_change: true,
    })
    // Seed a project that already has overrides — verify Save with
    // emptied inputs sends null (clear the override).
    setTenantAndProject({
      projects: [
        {
          ...sampleProject,
          plan_override: "starter",
          disk_budget_bytes: 999,
          llm_budget_tokens: 1234,
        },
      ],
    })
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-budget"))
    await waitFor(() =>
      expect(screen.getByTestId("project-budget-submit")).toBeInTheDocument(),
    )

    fireEvent.change(screen.getByTestId("project-budget-plan"), {
      target: { value: "" },
    })
    fireEvent.change(screen.getByTestId("project-budget-disk"), {
      target: { value: "" },
    })
    fireEvent.change(screen.getByTestId("project-budget-llm"), {
      target: { value: "" },
    })
    fireEvent.click(screen.getByTestId("project-budget-submit"))

    await waitFor(() => expect(mockedPatchBudget).toHaveBeenCalledTimes(1))
    expect(mockedPatchBudget).toHaveBeenCalledWith(TID, PID, {
      plan_override: null,
      disk_budget_bytes: null,
      llm_budget_tokens: null,
    })
  })

  it("rejects negative or non-integer disk budgets locally", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-budget"))
    await waitFor(() =>
      expect(screen.getByTestId("project-budget-submit")).toBeInTheDocument(),
    )
    fireEvent.change(screen.getByTestId("project-budget-disk"), {
      target: { value: "-5" },
    })
    fireEvent.click(screen.getByTestId("project-budget-submit"))

    expect(
      await screen.findByTestId("project-budget-error"),
    ).toBeInTheDocument()
    expect(mockedPatchBudget).not.toHaveBeenCalled()
  })

  it("server 409 (oversell) is surfaced inline", async () => {
    mockedPatchBudget.mockRejectedValue(
      new ApiError({
        kind: "conflict",
        status: 409,
        body: '{"detail":"would oversell tenant quota"}',
        parsed: { detail: "would oversell tenant quota" },
        traceId: null,
        path: `/tenants/${TID}/projects/${PID}`,
        method: "PATCH",
      }),
    )
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-budget"))
    await waitFor(() =>
      expect(screen.getByTestId("project-budget-submit")).toBeInTheDocument(),
    )
    fireEvent.change(screen.getByTestId("project-budget-disk"), {
      target: { value: "999999999999" },
    })
    fireEvent.click(screen.getByTestId("project-budget-submit"))

    expect(
      await screen.findByTestId("project-budget-error"),
    ).toBeInTheDocument()
    expect(screen.getByTestId("project-budget-error")).toHaveTextContent(
      "oversell",
    )
  })
})

// ─── Shares tab ─────────────────────────────────────────────────

describe("/projects/{pid}/settings — Shares tab", () => {
  beforeEach(() => {
    setAdmin()
    setTenantAndProject()
  })

  it("lists shares from listProjectShares", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-shares"))
    await waitFor(() =>
      expect(screen.getByTestId(`project-share-row-${sampleShare.share_id}`))
        .toBeInTheDocument(),
    )
    expect(screen.getByText("t-bob")).toBeInTheDocument()
    expect(mockedListShares).toHaveBeenCalledWith(TID, PID)
  })

  it("Revoke button calls deleteProjectShare and refetches", async () => {
    mockedDeleteShare.mockResolvedValue({
      tenant_id: TID,
      project_id: PID,
      share_id: sampleShare.share_id,
      already_revoked: false,
      guest_tenant_id: "t-bob",
      role: "viewer",
    })
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-shares"))
    await waitFor(() =>
      expect(screen.getByTestId(`project-share-revoke-${sampleShare.share_id}`))
        .toBeInTheDocument(),
    )
    fireEvent.click(
      screen.getByTestId(`project-share-revoke-${sampleShare.share_id}`),
    )
    await waitFor(() => expect(mockedDeleteShare).toHaveBeenCalledTimes(1))
    expect(mockedDeleteShare).toHaveBeenCalledWith(
      TID, PID, sampleShare.share_id,
    )
    await waitFor(() => expect(mockedListShares).toHaveBeenCalledTimes(2))
  })

  it("create dialog rejects malformed guest tenant id locally", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-shares"))
    await waitFor(() =>
      expect(screen.getByTestId("project-shares-create-btn")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("project-shares-create-btn"))
    fireEvent.change(screen.getByTestId("project-shares-create-guest"), {
      target: { value: "not-a-tid" },
    })
    fireEvent.click(screen.getByTestId("project-shares-create-submit"))

    expect(
      await screen.findByTestId("project-shares-create-error"),
    ).toBeInTheDocument()
    expect(mockedCreateShare).not.toHaveBeenCalled()
  })

  it("create dialog blocks self-share locally (guest === owning tenant)", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-shares"))
    await waitFor(() =>
      expect(screen.getByTestId("project-shares-create-btn")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("project-shares-create-btn"))
    fireEvent.change(screen.getByTestId("project-shares-create-guest"), {
      target: { value: TID },
    })
    fireEvent.click(screen.getByTestId("project-shares-create-submit"))

    expect(
      await screen.findByTestId("project-shares-create-error"),
    ).toHaveTextContent("Cannot share")
    expect(mockedCreateShare).not.toHaveBeenCalled()
  })

  it("happy path grant share POSTs and refetches", async () => {
    mockedCreateShare.mockResolvedValue({
      ...sampleShare,
      share_id: "psh-new0123456789",
      guest_tenant_id: "t-newco",
      tenant_id: TID,
    })
    await renderPage()
    fireEvent.click(await screen.findByTestId("project-settings-tab-shares"))
    await waitFor(() =>
      expect(screen.getByTestId("project-shares-create-btn")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("project-shares-create-btn"))
    fireEvent.change(screen.getByTestId("project-shares-create-guest"), {
      target: { value: "t-newco" },
    })
    fireEvent.change(screen.getByTestId("project-shares-create-role"), {
      target: { value: "contributor" },
    })
    fireEvent.click(screen.getByTestId("project-shares-create-submit"))

    await waitFor(() => expect(mockedCreateShare).toHaveBeenCalledTimes(1))
    expect(mockedCreateShare).toHaveBeenCalledWith(TID, PID, {
      guest_tenant_id: "t-newco",
      role: "contributor",
      expires_at: null,
    })
    await waitFor(() => expect(mockedListShares).toHaveBeenCalledTimes(2))
  })
})

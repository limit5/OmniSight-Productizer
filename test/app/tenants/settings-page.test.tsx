/**
 * Y8 row 4 — /tenants/{tid}/settings page contract tests.
 *
 * Locks in the operator-visible behaviour of the new tenant-admin
 * page on top of Y3 / Y4 / storage backend surfaces:
 *
 *   1. Access gate: viewer / operator → 403 placeholder, no API
 *      calls. tenant admin / super_admin → full UI. authMode=open
 *      (synthetic anon admin) treated as admin for dev-loop ergonomics.
 *   2. Tab routing: Members default; switching to each tab triggers
 *      that tab's list endpoint.
 *   3. Members tab: list rows, role-dropdown PATCH, remove (DELETE),
 *      server 409 surfaced inline (last-admin floor).
 *   4. Invites tab: create dialog → POST + token shown once;
 *      pending list → revoke (DELETE).
 *   5. Projects tab: create with slug regex, archive, restore.
 *   6. Quotas tab: plan + soft/hard breakdown rendered from
 *      /storage/usage payload.
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

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    listTenantMembers: vi.fn(),
    patchTenantMember: vi.fn(),
    deleteTenantMember: vi.fn(),
    listTenantInvites: vi.fn(),
    createTenantInvite: vi.fn(),
    revokeTenantInvite: vi.fn(),
    listAllTenantProjects: vi.fn(),
    createTenantProject: vi.fn(),
    archiveTenantProject: vi.fn(),
    restoreTenantProject: vi.fn(),
    getStorageUsage: vi.fn(),
  }
})

import TenantSettingsPage from "@/app/tenants/[tid]/settings/page"
import { useAuth } from "@/lib/auth-context"
import {
  ApiError,
  archiveTenantProject,
  createTenantInvite,
  createTenantProject,
  deleteTenantMember,
  getStorageUsage,
  listAllTenantProjects,
  listTenantInvites,
  listTenantMembers,
  patchTenantMember,
  restoreTenantProject,
  revokeTenantInvite,
} from "@/lib/api"

const mockedUseAuth = useAuth as unknown as ReturnType<typeof vi.fn>
const mockedListMembers = listTenantMembers as unknown as ReturnType<typeof vi.fn>
const mockedPatchMember = patchTenantMember as unknown as ReturnType<typeof vi.fn>
const mockedDeleteMember = deleteTenantMember as unknown as ReturnType<typeof vi.fn>
const mockedListInvites = listTenantInvites as unknown as ReturnType<typeof vi.fn>
const mockedCreateInvite = createTenantInvite as unknown as ReturnType<typeof vi.fn>
const mockedRevokeInvite = revokeTenantInvite as unknown as ReturnType<typeof vi.fn>
const mockedListAllProjects = listAllTenantProjects as unknown as ReturnType<typeof vi.fn>
const mockedCreateProject = createTenantProject as unknown as ReturnType<typeof vi.fn>
const mockedArchiveProject = archiveTenantProject as unknown as ReturnType<typeof vi.fn>
const mockedRestoreProject = restoreTenantProject as unknown as ReturnType<typeof vi.fn>
const mockedGetUsage = getStorageUsage as unknown as ReturnType<typeof vi.fn>

const TID = "t-acme"

function makeParams(tid: string = TID) {
  return Promise.resolve({ tid })
}

// React 19's ``use(params)`` suspends the component until the
// path-param Promise resolves. Wrapping the render in
// ``await act(async () => …)`` flushes the pending microtask + the
// useEffect mount-fetch cycle so the test can immediately query for
// rendered DOM nodes.
async function renderPage(tid: string = TID) {
  await act(async () => {
    render(
      <Suspense fallback={null}>
        <TenantSettingsPage params={makeParams(tid)} />
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

const sampleMembers = [
  {
    user_id: "u-alice",
    email: "alice@x.io",
    name: "Alice",
    role: "owner" as const,
    status: "active" as const,
    user_enabled: true,
    joined_at: "2026-04-01 12:00:00",
    last_active_at: null,
  },
  {
    user_id: "u-bob",
    email: "bob@x.io",
    name: "Bob",
    role: "member" as const,
    status: "active" as const,
    user_enabled: true,
    joined_at: "2026-04-10 12:00:00",
    last_active_at: null,
  },
]

const sampleInvite = {
  invite_id: "inv-001",
  email: "carol@x.io",
  role: "member" as const,
  status: "pending" as const,
  invited_by: "u-alice",
  created_at: "2026-04-20 12:00:00",
  expires_at: "2026-04-27 12:00:00",
}

const sampleProject = {
  project_id: "p-fw",
  tenant_id: TID,
  product_line: "embedded" as const,
  name: "Firmware",
  slug: "firmware",
  parent_id: null,
  plan_override: null,
  disk_budget_bytes: null,
  llm_budget_tokens: null,
  created_by: "u-alice",
  created_at: "2026-04-01 12:00:00",
  archived_at: null,
}

const sampleArchived = {
  ...sampleProject,
  project_id: "p-old",
  name: "Old",
  slug: "old",
  archived_at: "2026-04-15 12:00:00",
}

const sampleUsage = {
  tenant_id: TID,
  plan: "pro",
  quota: { soft_bytes: 100 * 1024 ** 3, hard_bytes: 200 * 1024 ** 3, keep_recent_runs: 50 },
  usage: {
    artifacts_bytes: 1 * 1024 ** 3,
    workflow_runs_bytes: 500 * 1024 ** 2,
    backups_bytes: 100 * 1024 ** 2,
    ingest_tmp_bytes: 0,
    total_bytes: 1.6 * 1024 ** 3,
  },
  over_soft: false,
  over_hard: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  cleanup()
  mockedListMembers.mockResolvedValue({
    tenant_id: TID,
    status_filter: "active",
    count: sampleMembers.length,
    members: sampleMembers,
  })
  mockedListInvites.mockResolvedValue({
    tenant_id: TID,
    status_filter: "pending",
    count: 1,
    invites: [sampleInvite],
  })
  mockedListAllProjects.mockResolvedValue([sampleProject, sampleArchived])
  mockedGetUsage.mockResolvedValue(sampleUsage)
})

describe("/tenants/{tid}/settings — access gate", () => {
  it("renders the 403 placeholder for a non-admin viewer", async () => {
    mockedUseAuth.mockReturnValue({
      user: {
        id: "u-1",
        email: "v@x.io",
        name: "V",
        role: "viewer",
        enabled: true,
        tenant_id: TID,
      },
      authMode: "session",
      loading: false,
    })
    await renderPage()
    expect(await screen.findByTestId("tenant-settings-forbidden")).toBeInTheDocument()
    expect(mockedListMembers).not.toHaveBeenCalled()
  })

  it("rejects an invalid tid path param without calling the API", async () => {
    setAdmin()
    await renderPage("not-a-tid")
    expect(await screen.findByTestId("tenant-settings-bad-id")).toBeInTheDocument()
    expect(mockedListMembers).not.toHaveBeenCalled()
  })

  it("treats authMode=open (dev anon admin) as admin", async () => {
    mockedUseAuth.mockReturnValue({
      user: null,
      authMode: "open",
      loading: false,
    })
    await renderPage()
    await waitFor(() => expect(mockedListMembers).toHaveBeenCalled())
    expect(screen.getByTestId("tenant-settings-page")).toBeInTheDocument()
  })

  it("shows a verifying placeholder while auth loads", async () => {
    mockedUseAuth.mockReturnValue({
      user: null,
      authMode: null,
      loading: true,
    })
    await renderPage()
    expect(screen.queryByTestId("tenant-settings-page")).not.toBeInTheDocument()
    expect(mockedListMembers).not.toHaveBeenCalled()
  })
})

describe("/tenants/{tid}/settings — Members tab", () => {
  beforeEach(setAdmin)

  it("renders rows and lets the admin change a role via PATCH", async () => {
    mockedPatchMember.mockResolvedValue({
      ...sampleMembers[1],
      role: "admin",
      tenant_id: TID,
      no_change: false,
    })
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("member-row-u-alice")).toBeInTheDocument(),
    )
    expect(screen.getByText("alice@x.io")).toBeInTheDocument()

    const sel = screen.getByTestId("member-role-u-bob") as HTMLSelectElement
    fireEvent.change(sel, { target: { value: "admin" } })

    await waitFor(() => expect(mockedPatchMember).toHaveBeenCalledTimes(1))
    expect(mockedPatchMember).toHaveBeenCalledWith(TID, "u-bob", { role: "admin" })
  })

  it("clicking Remove calls deleteTenantMember and refetches", async () => {
    mockedDeleteMember.mockResolvedValue({
      ...sampleMembers[1],
      status: "suspended",
      tenant_id: TID,
    })
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("member-remove-u-bob")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId("member-remove-u-bob"))
    await waitFor(() => expect(mockedDeleteMember).toHaveBeenCalledTimes(1))
    expect(mockedDeleteMember).toHaveBeenCalledWith(TID, "u-bob")
    await waitFor(() => expect(mockedListMembers).toHaveBeenCalledTimes(2))
  })

  it("server 409 (last-admin floor) is surfaced inline on the row", async () => {
    mockedPatchMember.mockRejectedValue(
      new ApiError({
        kind: "conflict",
        status: 409,
        body: '{"detail":"refuses to demote last admin"}',
        parsed: { detail: "refuses to demote last admin" },
        traceId: null,
        path: `/tenants/${TID}/members/u-alice`,
        method: "PATCH",
      }),
    )
    await renderPage()
    await waitFor(() =>
      expect(screen.getByTestId("member-row-u-alice")).toBeInTheDocument(),
    )
    fireEvent.change(screen.getByTestId("member-role-u-alice"), {
      target: { value: "viewer" },
    })
    await waitFor(() =>
      expect(screen.getByTestId("member-row-error-u-alice")).toBeInTheDocument(),
    )
    expect(screen.getByTestId("member-row-error-u-alice")).toHaveTextContent(
      "last admin",
    )
  })
})

describe("/tenants/{tid}/settings — Invites tab", () => {
  beforeEach(setAdmin)

  it("create dialog → createTenantInvite + token shown once + list refresh", async () => {
    mockedCreateInvite.mockResolvedValue({
      invite_id: "inv-new",
      token_plaintext: "OPAQUE-TOKEN-VALUE",
      expires_at: "2026-05-03 12:00:00",
    })

    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-invites"))
    await waitFor(() => expect(mockedListInvites).toHaveBeenCalled())

    fireEvent.click(screen.getByTestId("invites-create-btn"))
    fireEvent.change(screen.getByTestId("invites-create-email"), {
      target: { value: "new@x.io" },
    })
    fireEvent.click(screen.getByTestId("invites-create-submit"))

    await waitFor(() => expect(mockedCreateInvite).toHaveBeenCalledTimes(1))
    expect(mockedCreateInvite).toHaveBeenCalledWith(TID, {
      email: "new@x.io",
      role: "member",
    })

    expect(await screen.findByTestId("invites-last-issued")).toBeInTheDocument()
    expect(screen.getByTestId("invites-last-token")).toHaveTextContent(
      "OPAQUE-TOKEN-VALUE",
    )
    await waitFor(() => expect(mockedListInvites).toHaveBeenCalledTimes(2))
  })

  it("rejects an invalid email locally without calling the API", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-invites"))
    await waitFor(() => expect(mockedListInvites).toHaveBeenCalled())

    fireEvent.click(screen.getByTestId("invites-create-btn"))
    fireEvent.change(screen.getByTestId("invites-create-email"), {
      target: { value: "not-an-email" },
    })
    fireEvent.click(screen.getByTestId("invites-create-submit"))

    expect(await screen.findByTestId("invites-create-error")).toBeInTheDocument()
    expect(mockedCreateInvite).not.toHaveBeenCalled()
  })

  it("Revoke button → revokeTenantInvite + refetch", async () => {
    mockedRevokeInvite.mockResolvedValue({
      ...sampleInvite,
      status: "revoked",
      tenant_id: TID,
    })
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-invites"))
    await waitFor(() =>
      expect(screen.getByTestId("invite-row-inv-001")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("invite-revoke-inv-001"))
    await waitFor(() => expect(mockedRevokeInvite).toHaveBeenCalledTimes(1))
    expect(mockedRevokeInvite).toHaveBeenCalledWith(TID, "inv-001")
    await waitFor(() => expect(mockedListInvites).toHaveBeenCalledTimes(2))
  })
})

describe("/tenants/{tid}/settings — Projects tab", () => {
  beforeEach(setAdmin)

  it("renders live + archived rows and offers Archive on live", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-projects"))
    await waitFor(() =>
      expect(screen.getByTestId("project-row-p-fw")).toBeInTheDocument(),
    )
    expect(screen.getByTestId("project-row-p-old")).toBeInTheDocument()
    expect(screen.getByTestId("project-archive-p-fw")).toBeInTheDocument()
    expect(screen.getByTestId("project-restore-p-old")).toBeInTheDocument()
  })

  it("rejects a slug failing the local regex without calling the API", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-projects"))
    await waitFor(() =>
      expect(screen.getByTestId("projects-create-btn")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("projects-create-btn"))
    fireEvent.change(screen.getByTestId("projects-create-name"), {
      target: { value: "Bad" },
    })
    fireEvent.change(screen.getByTestId("projects-create-slug"), {
      target: { value: "Bad Slug!" },
    })
    fireEvent.click(screen.getByTestId("projects-create-submit"))

    expect(await screen.findByTestId("projects-create-error")).toBeInTheDocument()
    expect(mockedCreateProject).not.toHaveBeenCalled()
  })

  it("Archive button calls archiveTenantProject and refetches", async () => {
    mockedArchiveProject.mockResolvedValue({ ...sampleProject, archived_at: "2026-04-26 09:00:00" })
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-projects"))
    await waitFor(() =>
      expect(screen.getByTestId("project-archive-p-fw")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId("project-archive-p-fw"))
    await waitFor(() => expect(mockedArchiveProject).toHaveBeenCalledTimes(1))
    expect(mockedArchiveProject).toHaveBeenCalledWith(TID, "p-fw")
    await waitFor(() => expect(mockedListAllProjects).toHaveBeenCalledTimes(2))
  })

  it("Restore on an archived row calls restoreTenantProject", async () => {
    mockedRestoreProject.mockResolvedValue({ ...sampleArchived, archived_at: null })
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-projects"))
    await waitFor(() =>
      expect(screen.getByTestId("project-restore-p-old")).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId("project-restore-p-old"))
    await waitFor(() => expect(mockedRestoreProject).toHaveBeenCalledTimes(1))
    expect(mockedRestoreProject).toHaveBeenCalledWith(TID, "p-old")
  })

  it("on successful create, refetches projects and closes the dialog", async () => {
    mockedCreateProject.mockResolvedValue({
      ...sampleProject,
      project_id: "p-newco",
      name: "Newco",
      slug: "newco",
    })
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-projects"))
    await waitFor(() =>
      expect(screen.getByTestId("projects-create-btn")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("projects-create-btn"))
    fireEvent.change(screen.getByTestId("projects-create-name"), {
      target: { value: "Newco" },
    })
    fireEvent.change(screen.getByTestId("projects-create-slug"), {
      target: { value: "newco" },
    })
    fireEvent.click(screen.getByTestId("projects-create-submit"))

    await waitFor(() => expect(mockedCreateProject).toHaveBeenCalledTimes(1))
    expect(mockedCreateProject).toHaveBeenCalledWith(TID, {
      name: "Newco",
      slug: "newco",
      product_line: "embedded",
    })
    await waitFor(() =>
      expect(
        screen.queryByTestId("projects-create-dialog"),
      ).not.toBeInTheDocument(),
    )
    await waitFor(() => expect(mockedListAllProjects).toHaveBeenCalledTimes(2))
  })
})

describe("/tenants/{tid}/settings — Quotas tab", () => {
  beforeEach(setAdmin)

  it("renders plan + soft/hard breakdown from /storage/usage", async () => {
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-quotas"))
    await waitFor(() => expect(mockedGetUsage).toHaveBeenCalledWith(TID))

    expect(await screen.findByTestId("quotas-detail")).toBeInTheDocument()
    expect(screen.getByTestId("quotas-plan")).toHaveTextContent("pro")
    expect(screen.getByTestId("quotas-breakdown")).toBeInTheDocument()
  })

  it("surfaces an over-quota warning when over_hard is true", async () => {
    mockedGetUsage.mockResolvedValue({
      ...sampleUsage,
      over_hard: true,
      usage: { ...sampleUsage.usage, total_bytes: 250 * 1024 ** 3 },
    })
    await renderPage()
    fireEvent.click(await screen.findByTestId("settings-tab-quotas"))
    expect(await screen.findByTestId("quotas-overage")).toHaveTextContent("hard")
  })
})

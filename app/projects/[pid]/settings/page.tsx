"use client"

/**
 * Y8 row 5 — project-owner /projects/{pid}/settings page.
 *
 * Backend contracts: tenant_projects router — Y4 row 5 (POST/PATCH/
 * DELETE project_members), Y4 row 6 (POST project_shares), Y8 row 5
 * (GET project_members, GET project_shares, DELETE project_share).
 * Frontend gates the page to project owners + tenant admins as a
 * cosmetic short-circuit; the server enforces with
 * ``_user_can_manage_project_members`` (super_admin / tenant
 * admin/owner / explicit project_members.role='owner').
 *
 *   • Members → list + invite / change role / remove
 *   • Budget  → read tenant project + PATCH plan/disk/llm budget
 *               (tri-state body — null = inherit from tenant)
 *   • Shares  → list + grant cross-tenant + revoke
 *
 * Tenant resolution
 * ─────────────────
 * The URL path is ``/projects/{pid}/settings`` only — there's no
 * tid in the path. We resolve tenant_id by looking up ``pid`` in
 * the dashboard's project context (``useProject().projects[]``,
 * which lives under the active tenant). This means: the operator
 * must already be on the right tenant; if the project belongs to
 * a different tenant, the page shows a "switch tenant first"
 * placeholder (operator clicks the dashboard TenantSwitcher to
 * navigate). This matches the same trust posture as
 * ``/tenants/{tid}/settings`` — the operator must know which scope
 * they are managing.
 *
 * Module-global state audit
 * ─────────────────────────
 * None introduced. Per-tab React contexts (``useAuth``,
 * ``useTenant``, ``useProject``) + per-component ``useState``.
 * The page reads tenant_id from project context but never mutates
 * either ``_currentTenantId`` or ``_currentProjectId`` —
 * navigating to settings should not flip the dashboard scope.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * All tab actions are sequential ``await`` (one inflight per tab,
 * disabled UI while busy) followed by a tab-scoped refetch. The
 * Budget tab's PATCH-then-refetch reads the same row that just
 * committed via ``RETURNING`` so there's no stale read.
 */

import { use, useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  ChevronRight,
  CircleAlert,
  Coins,
  Folder,
  Loader2,
  Plus,
  RefreshCw,
  Share2,
  ShieldAlert,
  Trash2,
  Users,
  X,
} from "lucide-react"
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
  type ProjectMemberRole,
  type ProjectMemberRow,
  type ProjectShareRole,
  type ProjectShareRow,
  type TenantPlan,
  type TenantProjectInfo,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { useProject } from "@/lib/project-context"

const PROJECT_MEMBER_ROLES: ProjectMemberRole[] = ["owner", "contributor", "viewer"]
const PROJECT_SHARE_ROLES: ProjectShareRole[] = ["viewer", "contributor"]
const PROJECT_PLANS: TenantPlan[] = ["free", "starter", "pro", "enterprise"]
const PROJECT_ID_PATTERN = /^p-[a-z0-9][a-z0-9-]{2,63}$/
const TENANT_ID_PATTERN = /^t-[a-z0-9][a-z0-9-]{2,62}$/
const USER_ID_PATTERN = /^u-[a-z0-9]{4,64}$/
const EXPIRES_AT_PATTERN = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/

type TabId = "members" | "budget" | "shares"

const TABS: { id: TabId; label: string; icon: typeof Users }[] = [
  { id: "members", label: "Members", icon: Users },
  { id: "budget", label: "Budget", icon: Coins },
  { id: "shares", label: "Shares", icon: Share2 },
]

function formatBytes(n: number | null | undefined): string {
  if (n == null) return "—"
  if (n === 0) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

function formatNumber(n: number | null | undefined): string {
  if (n == null) return "—"
  return n.toLocaleString("en-US")
}

function describeError(exc: unknown): string {
  if (exc instanceof ApiError) {
    const detail = (exc.parsed as { detail?: string } | null)?.detail
    return detail ? `${exc.status}: ${detail}` : `${exc.status}: ${exc.body}`
  }
  if (exc instanceof Error) return exc.message
  return String(exc)
}

function parseBytesInput(raw: string): number | null | "invalid" {
  const trimmed = raw.trim()
  if (trimmed === "") return null
  const n = Number(trimmed)
  if (!Number.isFinite(n) || n < 0 || !Number.isInteger(n)) return "invalid"
  return n
}

export default function ProjectSettingsPage({
  params,
}: {
  params: Promise<{ pid: string }>
}) {
  const { pid } = use(params)
  const { user, authMode, loading: authLoading } = useAuth()
  const { currentTenantId } = useTenant()
  const { projects, loading: projectsLoading } = useProject()
  const [tab, setTab] = useState<TabId>("members")

  const projectIdValid = useMemo(() => PROJECT_ID_PATTERN.test(pid), [pid])

  // Resolve the project from the active tenant's project list. If the
  // operator clicks into the page while on the wrong tenant, this
  // returns null — we surface a "switch tenant first" placeholder.
  const project = useMemo(
    () => projects.find((p) => p.project_id === pid) ?? null,
    [projects, pid],
  )

  const isAllowed = useMemo(() => {
    if (authMode === "open") return true
    if (!user) return false
    if (user.role === "super_admin") return true
    // Frontend cosmetic gate. The server enforces project owner /
    // tenant admin via ``_user_can_manage_project_members`` —
    // forwarding ``admin`` here covers the tenant-admin path; project-
    // owner-only callers (whose user.role is ``operator`` /
    // ``viewer``) will get 403 panels from the API. Surfacing those
    // inline is preferable to gating them out of the page entirely
    // because we cannot know whether they own this project without
    // querying the backend.
    return user.role === "admin" || user.role === "operator"
  }, [user, authMode])

  if (authLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying session…
        </div>
      </main>
    )
  }

  if (!projectIdValid) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="project-settings-bad-id"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <CircleAlert size={16} />
            <span className="text-sm font-semibold">Invalid project id</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            <code className="px-1 rounded bg-[var(--secondary)]/40">{pid}</code>{" "}
            does not match{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">
              ^p-[a-z0-9][a-z0-9-]&#123;2,63&#125;$
            </code>
            .
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs underline text-[var(--neural-blue)]"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  if (!isAllowed) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="project-settings-forbidden"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <ShieldAlert size={16} />
            <span className="text-sm font-semibold">403 — project owner required</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            This page is gated to the{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">project owner</code>
            {" "}/{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">tenant admin</code>
            {" "}/{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">super_admin</code>
            {" "}roles. Contact the project owner to request access.
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs underline text-[var(--neural-blue)]"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  // Project context still loading — wait. Handles the legitimate
  // "fresh tab on /projects/p-xxx/settings" race: TenantProvider must
  // load currentTenantId, then ProjectProvider fetches the projects[]
  // list, then we can find ``pid``.
  if (projectsLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2" data-testid="project-settings-loading">
          <Loader2 size={14} className="animate-spin" />
          Loading project…
        </div>
      </main>
    )
  }

  if (!currentTenantId) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="project-settings-no-tenant"
      >
        <div className="max-w-md w-full rounded border border-[var(--border)] bg-[var(--card)] p-6 font-mono">
          <div className="text-sm font-semibold mb-2">Select a tenant first</div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            Project settings are scoped to a tenant. Use the tenant
            dropdown in the dashboard header to pick a tenant, then
            navigate back here.
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs underline text-[var(--neural-blue)]"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  if (!project) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="project-settings-not-found"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <CircleAlert size={16} />
            <span className="text-sm font-semibold">Project not found in current tenant</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            <code className="px-1 rounded bg-[var(--secondary)]/40">{pid}</code>{" "}
            is not a project on tenant{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">{currentTenantId}</code>.
            Switch to the project&apos;s tenant via the dashboard
            header dropdown and try again.
          </p>
          <Link
            href="/"
            className="inline-flex items-center gap-1 text-xs underline text-[var(--neural-blue)]"
          >
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="project-settings-page"
    >
      <div className="max-w-5xl mx-auto">
        <header className="mb-6">
          <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
            <Link href="/" className="hover:text-[var(--foreground)] inline-flex items-center gap-1">
              <ArrowLeft size={10} /> dashboard
            </Link>
            <ChevronRight size={10} />
            <span>tenants</span>
            <ChevronRight size={10} />
            <span>{project.tenant_id}</span>
            <ChevronRight size={10} />
            <span>projects</span>
            <ChevronRight size={10} />
            <span className="text-[var(--foreground)]">{project.slug}</span>
            <ChevronRight size={10} />
            <span className="text-[var(--foreground)]">settings</span>
          </div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <Folder size={20} />
            Project settings · <span className="font-mono text-base">{project.name}</span>
          </h1>
          <p className="text-xs text-[var(--muted-foreground)] mt-1">
            Manage project members, budget, and cross-tenant shares.
            All operations are recorded on the I8 audit chain.
          </p>
        </header>

        <nav
          className="flex items-center gap-1 mb-4 border-b border-[var(--border)]"
          aria-label="Project settings tabs"
          role="tablist"
        >
          {TABS.map((t) => {
            const Icon = t.icon
            const active = tab === t.id
            return (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => setTab(t.id)}
                className={`px-3 py-2 inline-flex items-center gap-1.5 text-xs font-mono border-b-2 transition-colors -mb-px ${
                  active
                    ? "border-[var(--neural-blue)] text-[var(--foreground)]"
                    : "border-transparent text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                }`}
                data-testid={`project-settings-tab-${t.id}`}
              >
                <Icon size={12} />
                {t.label}
              </button>
            )
          })}
        </nav>

        <section
          role="tabpanel"
          aria-labelledby={`project-settings-tab-${tab}`}
          data-testid={`project-settings-panel-${tab}`}
        >
          {tab === "members" && (
            <MembersTab tid={project.tenant_id} pid={pid} />
          )}
          {tab === "budget" && <BudgetTab project={project} />}
          {tab === "shares" && (
            <SharesTab tid={project.tenant_id} pid={pid} />
          )}
        </section>
      </div>
    </main>
  )
}

// ─── Members tab ────────────────────────────────────────────────

function MembersTab({ tid, pid }: { tid: string; pid: string }) {
  const [rows, setRows] = useState<ProjectMemberRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createUserId, setCreateUserId] = useState("")
  const [createRole, setCreateRole] = useState<ProjectMemberRole>("contributor")
  const [createSubmitting, setCreateSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listProjectMembers(tid, pid)
      setRows(res.members)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setLoading(false)
    }
  }, [tid, pid])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const onPatch = useCallback(
    async (uid: string, role: ProjectMemberRole) => {
      setBusyId(uid)
      setRowError(null)
      try {
        const updated = await patchProjectMember(tid, pid, uid, { role })
        setRows((cur) =>
          cur.map((r) =>
            r.user_id === uid ? { ...r, role: updated.role } : r,
          ),
        )
      } catch (exc) {
        setRowError({ id: uid, message: describeError(exc) })
      } finally {
        setBusyId(null)
      }
    },
    [tid, pid],
  )

  const onRemove = useCallback(
    async (uid: string) => {
      setBusyId(uid)
      setRowError(null)
      try {
        await deleteProjectMember(tid, pid, uid)
        await refresh()
      } catch (exc) {
        setRowError({ id: uid, message: describeError(exc) })
      } finally {
        setBusyId(null)
      }
    },
    [tid, pid, refresh],
  )

  const onCreate = useCallback(async () => {
    setCreateSubmitting(true)
    setCreateError(null)
    const uid = createUserId.trim()
    if (!USER_ID_PATTERN.test(uid)) {
      setCreateSubmitting(false)
      setCreateError("Invalid user id — must match ^u-[a-z0-9]{4,64}$.")
      return
    }
    try {
      await createProjectMember(tid, pid, { user_id: uid, role: createRole })
      setCreateUserId("")
      setCreateRole("contributor")
      setCreateOpen(false)
      await refresh()
    } catch (exc) {
      setCreateError(describeError(exc))
    } finally {
      setCreateSubmitting(false)
    }
  }, [tid, pid, createUserId, createRole, refresh])

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold inline-flex items-center gap-2">
          <Users size={14} />
          Project members
        </h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="project-members-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => {
              setCreateUserId("")
              setCreateRole("contributor")
              setCreateError(null)
              setCreateOpen(true)
            }}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs font-mono"
            data-testid="project-members-create-btn"
          >
            <Plus size={12} />
            Add member
          </button>
        </div>
      </div>

      {error && (
        <div
          className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-xs font-mono text-[var(--destructive)]"
          data-testid="project-members-error"
        >
          {error}
        </div>
      )}

      <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-x-auto">
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
              <th className="text-left px-3 py-2">email</th>
              <th className="text-left px-3 py-2">name</th>
              <th className="text-left px-3 py-2">role</th>
              <th className="text-left px-3 py-2">added</th>
              <th className="text-right px-3 py-2">actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="project-members-loading">
                  <Loader2 size={14} className="animate-spin inline-block mr-2" />
                  Loading members…
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && !error && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="project-members-empty">
                  No explicit project members. Tenant admins / owners
                  retain access via the tenant default.
                </td>
              </tr>
            )}
            {!loading && rows.map((r) => {
              const busy = busyId === r.user_id
              const isError = rowError?.id === r.user_id
              return (
                <tr
                  key={r.user_id}
                  className="border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--secondary)]/20"
                  data-testid={`project-member-row-${r.user_id}`}
                >
                  <td className="px-3 py-2">{r.email}</td>
                  <td className="px-3 py-2">{r.name}</td>
                  <td className="px-3 py-2">
                    <select
                      value={r.role}
                      disabled={busy}
                      onChange={(e) =>
                        void onPatch(r.user_id, e.target.value as ProjectMemberRole)
                      }
                      aria-label={`Change role for ${r.email}`}
                      className="px-1 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] text-xs font-mono"
                      data-testid={`project-member-role-${r.user_id}`}
                    >
                      {PROJECT_MEMBER_ROLES.map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-3 py-2 text-[var(--muted-foreground)]">{r.created_at}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void onRemove(r.user_id)}
                      aria-label={`Remove ${r.email}`}
                      className="px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--destructive)]/20 text-[10px] disabled:opacity-50"
                      data-testid={`project-member-remove-${r.user_id}`}
                    >
                      {busy ? <Loader2 size={10} className="animate-spin inline" /> : "Remove"}
                    </button>
                    {isError && (
                      <div
                        className="text-[10px] text-[var(--destructive)] mt-1"
                        data-testid={`project-member-row-error-${r.user_id}`}
                      >
                        {rowError?.message}
                      </div>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[10px] text-[var(--muted-foreground)] font-mono">
        Removing a project member drops them to the tenant default role
        (admin/owner → contributor on every project; member/viewer → no
        project access).
      </p>

      {createOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Add project member"
          data-testid="project-members-create-dialog"
        >
          <div className="w-full max-w-md rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold inline-flex items-center gap-2">
                <Plus size={14} /> Add project member
              </h3>
              <button
                type="button"
                aria-label="Close add member dialog"
                onClick={() => setCreateOpen(false)}
                className="p-1 rounded hover:bg-[var(--secondary)]/40"
              >
                <X size={14} />
              </button>
            </div>
            <div className="space-y-3 text-xs">
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">User id</span>
                <input
                  type="text"
                  value={createUserId}
                  onChange={(e) => setCreateUserId(e.target.value)}
                  placeholder="u-abcd1234"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="project-members-create-user-id"
                />
                <span className="block text-[10px] text-[var(--muted-foreground)] mt-1">
                  User must already be an active tenant member. Pattern:
                  ^u-[a-z0-9]{"{"}4,64{"}"}$
                </span>
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Role</span>
                <select
                  value={createRole}
                  onChange={(e) => setCreateRole(e.target.value as ProjectMemberRole)}
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="project-members-create-role"
                >
                  {PROJECT_MEMBER_ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              {createError && (
                <div
                  className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-[11px] text-[var(--destructive)]"
                  data-testid="project-members-create-error"
                >
                  {createError}
                </div>
              )}
            </div>
            <div className="flex items-center justify-end gap-2 mt-5">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                disabled={createSubmitting}
                className="px-3 py-1.5 rounded border border-[var(--border)] text-xs disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void onCreate()}
                disabled={createSubmitting}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs disabled:opacity-50"
                data-testid="project-members-create-submit"
              >
                {createSubmitting && <Loader2 size={12} className="animate-spin" />}
                Add
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Budget tab ─────────────────────────────────────────────────

function BudgetTab({ project }: { project: TenantProjectInfo }) {
  // Local form state mirrors the project's current budget fields,
  // reset every time the upstream project record changes (e.g. after
  // a successful PATCH). Tri-state: empty input = "" → null = clear
  // (inherit from tenant); non-empty = explicit override.
  const [planOverride, setPlanOverride] = useState<TenantPlan | "">(
    (project.plan_override as TenantPlan | null) ?? "",
  )
  const [diskBudget, setDiskBudget] = useState<string>(
    project.disk_budget_bytes != null ? String(project.disk_budget_bytes) : "",
  )
  const [llmBudget, setLlmBudget] = useState<string>(
    project.llm_budget_tokens != null ? String(project.llm_budget_tokens) : "",
  )
  const [submitting, setSubmitting] = useState(false)
  const [success, setSuccess] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [current, setCurrent] = useState<TenantProjectInfo>(project)

  // When the parent project prop changes (e.g. tenant switch), re-seed.
  useEffect(() => {
    setCurrent(project)
    setPlanOverride((project.plan_override as TenantPlan | null) ?? "")
    setDiskBudget(project.disk_budget_bytes != null ? String(project.disk_budget_bytes) : "")
    setLlmBudget(project.llm_budget_tokens != null ? String(project.llm_budget_tokens) : "")
    setSuccess(null)
    setError(null)
  }, [project])

  const onSubmit = useCallback(async () => {
    setSubmitting(true)
    setSuccess(null)
    setError(null)

    const disk = parseBytesInput(diskBudget)
    if (disk === "invalid") {
      setSubmitting(false)
      setError("Disk budget must be a non-negative integer (bytes) or empty.")
      return
    }
    const llm = parseBytesInput(llmBudget)
    if (llm === "invalid") {
      setSubmitting(false)
      setError("LLM budget must be a non-negative integer (tokens) or empty.")
      return
    }

    try {
      const updated = await patchProjectBudget(
        current.tenant_id,
        current.project_id,
        {
          plan_override: planOverride === "" ? null : (planOverride as TenantPlan),
          disk_budget_bytes: disk,
          llm_budget_tokens: llm,
        },
      )
      setCurrent(updated)
      setSuccess(updated.no_change ? "No changes — already at requested state." : "Budget updated.")
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setSubmitting(false)
    }
  }, [current, planOverride, diskBudget, llmBudget])

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold inline-flex items-center gap-2">
          <Coins size={14} />
          Budget &amp; quota override
        </h2>
      </div>

      <div className="rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono text-xs space-y-4" data-testid="project-budget-form">
        <div>
          <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wide mb-2">
            Current
          </div>
          <ul className="space-y-1">
            <li className="flex items-center justify-between" data-testid="project-budget-current-plan">
              <span>plan_override</span>
              <span className="text-[var(--muted-foreground)]">
                {current.plan_override ?? "(inherit from tenant)"}
              </span>
            </li>
            <li className="flex items-center justify-between" data-testid="project-budget-current-disk">
              <span>disk_budget_bytes</span>
              <span className="text-[var(--muted-foreground)]">
                {current.disk_budget_bytes != null
                  ? `${formatBytes(current.disk_budget_bytes)} (${formatNumber(current.disk_budget_bytes)} bytes)`
                  : "(inherit from tenant)"}
              </span>
            </li>
            <li className="flex items-center justify-between" data-testid="project-budget-current-llm">
              <span>llm_budget_tokens</span>
              <span className="text-[var(--muted-foreground)]">
                {current.llm_budget_tokens != null
                  ? `${formatNumber(current.llm_budget_tokens)} tokens`
                  : "(inherit from tenant)"}
              </span>
            </li>
          </ul>
        </div>

        <div className="border-t border-[var(--border)] pt-4 space-y-3">
          <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wide">
            Override
          </div>

          <label className="block">
            <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
              plan_override (empty = inherit from tenant)
            </span>
            <select
              value={planOverride}
              onChange={(e) => setPlanOverride(e.target.value as TenantPlan | "")}
              className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
              data-testid="project-budget-plan"
              disabled={submitting}
            >
              <option value="">(inherit from tenant)</option>
              {PROJECT_PLANS.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
              disk_budget_bytes (empty = inherit; non-negative integer)
            </span>
            <input
              type="text"
              inputMode="numeric"
              value={diskBudget}
              onChange={(e) => setDiskBudget(e.target.value)}
              placeholder="e.g. 1073741824 for 1 GiB"
              className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
              data-testid="project-budget-disk"
              disabled={submitting}
            />
          </label>

          <label className="block">
            <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
              llm_budget_tokens (empty = inherit; non-negative integer)
            </span>
            <input
              type="text"
              inputMode="numeric"
              value={llmBudget}
              onChange={(e) => setLlmBudget(e.target.value)}
              placeholder="e.g. 1000000 for 1M tokens"
              className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
              data-testid="project-budget-llm"
              disabled={submitting}
            />
          </label>

          {success && (
            <div
              className="rounded border border-[var(--neural-green)]/40 bg-[var(--neural-green)]/10 p-2 text-[11px] text-[var(--neural-green)]"
              data-testid="project-budget-success"
            >
              {success}
            </div>
          )}
          {error && (
            <div
              className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-[11px] text-[var(--destructive)]"
              data-testid="project-budget-error"
            >
              {error}
            </div>
          )}

          <div className="flex items-center justify-end">
            <button
              type="button"
              onClick={() => void onSubmit()}
              disabled={submitting}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs disabled:opacity-50"
              data-testid="project-budget-submit"
            >
              {submitting && <Loader2 size={12} className="animate-spin" />}
              Save budget
            </button>
          </div>
        </div>
      </div>
      <p className="mt-3 text-[10px] text-[var(--muted-foreground)] font-mono">
        Server enforces Σ(project budgets) ≤ tenant plan ceiling. A 409
        on save indicates the override would oversell the tenant
        quota — reduce on this project or another, then retry.
      </p>
    </div>
  )
}

// ─── Shares tab ─────────────────────────────────────────────────

function SharesTab({ tid, pid }: { tid: string; pid: string }) {
  const [rows, setRows] = useState<ProjectShareRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createGuest, setCreateGuest] = useState("")
  const [createRole, setCreateRole] = useState<ProjectShareRole>("viewer")
  const [createExpires, setCreateExpires] = useState("")
  const [createSubmitting, setCreateSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listProjectShares(tid, pid)
      setRows(res.shares)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setLoading(false)
    }
  }, [tid, pid])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const onRevoke = useCallback(
    async (sid: string) => {
      setBusyId(sid)
      try {
        await deleteProjectShare(tid, pid, sid)
        await refresh()
      } catch (exc) {
        setError(describeError(exc))
      } finally {
        setBusyId(null)
      }
    },
    [tid, pid, refresh],
  )

  const onCreate = useCallback(async () => {
    setCreateSubmitting(true)
    setCreateError(null)
    const guest = createGuest.trim()
    if (!TENANT_ID_PATTERN.test(guest)) {
      setCreateSubmitting(false)
      setCreateError("Invalid guest tenant id — must match ^t-[a-z0-9][a-z0-9-]{2,62}$.")
      return
    }
    if (guest === tid) {
      setCreateSubmitting(false)
      setCreateError("Cannot share a project to its owning tenant.")
      return
    }
    const expires = createExpires.trim()
    if (expires !== "" && !EXPIRES_AT_PATTERN.test(expires)) {
      setCreateSubmitting(false)
      setCreateError("Invalid expires_at — must be UTC YYYY-MM-DD HH:MM:SS or empty.")
      return
    }
    try {
      await createProjectShare(tid, pid, {
        guest_tenant_id: guest,
        role: createRole,
        expires_at: expires === "" ? null : expires,
      })
      setCreateGuest("")
      setCreateRole("viewer")
      setCreateExpires("")
      setCreateOpen(false)
      await refresh()
    } catch (exc) {
      setCreateError(describeError(exc))
    } finally {
      setCreateSubmitting(false)
    }
  }, [tid, pid, createGuest, createRole, createExpires, refresh])

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold inline-flex items-center gap-2">
          <Share2 size={14} />
          Cross-tenant shares
        </h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="project-shares-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => {
              setCreateGuest("")
              setCreateRole("viewer")
              setCreateExpires("")
              setCreateError(null)
              setCreateOpen(true)
            }}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs font-mono"
            data-testid="project-shares-create-btn"
          >
            <Plus size={12} />
            Grant share
          </button>
        </div>
      </div>

      {error && (
        <div
          className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-xs font-mono text-[var(--destructive)]"
          data-testid="project-shares-error"
        >
          {error}
        </div>
      )}

      <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-x-auto">
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
              <th className="text-left px-3 py-2">share_id</th>
              <th className="text-left px-3 py-2">guest tenant</th>
              <th className="text-left px-3 py-2">role</th>
              <th className="text-left px-3 py-2">created</th>
              <th className="text-left px-3 py-2">expires</th>
              <th className="text-right px-3 py-2">actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={6} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="project-shares-loading">
                  <Loader2 size={14} className="animate-spin inline-block mr-2" />
                  Loading shares…
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && !error && (
              <tr>
                <td colSpan={6} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="project-shares-empty">
                  No cross-tenant shares yet. Click <strong>Grant share</strong> to expose this project to another tenant.
                </td>
              </tr>
            )}
            {!loading && rows.map((s) => {
              const busy = busyId === s.share_id
              return (
                <tr
                  key={s.share_id}
                  className="border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--secondary)]/20"
                  data-testid={`project-share-row-${s.share_id}`}
                >
                  <td className="px-3 py-2 text-[var(--muted-foreground)]">{s.share_id}</td>
                  <td className="px-3 py-2">{s.guest_tenant_id}</td>
                  <td className="px-3 py-2">{s.role}</td>
                  <td className="px-3 py-2 text-[var(--muted-foreground)]">{s.created_at}</td>
                  <td className="px-3 py-2 text-[var(--muted-foreground)]">{s.expires_at ?? "permanent"}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void onRevoke(s.share_id)}
                      className="px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--destructive)]/20 text-[10px] inline-flex items-center gap-1 disabled:opacity-50"
                      data-testid={`project-share-revoke-${s.share_id}`}
                    >
                      {busy ? <Loader2 size={10} className="animate-spin" /> : <Trash2 size={10} />}
                      Revoke
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[10px] text-[var(--muted-foreground)] font-mono">
        Guest tenants see this project in the Guest tab of their own
        admin console. Role <code>owner</code> is intentionally not
        offered for cross-tenant shares — guest tenants cannot own a
        project belonging to another tenant.
      </p>

      {createOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Grant cross-tenant share"
          data-testid="project-shares-create-dialog"
        >
          <div className="w-full max-w-md rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold inline-flex items-center gap-2">
                <Plus size={14} /> Grant share
              </h3>
              <button
                type="button"
                aria-label="Close grant share dialog"
                onClick={() => setCreateOpen(false)}
                className="p-1 rounded hover:bg-[var(--secondary)]/40"
              >
                <X size={14} />
              </button>
            </div>
            <div className="space-y-3 text-xs">
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Guest tenant id</span>
                <input
                  type="text"
                  value={createGuest}
                  onChange={(e) => setCreateGuest(e.target.value)}
                  placeholder="t-other-tenant"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="project-shares-create-guest"
                />
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Role</span>
                <select
                  value={createRole}
                  onChange={(e) => setCreateRole(e.target.value as ProjectShareRole)}
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="project-shares-create-role"
                >
                  {PROJECT_SHARE_ROLES.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
                  Expires at (UTC, empty = permanent)
                </span>
                <input
                  type="text"
                  value={createExpires}
                  onChange={(e) => setCreateExpires(e.target.value)}
                  placeholder="2027-01-01 00:00:00"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="project-shares-create-expires"
                />
              </label>
              {createError && (
                <div
                  className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-[11px] text-[var(--destructive)]"
                  data-testid="project-shares-create-error"
                >
                  {createError}
                </div>
              )}
            </div>
            <div className="flex items-center justify-end gap-2 mt-5">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                disabled={createSubmitting}
                className="px-3 py-1.5 rounded border border-[var(--border)] text-xs disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void onCreate()}
                disabled={createSubmitting}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs disabled:opacity-50"
                data-testid="project-shares-create-submit"
              >
                {createSubmitting && <Loader2 size={12} className="animate-spin" />}
                Grant
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

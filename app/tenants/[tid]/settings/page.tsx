"use client"

/**
 * Y8 row 4 — tenant-admin /tenants/{tid}/settings page.
 *
 * Backend contracts already in place from Y3 + Y4 (tenant_invites,
 * tenant_members, tenant_projects routers) + storage.py /usage. This
 * page is the operator-facing UI on top of them, gated to tenant
 * admins (server still enforces; the frontend gate avoids 403 noise).
 *
 *   • Members  → GET / PATCH / DELETE /api/v1/tenants/{tid}/members
 *   • Invites  → GET / POST / DELETE /api/v1/tenants/{tid}/invites
 *   • Projects → GET / POST /api/v1/tenants/{tid}/projects
 *                + POST .../archive + .../restore
 *   • Quotas   → GET /api/v1/storage/usage?tenant_id={tid}
 *                (admin can pass a tenant_id != self per storage.py)
 *
 * Module-global state audit
 * ─────────────────────────
 * None introduced. The page reads ``useAuth()`` / ``useTenant()``
 * (per-tab React contexts), local ``useState``, and calls
 * ``lib/api.ts`` wrappers (which set X-Tenant-Id from the already-
 * audited ``_currentTenantId`` module-global). Path-param ``tid`` is
 * passed explicitly into every request, so visiting this page does
 * NOT mutate the dashboard's tenant context.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * All tab actions are sequential ``await`` (one inflight per tab,
 * disabled UI while busy) followed by a tab-scoped refetch / response
 * projection — no shared state to race.
 */

import { use, useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  Archive,
  Building2,
  ChevronRight,
  CheckCircle2,
  CircleAlert,
  Copy,
  Folder,
  KeyRound,
  Loader2,
  Mail,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  Users,
  X,
} from "lucide-react"
import {
  ApiError,
  archiveTenantProject,
  completeCmekWizard,
  createTenantInvite,
  createTenantProject,
  deleteTenantMember,
  generateCmekWizardPolicy,
  getStorageUsage,
  listAllTenantProjects,
  listCmekWizardProviders,
  listTenantInvites,
  listTenantMembers,
  patchTenantMember,
  restoreTenantProject,
  revokeTenantInvite,
  type CreatedTenantInvite,
  type CmekProvider,
  type CmekProviderSpec,
  type CompleteCmekResponse,
  type ProductLine,
  type TenantInviteRow,
  type TenantMemberRole,
  type TenantMemberRow,
  type TenantProjectInfo,
  type TenantStorageUsage,
  type VerifyCmekResponse,
  verifyCmekWizardConnection,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const ROLES: TenantMemberRole[] = ["owner", "admin", "member", "viewer"]
const PRODUCT_LINES: ProductLine[] = [
  "embedded",
  "web",
  "mobile",
  "software",
  "custom",
]
const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]*$/
const TENANT_ID_PATTERN = /^t-[a-z0-9][a-z0-9-]{2,62}$/
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

type TabId = "members" | "invites" | "projects" | "quotas" | "security"

const TABS: { id: TabId; label: string; icon: typeof Users }[] = [
  { id: "members", label: "Members", icon: Users },
  { id: "invites", label: "Invites", icon: Mail },
  { id: "projects", label: "Projects", icon: Folder },
  { id: "quotas", label: "Quotas", icon: Archive },
  { id: "security", label: "Security", icon: KeyRound },
]

function formatBytes(n: number | null | undefined): string {
  if (n == null || n === 0) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

function describeError(exc: unknown): string {
  if (exc instanceof ApiError) {
    const detail = (exc.parsed as { detail?: string } | null)?.detail
    return detail ? `${exc.status}: ${detail}` : `${exc.status}: ${exc.body}`
  }
  if (exc instanceof Error) return exc.message
  return String(exc)
}

export default function TenantSettingsPage({
  params,
}: {
  params: Promise<{ tid: string }>
}) {
  const { tid } = use(params)
  const { user, authMode, loading: authLoading } = useAuth()
  const [tab, setTab] = useState<TabId>("members")

  const tenantIdValid = useMemo(() => TENANT_ID_PATTERN.test(tid), [tid])

  const isAllowed = useMemo(() => {
    if (authMode === "open") return true
    if (!user) return false
    if (user.role === "super_admin") return true
    // Tenant admin tier — frontend cosmetic gate only. The server still
    // enforces (active membership with role ∈ {owner, admin}); a user
    // with role "admin" on a different tenant will get 403 panels.
    return user.role === "admin"
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

  if (!tenantIdValid) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="tenant-settings-bad-id"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <CircleAlert size={16} />
            <span className="text-sm font-semibold">Invalid tenant id</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            <code className="px-1 rounded bg-[var(--secondary)]/40">{tid}</code>{" "}
            does not match{" "}
            <code className="px-1 rounded bg-[var(--secondary)]/40">
              ^t-[a-z0-9][a-z0-9-]&#123;2,62&#125;$
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
        data-testid="tenant-settings-forbidden"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <ShieldAlert size={16} />
            <span className="text-sm font-semibold">403 — tenant admin required</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            This page is gated to the <code className="px-1 rounded bg-[var(--secondary)]/40">admin</code>
            {" "}/ {" "}<code className="px-1 rounded bg-[var(--secondary)]/40">super_admin</code>{" "}
            roles. Contact your tenant owner to request access.
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
      data-testid="tenant-settings-page"
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
            <span className="text-[var(--foreground)]">{tid}</span>
            <ChevronRight size={10} />
            <span className="text-[var(--foreground)]">settings</span>
          </div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <Building2 size={20} />
            Tenant settings · <span className="font-mono text-base">{tid}</span>
          </h1>
          <p className="text-xs text-[var(--muted-foreground)] mt-1">
            Manage members, invites, projects, and quotas for this tenant.
            Changes are audited via the I8 chain.
          </p>
        </header>

        <nav
          className="flex items-center gap-1 mb-4 border-b border-[var(--border)]"
          aria-label="Tenant settings tabs"
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
                data-testid={`settings-tab-${t.id}`}
              >
                <Icon size={12} />
                {t.label}
              </button>
            )
          })}
        </nav>

        <section
          role="tabpanel"
          aria-labelledby={`settings-tab-${tab}`}
          data-testid={`settings-panel-${tab}`}
        >
          {tab === "members" && <MembersTab tid={tid} />}
          {tab === "invites" && <InvitesTab tid={tid} />}
          {tab === "projects" && <ProjectsTab tid={tid} />}
          {tab === "quotas" && <QuotasTab tid={tid} />}
          {tab === "security" && <SecurityTab tid={tid} />}
        </section>
      </div>
    </main>
  )
}

// ─── Members tab ────────────────────────────────────────────────

function MembersTab({ tid }: { tid: string }) {
  const [rows, setRows] = useState<TenantMemberRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listTenantMembers(tid, { status: "active" })
      setRows(res.members)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setLoading(false)
    }
  }, [tid])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const onPatch = useCallback(
    async (uid: string, body: { role?: TenantMemberRole }) => {
      setBusyId(uid)
      setRowError(null)
      try {
        const updated = await patchTenantMember(tid, uid, body)
        setRows((cur) =>
          cur.map((r) =>
            r.user_id === uid
              ? { ...r, role: updated.role, status: updated.status }
              : r,
          ),
        )
      } catch (exc) {
        setRowError({ id: uid, message: describeError(exc) })
      } finally {
        setBusyId(null)
      }
    },
    [tid],
  )

  const onRemove = useCallback(
    async (uid: string) => {
      setBusyId(uid)
      setRowError(null)
      try {
        await deleteTenantMember(tid, uid)
        await refresh()
      } catch (exc) {
        setRowError({ id: uid, message: describeError(exc) })
      } finally {
        setBusyId(null)
      }
    },
    [tid, refresh],
  )

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold inline-flex items-center gap-2">
          <Users size={14} />
          Members
        </h2>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
          data-testid="members-refresh"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </div>

      {error && (
        <div
          className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-xs font-mono text-[var(--destructive)]"
          data-testid="members-error"
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
              <th className="text-left px-3 py-2">joined</th>
              <th className="text-right px-3 py-2">actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="members-loading">
                  <Loader2 size={14} className="animate-spin inline-block mr-2" />
                  Loading members…
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && !error && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="members-empty">
                  No active members. Invite users via the Invites tab.
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
                  data-testid={`member-row-${r.user_id}`}
                >
                  <td className="px-3 py-2">{r.email}</td>
                  <td className="px-3 py-2">{r.name}</td>
                  <td className="px-3 py-2">
                    <select
                      value={r.role}
                      disabled={busy}
                      onChange={(e) =>
                        void onPatch(r.user_id, { role: e.target.value as TenantMemberRole })
                      }
                      aria-label={`Change role for ${r.email}`}
                      className="px-1 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] text-xs font-mono"
                      data-testid={`member-role-${r.user_id}`}
                    >
                      {ROLES.map((role) => (
                        <option key={role} value={role}>
                          {role}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-3 py-2 text-[var(--muted-foreground)]">{r.joined_at}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void onRemove(r.user_id)}
                      aria-label={`Remove ${r.email}`}
                      className="px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--destructive)]/20 text-[10px] disabled:opacity-50"
                      data-testid={`member-remove-${r.user_id}`}
                    >
                      {busy ? <Loader2 size={10} className="animate-spin inline" /> : "Remove"}
                    </button>
                    {isError && (
                      <div
                        className="text-[10px] text-[var(--destructive)] mt-1"
                        data-testid={`member-row-error-${r.user_id}`}
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
        Demoting / removing the last active owner-or-admin is refused server-side (409); the row error reflects the gap.
      </p>
    </div>
  )
}

// ─── Invites tab ────────────────────────────────────────────────

function InvitesTab({ tid }: { tid: string }) {
  const [rows, setRows] = useState<TenantInviteRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createEmail, setCreateEmail] = useState("")
  const [createRole, setCreateRole] = useState<TenantMemberRole>("member")
  const [createSubmitting, setCreateSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [lastIssued, setLastIssued] = useState<CreatedTenantInvite | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listTenantInvites(tid, { status: "pending" })
      setRows(res.invites)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setLoading(false)
    }
  }, [tid])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const onCreate = useCallback(async () => {
    setCreateSubmitting(true)
    setCreateError(null)
    const email = createEmail.trim()
    if (!EMAIL_RE.test(email)) {
      setCreateSubmitting(false)
      setCreateError("Invalid email address.")
      return
    }
    try {
      const issued = await createTenantInvite(tid, { email, role: createRole })
      setLastIssued(issued)
      setCreateEmail("")
      setCreateRole("member")
      setCreateOpen(false)
      await refresh()
    } catch (exc) {
      setCreateError(describeError(exc))
    } finally {
      setCreateSubmitting(false)
    }
  }, [tid, createEmail, createRole, refresh])

  const onRevoke = useCallback(
    async (inviteId: string) => {
      setBusyId(inviteId)
      try {
        await revokeTenantInvite(tid, inviteId)
        await refresh()
      } catch (exc) {
        setError(describeError(exc))
      } finally {
        setBusyId(null)
      }
    },
    [tid, refresh],
  )

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold inline-flex items-center gap-2">
          <Mail size={14} />
          Pending invites
        </h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="invites-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => {
              setCreateEmail("")
              setCreateRole("member")
              setCreateError(null)
              setCreateOpen(true)
            }}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs font-mono"
            data-testid="invites-create-btn"
          >
            <Plus size={12} />
            New invite
          </button>
        </div>
      </div>

      {error && (
        <div
          className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-xs font-mono text-[var(--destructive)]"
          data-testid="invites-error"
        >
          {error}
        </div>
      )}

      {lastIssued && (
        <div
          className="mb-3 rounded border border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/10 p-3 text-xs font-mono"
          data-testid="invites-last-issued"
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="font-semibold mb-1">Invite link issued — copy now, it&apos;s shown once.</div>
              <div className="text-[10px] text-[var(--muted-foreground)]">
                invite_id: {lastIssued.invite_id} · expires {lastIssued.expires_at}
              </div>
              <code
                className="block mt-2 px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[10px] break-all"
                data-testid="invites-last-token"
              >
                {lastIssued.token_plaintext}
              </code>
            </div>
            <div className="flex flex-col items-end gap-1">
              <button
                type="button"
                onClick={() => {
                  void navigator.clipboard?.writeText(lastIssued.token_plaintext)
                }}
                className="inline-flex items-center gap-1 px-2 py-1 rounded border border-[var(--border)] text-[10px]"
                data-testid="invites-last-copy"
              >
                <Copy size={10} /> Copy token
              </button>
              <button
                type="button"
                onClick={() => setLastIssued(null)}
                aria-label="Dismiss issued invite banner"
                className="p-1 rounded hover:bg-[var(--secondary)]/40"
              >
                <X size={12} />
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-x-auto">
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
              <th className="text-left px-3 py-2">email</th>
              <th className="text-left px-3 py-2">role</th>
              <th className="text-left px-3 py-2">created</th>
              <th className="text-left px-3 py-2">expires</th>
              <th className="text-right px-3 py-2">actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="invites-loading">
                  <Loader2 size={14} className="animate-spin inline-block mr-2" />
                  Loading invites…
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && !error && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="invites-empty">
                  No pending invites.
                </td>
              </tr>
            )}
            {!loading && rows.map((r) => {
              const busy = busyId === r.invite_id
              return (
                <tr
                  key={r.invite_id}
                  className="border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--secondary)]/20"
                  data-testid={`invite-row-${r.invite_id}`}
                >
                  <td className="px-3 py-2">{r.email}</td>
                  <td className="px-3 py-2">{r.role}</td>
                  <td className="px-3 py-2 text-[var(--muted-foreground)]">{r.created_at}</td>
                  <td className="px-3 py-2 text-[var(--muted-foreground)]">{r.expires_at}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void onRevoke(r.invite_id)}
                      className="px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--destructive)]/20 text-[10px] disabled:opacity-50"
                      data-testid={`invite-revoke-${r.invite_id}`}
                    >
                      {busy ? <Loader2 size={10} className="animate-spin inline" /> : "Revoke"}
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Create invite"
          data-testid="invites-create-dialog"
        >
          <div className="w-full max-w-md rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold inline-flex items-center gap-2">
                <Plus size={14} /> New invite
              </h3>
              <button
                type="button"
                aria-label="Close create dialog"
                onClick={() => setCreateOpen(false)}
                className="p-1 rounded hover:bg-[var(--secondary)]/40"
              >
                <X size={14} />
              </button>
            </div>
            <div className="space-y-3 text-xs">
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Email</span>
                <input
                  type="email"
                  value={createEmail}
                  onChange={(e) => setCreateEmail(e.target.value)}
                  placeholder="alice@example.com"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="invites-create-email"
                />
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Role on accept</span>
                <select
                  value={createRole}
                  onChange={(e) => setCreateRole(e.target.value as TenantMemberRole)}
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="invites-create-role"
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </label>
              {createError && (
                <div
                  className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-[11px] text-[var(--destructive)]"
                  data-testid="invites-create-error"
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
                data-testid="invites-create-submit"
              >
                {createSubmitting && <Loader2 size={12} className="animate-spin" />}
                Send invite
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Projects tab ──────────────────────────────────────────────

function ProjectsTab({ tid }: { tid: string }) {
  const [rows, setRows] = useState<TenantProjectInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createState, setCreateState] = useState<{
    name: string
    slug: string
    product_line: ProductLine
    submitting: boolean
    error: string | null
  }>({
    name: "",
    slug: "",
    product_line: "embedded",
    submitting: false,
    error: null,
  })

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const projects = await listAllTenantProjects(tid)
      setRows(projects)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setLoading(false)
    }
  }, [tid])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const onArchive = useCallback(
    async (pid: string) => {
      setBusyId(pid)
      try {
        await archiveTenantProject(tid, pid)
        await refresh()
      } catch (exc) {
        setError(describeError(exc))
      } finally {
        setBusyId(null)
      }
    },
    [tid, refresh],
  )

  const onRestore = useCallback(
    async (pid: string) => {
      setBusyId(pid)
      try {
        await restoreTenantProject(tid, pid)
        await refresh()
      } catch (exc) {
        setError(describeError(exc))
      } finally {
        setBusyId(null)
      }
    },
    [tid, refresh],
  )

  const onCreate = useCallback(async () => {
    setCreateState((s) => ({ ...s, submitting: true, error: null }))
    if (!createState.name.trim()) {
      setCreateState((s) => ({ ...s, submitting: false, error: "Name is required." }))
      return
    }
    if (!SLUG_PATTERN.test(createState.slug)) {
      setCreateState((s) => ({
        ...s,
        submitting: false,
        error: "Slug must match ^[a-z0-9][a-z0-9-]*$.",
      }))
      return
    }
    try {
      await createTenantProject(tid, {
        name: createState.name.trim(),
        slug: createState.slug,
        product_line: createState.product_line,
      })
      setCreateOpen(false)
      setCreateState({
        name: "",
        slug: "",
        product_line: "embedded",
        submitting: false,
        error: null,
      })
      await refresh()
    } catch (exc) {
      setCreateState((s) => ({ ...s, submitting: false, error: describeError(exc) }))
    }
  }, [tid, createState, refresh])

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold inline-flex items-center gap-2">
          <Folder size={14} />
          Projects
        </h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="projects-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs font-mono"
            data-testid="projects-create-btn"
          >
            <Plus size={12} />
            New project
          </button>
        </div>
      </div>

      {error && (
        <div
          className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-xs font-mono text-[var(--destructive)]"
          data-testid="projects-error"
        >
          {error}
        </div>
      )}

      <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-x-auto">
        <table className="w-full font-mono text-xs">
          <thead>
            <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
              <th className="text-left px-3 py-2">name</th>
              <th className="text-left px-3 py-2">slug</th>
              <th className="text-left px-3 py-2">product line</th>
              <th className="text-left px-3 py-2">status</th>
              <th className="text-right px-3 py-2">actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="projects-loading">
                  <Loader2 size={14} className="animate-spin inline-block mr-2" />
                  Loading projects…
                </td>
              </tr>
            )}
            {!loading && rows.length === 0 && !error && (
              <tr>
                <td colSpan={5} className="text-center py-8 text-[var(--muted-foreground)]" data-testid="projects-empty">
                  No projects. Click <strong>New project</strong> to create one.
                </td>
              </tr>
            )}
            {!loading &&
              rows.map((p) => {
                const archived = p.archived_at !== null
                const busy = busyId === p.project_id
                return (
                  <tr
                    key={p.project_id}
                    className={`border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--secondary)]/20 ${
                      archived ? "opacity-60" : ""
                    }`}
                    data-testid={`project-row-${p.project_id}`}
                  >
                    <td className="px-3 py-2">{p.name}</td>
                    <td className="px-3 py-2 text-[var(--muted-foreground)]">{p.slug}</td>
                    <td className="px-3 py-2">{p.product_line}</td>
                    <td className="px-3 py-2">
                      {archived ? (
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-[var(--muted)]/40 text-[var(--muted-foreground)]">
                          archived
                        </span>
                      ) : (
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-[var(--neural-green)]/15 text-[var(--neural-green)]">
                          live
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right">
                      {archived ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void onRestore(p.project_id)}
                          className="px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40 text-[10px] inline-flex items-center gap-1 disabled:opacity-50"
                          data-testid={`project-restore-${p.project_id}`}
                        >
                          {busy ? <Loader2 size={10} className="animate-spin" /> : <RotateCcw size={10} />}
                          Restore
                        </button>
                      ) : (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void onArchive(p.project_id)}
                          className="px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--destructive)]/20 text-[10px] inline-flex items-center gap-1 disabled:opacity-50"
                          data-testid={`project-archive-${p.project_id}`}
                        >
                          {busy ? <Loader2 size={10} className="animate-spin" /> : <Archive size={10} />}
                          Archive
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Create project"
          data-testid="projects-create-dialog"
        >
          <div className="w-full max-w-md rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold inline-flex items-center gap-2">
                <Plus size={14} /> New project
              </h3>
              <button
                type="button"
                aria-label="Close create dialog"
                onClick={() => setCreateOpen(false)}
                className="p-1 rounded hover:bg-[var(--secondary)]/40"
              >
                <X size={14} />
              </button>
            </div>
            <div className="space-y-3 text-xs">
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Name</span>
                <input
                  type="text"
                  value={createState.name}
                  onChange={(e) => setCreateState((s) => ({ ...s, name: e.target.value }))}
                  placeholder="Acme firmware"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="projects-create-name"
                />
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Slug</span>
                <input
                  type="text"
                  value={createState.slug}
                  onChange={(e) => setCreateState((s) => ({ ...s, slug: e.target.value }))}
                  placeholder="acme-firmware"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="projects-create-slug"
                />
                <span className="block text-[10px] text-[var(--muted-foreground)] mt-1">
                  Pattern: ^[a-z0-9][a-z0-9-]*$
                </span>
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">Product line</span>
                <select
                  value={createState.product_line}
                  onChange={(e) =>
                    setCreateState((s) => ({ ...s, product_line: e.target.value as ProductLine }))
                  }
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="projects-create-line"
                >
                  {PRODUCT_LINES.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              {createState.error && (
                <div
                  className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-[11px] text-[var(--destructive)]"
                  data-testid="projects-create-error"
                >
                  {createState.error}
                </div>
              )}
            </div>
            <div className="flex items-center justify-end gap-2 mt-5">
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                disabled={createState.submitting}
                className="px-3 py-1.5 rounded border border-[var(--border)] text-xs disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void onCreate()}
                disabled={createState.submitting}
                className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs disabled:opacity-50"
                data-testid="projects-create-submit"
              >
                {createState.submitting && <Loader2 size={12} className="animate-spin" />}
                Create
              </button>
            </div>
          </div>
        </div>
      )}

      <p className="mt-3 text-[10px] text-[var(--muted-foreground)] font-mono">
        Archived projects are kept for OMNISIGHT_PROJECT_GC_RETENTION_DAYS (default 90) before background GC permanently deletes them.
      </p>
    </div>
  )
}

// ─── Quotas tab ─────────────────────────────────────────────────

function QuotasTab({ tid }: { tid: string }) {
  const [usage, setUsage] = useState<TenantStorageUsage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const u = await getStorageUsage(tid)
      setUsage(u)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setLoading(false)
    }
  }, [tid])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const softPct = usage ? Math.min(100, (usage.usage.total_bytes / usage.quota.soft_bytes) * 100) : 0
  const hardPct = usage ? Math.min(100, (usage.usage.total_bytes / usage.quota.hard_bytes) * 100) : 0

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold inline-flex items-center gap-2">
          <Archive size={14} />
          Quotas &amp; usage
        </h2>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
          data-testid="quotas-refresh"
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </div>

      {error && (
        <div
          className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-xs font-mono text-[var(--destructive)]"
          data-testid="quotas-error"
        >
          {error}
        </div>
      )}

      {loading && (
        <div className="rounded border border-[var(--border)] bg-[var(--card)] py-8 text-center text-xs font-mono text-[var(--muted-foreground)]" data-testid="quotas-loading">
          <Loader2 size={14} className="animate-spin inline-block mr-2" />
          Loading quotas…
        </div>
      )}

      {!loading && usage && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="quotas-detail">
          <div className="rounded border border-[var(--border)] bg-[var(--card)] p-4 font-mono">
            <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wide mb-1">
              Plan
            </div>
            <div className="text-lg font-semibold" data-testid="quotas-plan">
              {usage.plan}
            </div>
            <div className="text-[10px] text-[var(--muted-foreground)] mt-1">
              keep_recent_runs · {usage.quota.keep_recent_runs}
            </div>
          </div>

          <div className="rounded border border-[var(--border)] bg-[var(--card)] p-4 font-mono">
            <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wide mb-1">
              Disk usage
            </div>
            <div className="text-lg font-semibold" data-testid="quotas-disk-used">
              {formatBytes(usage.usage.total_bytes)}
            </div>
            <div className="text-[10px] text-[var(--muted-foreground)] mt-1">
              soft · {formatBytes(usage.quota.soft_bytes)} · hard ·{" "}
              {formatBytes(usage.quota.hard_bytes)}
            </div>
            <div className="mt-3">
              <div className="h-1.5 rounded bg-[var(--muted)]/40 overflow-hidden">
                <div
                  className={`h-full ${
                    usage.over_hard
                      ? "bg-[var(--destructive)]"
                      : usage.over_soft
                        ? "bg-yellow-500"
                        : "bg-[var(--neural-green)]"
                  }`}
                  style={{ width: `${hardPct}%` }}
                  aria-label={`Disk usage ${hardPct.toFixed(0)}% of hard quota`}
                />
              </div>
              <div className="flex items-center justify-between text-[10px] text-[var(--muted-foreground)] mt-1">
                <span>{softPct.toFixed(0)}% of soft</span>
                <span>{hardPct.toFixed(0)}% of hard</span>
              </div>
            </div>
            {(usage.over_soft || usage.over_hard) && (
              <div
                className="mt-2 text-[10px] text-[var(--destructive)]"
                data-testid="quotas-overage"
              >
                {usage.over_hard ? "Over hard quota — writes refused" : "Over soft quota — cleanup pending"}
              </div>
            )}
          </div>

          <div
            className="rounded border border-[var(--border)] bg-[var(--card)] p-4 font-mono md:col-span-2"
            data-testid="quotas-breakdown"
          >
            <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wide mb-2">
              Breakdown
            </div>
            <ul className="space-y-1 text-xs">
              <li className="flex items-center justify-between">
                <span>artifacts</span>
                <span>{formatBytes(usage.usage.artifacts_bytes)}</span>
              </li>
              <li className="flex items-center justify-between">
                <span>workflow runs</span>
                <span>{formatBytes(usage.usage.workflow_runs_bytes)}</span>
              </li>
              <li className="flex items-center justify-between">
                <span>backups</span>
                <span>{formatBytes(usage.usage.backups_bytes)}</span>
              </li>
              <li className="flex items-center justify-between">
                <span>ingest tmp</span>
                <span>{formatBytes(usage.usage.ingest_tmp_bytes)}</span>
              </li>
            </ul>
          </div>
        </div>
      )}

      <p className="mt-3 text-[10px] text-[var(--muted-foreground)] font-mono">
        Plan changes ship via the super-admin /admin/tenants surface (Y8 row 3). This panel is read-only for tenant admins.
      </p>
    </div>
  )
}

// ─── Security tab / KS.2.1 CMEK wizard ─────────────────────────

const CMEK_STEPS = [
  "Provider",
  "IAM policy",
  "Key id",
  "Verify",
  "Done",
] as const

function SecurityTab({ tid }: { tid: string }) {
  const [providers, setProviders] = useState<CmekProviderSpec[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [step, setStep] = useState(0)
  const [provider, setProvider] = useState<CmekProvider>("aws-kms")
  const [principal, setPrincipal] = useState("")
  const [keyId, setKeyId] = useState("")
  const [policyJson, setPolicyJson] = useState("")
  const [verifyResult, setVerifyResult] = useState<VerifyCmekResponse | null>(null)
  const [completeResult, setCompleteResult] = useState<CompleteCmekResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const selected = useMemo(
    () => providers.find((p) => p.provider === provider) ?? providers[0],
    [providers, provider],
  )

  const securityTier = completeResult?.security_tier ?? "tier-1"

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    listCmekWizardProviders(tid)
      .then((res) => {
        if (cancelled) return
        setProviders(res.providers)
        if (res.providers[0]) {
          setProvider(res.providers[0].provider)
          setPrincipal(res.providers[0].policy_target_example)
          setKeyId(res.providers[0].key_id_example)
        }
      })
      .catch((exc) => {
        if (!cancelled) setLoadError(describeError(exc))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [tid])

  useEffect(() => {
    if (!selected) return
    setPrincipal(selected.policy_target_example)
    setKeyId(selected.key_id_example)
    setPolicyJson("")
    setVerifyResult(null)
    setCompleteResult(null)
    setError(null)
    setStep(0)
  }, [selected])

  async function onGeneratePolicy() {
    setBusy(true)
    setError(null)
    try {
      const res = await generateCmekWizardPolicy(tid, {
        provider,
        principal,
        key_id: keyId.trim() || null,
      })
      setPolicyJson(res.policy_json)
      setStep(1)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setBusy(false)
    }
  }

  async function onVerify() {
    setBusy(true)
    setError(null)
    try {
      const res = await verifyCmekWizardConnection(tid, { provider, key_id: keyId })
      setVerifyResult(res)
      setStep(3)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setBusy(false)
    }
  }

  async function onComplete() {
    if (!verifyResult) return
    setBusy(true)
    setError(null)
    try {
      const res = await completeCmekWizard(tid, {
        provider,
        key_id: keyId,
        verification_id: verifyResult.verification_id,
      })
      setCompleteResult(res)
      setStep(4)
    } catch (exc) {
      setError(describeError(exc))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="rounded border border-[var(--border)] bg-[var(--card)] p-6 font-mono text-xs text-[var(--muted-foreground)]">
        <Loader2 size={14} className="animate-spin inline-block mr-2" />
        Loading CMEK wizard…
      </div>
    )
  }

  if (loadError) {
    return (
      <div
        className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
        data-testid="cmek-load-error"
      >
        {loadError}
      </div>
    )
  }

  return (
    <div data-testid="cmek-security-tab">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3 mb-4">
        <div>
          <h2 className="text-sm font-semibold inline-flex items-center gap-2">
            <KeyRound size={14} />
            CMEK security tier
          </h2>
          <p className="text-[10px] text-[var(--muted-foreground)] font-mono mt-1">
            Configure a customer-managed key draft for tenant {tid}.
          </p>
        </div>
        <div
          className={`inline-flex items-center gap-2 rounded border px-3 py-2 text-xs font-mono ${
            securityTier === "tier-2"
              ? "border-[var(--neural-green)]/50 bg-[var(--neural-green)]/10"
              : "border-[var(--border)] bg-[var(--card)]"
          }`}
          data-testid="cmek-security-tier"
        >
          <ShieldAlert size={13} />
          {securityTier === "tier-2" ? "Tier 2 · CMEK draft" : "Tier 1 · OmniSight-managed KEK"}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[220px_1fr] gap-4">
        <ol className="rounded border border-[var(--border)] bg-[var(--card)] p-3 font-mono text-xs space-y-1">
          {CMEK_STEPS.map((label, idx) => {
            const done = step > idx
            const active = step === idx
            return (
              <li
                key={label}
                className={`flex items-center gap-2 rounded px-2 py-2 ${
                  active ? "bg-[var(--secondary)]/40 text-[var(--foreground)]" : "text-[var(--muted-foreground)]"
                }`}
                data-testid={`cmek-step-${idx + 1}`}
              >
                {done ? <CheckCircle2 size={13} className="text-[var(--neural-green)]" /> : <span className="w-[13px] text-center">{idx + 1}</span>}
                {label}
              </li>
            )
          })}
        </ol>

        <div className="rounded border border-[var(--border)] bg-[var(--card)] p-4 font-mono">
          {error && (
            <div
              className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-xs text-[var(--destructive)]"
              data-testid="cmek-error"
            >
              {error}
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-3 gap-2 mb-4" role="radiogroup" aria-label="KMS provider">
            {providers.map((p) => (
              <button
                key={p.provider}
                type="button"
                onClick={() => setProvider(p.provider)}
                className={`rounded border px-3 py-3 text-left text-xs transition-colors ${
                  provider === p.provider
                    ? "border-[var(--neural-blue)] bg-[var(--neural-blue)]/10"
                    : "border-[var(--border)] hover:bg-[var(--secondary)]/30"
                }`}
                data-testid={`cmek-provider-${p.provider}`}
              >
                <span className="block font-semibold">{p.label}</span>
                <span className="block text-[10px] text-[var(--muted-foreground)] mt-1">
                  {p.key_id_label}
                </span>
              </button>
            ))}
          </div>

          <div className="space-y-4">
            <label className="block">
              <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
                {selected?.policy_target_label ?? "OmniSight principal"}
              </span>
              <input
                value={principal}
                onChange={(e) => setPrincipal(e.target.value)}
                className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-xs"
                placeholder={selected?.policy_target_example}
                data-testid="cmek-principal-input"
              />
            </label>

            <div>
              <div className="flex items-center justify-between gap-2 mb-1">
                <span className="text-[10px] text-[var(--muted-foreground)]">Generated IAM policy JSON</span>
                <button
                  type="button"
                  onClick={() => void onGeneratePolicy()}
                  disabled={busy || !principal.trim()}
                  className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-2 py-1 text-[10px] disabled:opacity-50"
                  data-testid="cmek-generate-policy"
                >
                  {busy ? <Loader2 size={10} className="animate-spin" /> : <Copy size={10} />}
                  Generate
                </button>
              </div>
              <pre
                className="min-h-40 max-h-64 overflow-auto rounded border border-[var(--border)] bg-[var(--background)] p-3 text-[10px] leading-relaxed whitespace-pre-wrap"
                data-testid="cmek-policy-json"
              >
                {policyJson || "Generate the policy, then paste the JSON into your cloud console."}
              </pre>
            </div>

            <label className="block">
              <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
                {selected?.key_id_label ?? "KMS key id"}
              </span>
              <input
                value={keyId}
                onChange={(e) => {
                  setKeyId(e.target.value)
                  setVerifyResult(null)
                  setCompleteResult(null)
                }}
                className="w-full rounded border border-[var(--border)] bg-[var(--background)] px-3 py-2 text-xs"
                placeholder={selected?.key_id_example}
                data-testid="cmek-key-id-input"
              />
            </label>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  setStep(2)
                  void onVerify()
                }}
                disabled={busy || !policyJson || !keyId.trim()}
                className="inline-flex items-center gap-1 rounded bg-[var(--neural-blue)] px-3 py-2 text-xs text-[var(--background)] disabled:opacity-50"
                data-testid="cmek-verify"
              >
                {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                Verify connection
              </button>
              <button
                type="button"
                onClick={() => void onComplete()}
                disabled={busy || !verifyResult?.ok}
                className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-3 py-2 text-xs disabled:opacity-50"
                data-testid="cmek-complete"
              >
                <CheckCircle2 size={12} />
                Done
              </button>
            </div>

            {verifyResult && (
              <div
                className="rounded border border-[var(--neural-green)]/40 bg-[var(--neural-green)]/10 p-3 text-xs"
                data-testid="cmek-verify-result"
              >
                encrypt-decrypt ok · {verifyResult.algorithm} · {verifyResult.elapsed_ms} ms · {verifyResult.verification_id}
              </div>
            )}

            {completeResult && (
              <div
                className="rounded border border-[var(--neural-green)]/40 bg-[var(--neural-green)]/10 p-3 text-xs"
                data-testid="cmek-complete-result"
              >
                Tier 2 draft ready for {completeResult.provider}; durable activation follows KS.2.11 storage.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

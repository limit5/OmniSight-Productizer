"use client"

/**
 * Y8 row 3 — super-admin only /admin/tenants page.
 *
 * The Y2 (#278) backend admin REST surface is already in place — this
 * page is the operator-facing UI on top of it:
 *
 *   • GET    /api/v1/admin/tenants            → list rows + usage
 *   • POST   /api/v1/admin/tenants            → create row
 *   • PATCH  /api/v1/admin/tenants/{id}       → plan / enabled / rename
 *
 * Auth gating
 * ───────────
 * The page renders an access-denied placeholder unless the auth context
 * reports ``user.role === "super_admin"`` (or ``authMode === "open"``,
 * which is the dev-only synthetic anon-admin). Tenant admins (role
 * ``admin``) get the same 403 page — the backend would 403 every call
 * anyway, but failing fast in the UI avoids a screen-full of toasts.
 *
 * Module-global state audit
 * ─────────────────────────
 * None introduced. The page reads ``useAuth()`` (per-tab React context),
 * its own ``useState`` rows / dialog flags (per-component instance),
 * and calls `lib/api.ts` wrappers (which set X-Tenant-Id from the
 * already-audited ``_currentTenantId`` module-global; switching tenant
 * mid-screen would be unusual but harmless — every admin call resolves
 * server-side via the super-admin gate, not via tenant scoping).
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  Building2,
  Check,
  ChevronRight,
  CircleAlert,
  Loader2,
  Plus,
  RefreshCw,
  ShieldAlert,
  X,
} from "lucide-react"
import {
  ApiError,
  adminCreateTenant,
  adminListTenants,
  adminPatchTenant,
  type AdminTenantRow,
  type TenantPlan,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const PLANS: TenantPlan[] = ["free", "starter", "pro", "enterprise"]
const TENANT_ID_PATTERN = /^t-[a-z0-9][a-z0-9-]{2,62}$/

function formatBytes(n: number): string {
  if (!n) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

function formatRelative(ts: number | null): string {
  if (ts == null) return "—"
  const ageS = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (ageS < 60) return `${ageS}s ago`
  if (ageS < 3600) return `${Math.floor(ageS / 60)}m ago`
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ago`
  return `${Math.floor(ageS / 86400)}d ago`
}

interface CreateDialogState {
  id: string
  name: string
  plan: TenantPlan
  enabled: boolean
  submitting: boolean
  error: string | null
}

const EMPTY_CREATE: CreateDialogState = {
  id: "",
  name: "",
  plan: "free",
  enabled: true,
  submitting: false,
  error: null,
}

export default function AdminTenantsPage() {
  const { user, authMode, loading: authLoading } = useAuth()

  const isSuperAdmin = useMemo(() => {
    if (authMode === "open") return true
    return user?.role === "super_admin"
  }, [user, authMode])

  const [rows, setRows] = useState<AdminTenantRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyTenantId, setBusyTenantId] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [createState, setCreateState] = useState<CreateDialogState>(EMPTY_CREATE)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await adminListTenants()
      setRows(res.tenants)
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!isSuperAdmin) {
      setLoading(false)
      return
    }
    void refresh()
  }, [isSuperAdmin, refresh])

  const onCreate = useCallback(async () => {
    setCreateState((s) => ({ ...s, submitting: true, error: null }))
    if (!TENANT_ID_PATTERN.test(createState.id)) {
      setCreateState((s) => ({
        ...s,
        submitting: false,
        error:
          "Tenant id must match ^t-[a-z0-9][a-z0-9-]{2,62}$ (e.g. t-acme).",
      }))
      return
    }
    if (!createState.name.trim()) {
      setCreateState((s) => ({
        ...s,
        submitting: false,
        error: "Name is required.",
      }))
      return
    }
    try {
      await adminCreateTenant({
        id: createState.id,
        name: createState.name.trim(),
        plan: createState.plan,
        enabled: createState.enabled,
      })
      setCreateOpen(false)
      setCreateState(EMPTY_CREATE)
      await refresh()
    } catch (exc) {
      const msg =
        exc instanceof ApiError
          ? `${exc.status}: ${(exc.parsed as { detail?: string } | null)?.detail ?? exc.body}`
          : exc instanceof Error
            ? exc.message
            : String(exc)
      setCreateState((s) => ({ ...s, submitting: false, error: msg }))
    }
  }, [createState, refresh])

  const onPatch = useCallback(
    async (
      id: string,
      patch: { plan?: TenantPlan; enabled?: boolean; name?: string },
    ) => {
      setBusyTenantId(id)
      setRowError(null)
      try {
        const updated = await adminPatchTenant(id, patch)
        setRows((current) =>
          current.map((r) =>
            r.id === id
              ? { ...r, name: updated.name, plan: updated.plan, enabled: updated.enabled }
              : r,
          ),
        )
      } catch (exc) {
        const detail =
          exc instanceof ApiError
            ? (exc.parsed as { detail?: string } | null)?.detail ?? exc.body
            : exc instanceof Error
              ? exc.message
              : String(exc)
        setRowError({ id, message: detail || "patch failed" })
      } finally {
        setBusyTenantId(null)
      }
    },
    [],
  )

  // ── Access-denied / loading short-circuits ────────────────────────

  if (authLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying super-admin session…
        </div>
      </main>
    )
  }

  if (!isSuperAdmin) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="admin-tenants-forbidden"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <ShieldAlert size={16} />
            <span className="text-sm font-semibold">403 — super-admin required</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            The /admin/tenants surface is gated by the platform-tier
            <code className="mx-1 px-1 rounded bg-[var(--secondary)]/40">super_admin</code>
            role. Tenant admins (role <code className="px-1 rounded bg-[var(--secondary)]/40">admin</code>)
            manage their own tenant via <Link href="/" className="underline">/tenants/&#123;tid&#125;/settings</Link>.
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
      data-testid="admin-tenants-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-center justify-between mb-6">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link href="/" className="hover:text-[var(--foreground)] inline-flex items-center gap-1">
                <ArrowLeft size={10} /> dashboard
              </Link>
              <ChevronRight size={10} />
              <span>admin</span>
              <ChevronRight size={10} />
              <span className="text-[var(--foreground)]">tenants</span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <Building2 size={20} />
              Tenants
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Super-admin scope. Create new tenants, change plans, enable / disable.
              Cascade-delete and per-tenant detail panel ship in a follow-up row.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={loading}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
              data-testid="admin-tenants-refresh"
            >
              <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
              Refresh
            </button>
            <button
              type="button"
              onClick={() => {
                setCreateState(EMPTY_CREATE)
                setCreateOpen(true)
              }}
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs font-mono"
              data-testid="admin-tenants-create-btn"
            >
              <Plus size={12} />
              New tenant
            </button>
          </div>
        </header>

        {error && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)] flex items-center gap-2"
            data-testid="admin-tenants-error"
          >
            <CircleAlert size={12} />
            <span>Failed to load tenants: {error}</span>
          </div>
        )}

        <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-x-auto">
          <table className="w-full font-mono text-xs">
            <thead>
              <tr className="border-b border-[var(--border)] text-[10px] text-[var(--muted-foreground)]">
                <th className="text-left px-3 py-2">id</th>
                <th className="text-left px-3 py-2">name</th>
                <th className="text-left px-3 py-2">plan</th>
                <th className="text-left px-3 py-2">status</th>
                <th className="text-right px-3 py-2">users</th>
                <th className="text-right px-3 py-2">projects</th>
                <th className="text-right px-3 py-2">disk</th>
                <th className="text-right px-3 py-2">tokens 30d</th>
                <th className="text-right px-3 py-2">last activity</th>
                <th className="text-right px-3 py-2">actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td
                    colSpan={10}
                    className="text-center py-8 text-[var(--muted-foreground)]"
                    data-testid="admin-tenants-loading"
                  >
                    <Loader2 size={14} className="animate-spin inline-block mr-2" />
                    Loading tenants…
                  </td>
                </tr>
              )}
              {!loading && rows.length === 0 && !error && (
                <tr>
                  <td
                    colSpan={10}
                    className="text-center py-8 text-[var(--muted-foreground)]"
                    data-testid="admin-tenants-empty"
                  >
                    No tenants. Click <strong>New tenant</strong> to create one.
                  </td>
                </tr>
              )}
              {!loading &&
                rows.map((r) => {
                  const busy = busyTenantId === r.id
                  const isError = rowError?.id === r.id
                  return (
                    <tr
                      key={r.id}
                      className={`border-b border-[var(--border)] last:border-b-0 hover:bg-[var(--secondary)]/20 ${
                        !r.enabled ? "opacity-60" : ""
                      }`}
                      data-testid={`admin-tenant-row-${r.id}`}
                    >
                      <td className="px-3 py-2 font-semibold">{r.id}</td>
                      <td className="px-3 py-2">{r.name}</td>
                      <td className="px-3 py-2">
                        <select
                          value={r.plan}
                          disabled={busy}
                          onChange={(e) =>
                            void onPatch(r.id, {
                              plan: e.target.value as TenantPlan,
                            })
                          }
                          aria-label={`Change plan for ${r.id}`}
                          className="px-1 py-0.5 rounded bg-[var(--background)] border border-[var(--border)] text-xs font-mono"
                          data-testid={`admin-tenant-plan-${r.id}`}
                        >
                          {PLANS.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] ${
                            r.enabled
                              ? "bg-[var(--neural-green)]/15 text-[var(--neural-green)]"
                              : "bg-[var(--muted)]/40 text-[var(--muted-foreground)]"
                          }`}
                          data-testid={`admin-tenant-status-${r.id}`}
                        >
                          {r.enabled ? <Check size={10} /> : <X size={10} />}
                          {r.enabled ? "enabled" : "disabled"}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right">{r.usage.user_count}</td>
                      <td className="px-3 py-2 text-right">{r.usage.project_count}</td>
                      <td className="px-3 py-2 text-right">
                        {formatBytes(r.usage.disk_used_bytes)}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {r.usage.llm_tokens_30d.toLocaleString()}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {formatRelative(r.usage.last_activity_at)}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void onPatch(r.id, { enabled: !r.enabled })
                          }
                          aria-label={
                            r.enabled
                              ? `Disable tenant ${r.id}`
                              : `Enable tenant ${r.id}`
                          }
                          className="px-2 py-0.5 rounded border border-[var(--border)] hover:bg-[var(--secondary)]/40 text-[10px] disabled:opacity-50"
                          data-testid={`admin-tenant-toggle-${r.id}`}
                        >
                          {busy ? (
                            <Loader2 size={10} className="animate-spin inline" />
                          ) : r.enabled ? (
                            "Disable"
                          ) : (
                            "Enable"
                          )}
                        </button>
                        {isError && (
                          <div
                            className="text-[10px] text-[var(--destructive)] mt-1"
                            data-testid={`admin-tenant-row-error-${r.id}`}
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

        <p className="mt-4 text-[10px] text-[var(--muted-foreground)] font-mono">
          Y2 (#278) backend; Y8 row 3 frontend. Plan downgrade is refused
          server-side (409) when the tenant&apos;s current disk usage exceeds the
          new plan&apos;s hard quota — the row error message reflects the gap.
        </p>
      </div>

      {createOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="Create tenant"
          data-testid="admin-tenants-create-dialog"
        >
          <div className="w-full max-w-md rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold flex items-center gap-2">
                <Plus size={14} /> Create tenant
              </h2>
              <button
                type="button"
                onClick={() => setCreateOpen(false)}
                aria-label="Close create dialog"
                className="p-1 rounded hover:bg-[var(--secondary)]/40"
              >
                <X size={14} />
              </button>
            </div>
            <div className="space-y-3 text-xs">
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
                  Tenant id
                </span>
                <input
                  type="text"
                  value={createState.id}
                  onChange={(e) =>
                    setCreateState((s) => ({ ...s, id: e.target.value }))
                  }
                  placeholder="t-acme"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="admin-tenants-create-id"
                />
                <span className="block text-[10px] text-[var(--muted-foreground)] mt-1">
                  Pattern: ^t-[a-z0-9][a-z0-9-]&#123;2,62&#125;$
                </span>
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
                  Display name
                </span>
                <input
                  type="text"
                  value={createState.name}
                  onChange={(e) =>
                    setCreateState((s) => ({ ...s, name: e.target.value }))
                  }
                  placeholder="Acme Robotics"
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="admin-tenants-create-name"
                />
              </label>
              <label className="block">
                <span className="block text-[10px] text-[var(--muted-foreground)] mb-1">
                  Plan
                </span>
                <select
                  value={createState.plan}
                  onChange={(e) =>
                    setCreateState((s) => ({
                      ...s,
                      plan: e.target.value as TenantPlan,
                    }))
                  }
                  className="w-full px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] text-xs"
                  data-testid="admin-tenants-create-plan"
                >
                  {PLANS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={createState.enabled}
                  onChange={(e) =>
                    setCreateState((s) => ({ ...s, enabled: e.target.checked }))
                  }
                  data-testid="admin-tenants-create-enabled"
                />
                <span>Enabled on creation</span>
              </label>
              {createState.error && (
                <div
                  className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-2 text-[11px] text-[var(--destructive)]"
                  data-testid="admin-tenants-create-error"
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
                data-testid="admin-tenants-create-submit"
              >
                {createState.submitting && (
                  <Loader2 size={12} className="animate-spin" />
                )}
                Create
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}

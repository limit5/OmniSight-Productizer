"use client"

/**
 * OP-741 -- CI recovery dead-letter dashboard.
 *
 * Shows patchsets paused by the loop guard and provides the four
 * operator exits: re-trigger CI, abandon PS, mark as quarantine, or
 * convert to manual review. Server-side action handlers write audit
 * rows; this component only owns local presentation state.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  ChevronRight,
  CircleAlert,
  FileWarning,
  Loader2,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  StopCircle,
  UserCheck,
} from "lucide-react"
import {
  ApiError,
  applyCiDeadLetterAction,
  listCiDeadLetters,
  type CiDeadLetterAction,
  type CiDeadLetterRow,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const ROLE_ORDER = ["viewer", "operator", "admin", "super_admin"]

function roleAtLeast(role: string | undefined, minRole: string): boolean {
  const have = role ? ROLE_ORDER.indexOf(role) : -1
  const need = ROLE_ORDER.indexOf(minRole)
  return have >= 0 && need >= 0 && have >= need
}

function formatRelative(ts: number): string {
  const ageS = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (ageS < 60) return `${ageS}s ago`
  if (ageS < 3600) return `${Math.floor(ageS / 60)}m ago`
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ago`
  return `${Math.floor(ageS / 86400)}d ago`
}

const ACTIONS: Array<{
  id: CiDeadLetterAction
  label: string
  icon: typeof RotateCcw
}> = [
  { id: "retrigger-ci", label: "Retry CI", icon: RotateCcw },
  { id: "abandon-ps", label: "Abandon", icon: StopCircle },
  { id: "mark-quarantine", label: "Quarantine", icon: FileWarning },
  { id: "manual-review", label: "Manual", icon: UserCheck },
]

export default function AdminCiDeadLetterPage() {
  const { user, authMode, loading: authLoading } = useAuth()

  const isAdmin = useMemo(() => {
    if (authMode === "open") return true
    return roleAtLeast(user?.role, "admin")
  }, [authMode, user?.role])

  const [rows, setRows] = useState<CiDeadLetterRow[]>([])
  const [loading, setLoading] = useState(true)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastAction, setLastAction] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await listCiDeadLetters()
      setRows(res.items)
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (authLoading) return
    if (!isAdmin) {
      setLoading(false)
      return
    }
    void refresh()
  }, [authLoading, isAdmin, refresh])

  const onAction = useCallback(
    async (row: CiDeadLetterRow, action: CiDeadLetterAction) => {
      setBusyKey(`${row.change.jira_key}:${action}`)
      setError(null)
      setLastAction(null)
      try {
        const res = await applyCiDeadLetterAction(
          row.change.jira_key,
          action,
          `operator selected ${action} from /admin/ci-dead-letter`,
        )
        setLastAction(`${res.jira_key}: ${res.action}`)
        await refresh()
      } catch (exc) {
        const detail =
          exc instanceof ApiError
            ? (exc.parsed as { detail?: string } | null)?.detail ?? exc.body
            : exc instanceof Error
              ? exc.message
              : String(exc)
        setError(detail || "dead-letter action failed")
      } finally {
        setBusyKey(null)
      }
    },
    [refresh],
  )

  if (authLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying operator session...
        </div>
      </main>
    )
  }

  if (!isAdmin) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6">
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <ShieldAlert size={16} />
            <span className="text-sm font-semibold">403 - admin required</span>
          </div>
          <Link href="/" className="inline-flex items-center gap-1 text-xs underline text-[var(--neural-blue)]">
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10">
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
              <span className="text-[var(--foreground)]">ci dead-letter</span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <CircleAlert size={20} />
              CI dead-letter
            </h1>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        {error ? (
          <div className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]">
            {error}
          </div>
        ) : null}
        {lastAction ? (
          <div className="mb-4 rounded border border-[var(--border)] bg-[var(--card)] p-3 text-xs font-mono">
            Applied {lastAction}
          </div>
        ) : null}

        <section className="border border-[var(--border)] rounded bg-[var(--card)] overflow-hidden">
          <div className="grid grid-cols-[1fr_120px_120px_360px] gap-3 px-3 py-2 text-[10px] uppercase tracking-wide text-[var(--muted-foreground)] font-mono border-b border-[var(--border)]">
            <span>Patchset</span>
            <span>Attempts</span>
            <span>Paused</span>
            <span>Operator exits</span>
          </div>
          {loading ? (
            <div className="p-6 text-xs font-mono text-[var(--muted-foreground)] flex items-center gap-2">
              <Loader2 size={14} className="animate-spin" />
              Loading...
            </div>
          ) : rows.length === 0 ? (
            <div className="p-6 text-xs font-mono text-[var(--muted-foreground)]">
              No paused CI patchsets.
            </div>
          ) : (
            rows.map((row) => (
              <div
                key={row.change.jira_key}
                className="grid grid-cols-[1fr_120px_120px_360px] gap-3 px-3 py-3 text-xs font-mono border-b border-[var(--border)] last:border-b-0"
              >
                <div className="min-w-0">
                  <div className="font-semibold truncate">
                    #{row.change.number} / {row.change.jira_key}
                  </div>
                  <div className="text-[var(--muted-foreground)] truncate">
                    {row.reason}
                  </div>
                </div>
                <span>{row.attempt_count}</span>
                <span>{formatRelative(row.paused_at)}</span>
                <div className="flex flex-wrap gap-2">
                  {ACTIONS.map((action) => {
                    const Icon = action.icon
                    const key = `${row.change.jira_key}:${action.id}`
                    return (
                      <button
                        key={action.id}
                        type="button"
                        onClick={() => void onAction(row, action.id)}
                        disabled={busyKey !== null}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded border border-[var(--border)] bg-[var(--background)] hover:bg-[var(--secondary)]/40 disabled:opacity-50"
                        title={action.label}
                      >
                        <Icon size={12} />
                        {busyKey === key ? "..." : action.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            ))
          )}
        </section>
      </div>
    </main>
  )
}

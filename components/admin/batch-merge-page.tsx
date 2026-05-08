"use client"

/**
 * OP-735 R5 -- /admin/batch-merge operator dashboard.
 *
 * The AI Reviewer (OP-713 Phase 2+) auto-tags low-risk patchsets with
 * ``runner-batch-merge-candidate`` and posts +1. This page surfaces
 * those tags as a sortable / filterable table; the operator multi-
 * selects rows and clicks "Confirm and +2 selected" to bulk-approve.
 *
 * Auth gating
 * -----------
 * Admin+ only. CLAUDE.md L1 invariant ("AI reviewer max +1, human +2
 * required for merge") is preserved -- the +2 is posted from this
 * page's authenticated session, not by the AI.
 *
 * Module-global state audit
 * -------------------------
 * None introduced. Per-component React state only; the typed
 * ``lib/api.ts`` wrappers handle the network. Audit chain coherence is
 * server-side via ``backend.audit.log`` per +2.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  ChevronRight,
  CircleAlert,
  GitMerge,
  Loader2,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
} from "lucide-react"
import {
  ApiError,
  approveBatchMergeCandidates,
  listBatchMergeCandidates,
  type BatchMergeCandidateRow,
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

interface RowResult {
  ok: boolean
  reason: string
}

export default function AdminBatchMergePage() {
  const { user, authMode, loading: authLoading } = useAuth()

  const isAdmin = useMemo(() => {
    if (authMode === "open") return true
    return roleAtLeast(user?.role, "admin")
  }, [authMode, user?.role])

  const [rows, setRows] = useState<BatchMergeCandidateRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [agentClass, setAgentClass] = useState<string>("")
  const [tier, setTier] = useState<string>("")
  const [fileGlob, setFileGlob] = useState<string>("")
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [rowResults, setRowResults] = useState<Record<string, RowResult>>({})
  const [bulkSummary, setBulkSummary] = useState<{
    succeeded: number
    failed: number
  } | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    // ``rowResults`` / ``bulkSummary`` are intentionally not cleared
    // here — onApprove relies on the post-approve refresh to update
    // the candidate list while keeping the per-row +2 results visible.
    try {
      const res = await listBatchMergeCandidates({
        agent_class: agentClass || undefined,
        tier: tier || undefined,
        file_glob: fileGlob || undefined,
      })
      setRows(res.candidates)
      setSelected((prev) => {
        const stillThere = new Set<string>()
        const live = new Set(res.candidates.map((r) => r.change_id))
        prev.forEach((id) => {
          if (live.has(id)) stillThere.add(id)
        })
        return stillThere
      })
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [agentClass, tier, fileGlob])

  useEffect(() => {
    if (authLoading) return
    if (!isAdmin) {
      setLoading(false)
      return
    }
    void refresh()
  }, [authLoading, isAdmin, refresh])

  const allChecked =
    rows.length > 0 && rows.every((r) => selected.has(r.change_id))
  const someChecked = rows.some((r) => selected.has(r.change_id))

  const toggleAll = useCallback(() => {
    setSelected((prev) => {
      if (prev.size === rows.length && rows.length > 0) return new Set()
      return new Set(rows.map((r) => r.change_id))
    })
  }, [rows])

  const toggleOne = useCallback((changeId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(changeId)) next.delete(changeId)
      else next.add(changeId)
      return next
    })
  }, [])

  const onApprove = useCallback(async () => {
    const ids = Array.from(selected)
    if (ids.length === 0) return
    setBusy(true)
    setRowResults({})
    setBulkSummary(null)
    try {
      const res = await approveBatchMergeCandidates(ids)
      const map: Record<string, RowResult> = {}
      res.results.forEach((r) => {
        map[r.change_id] = { ok: r.ok, reason: r.reason }
      })
      setRowResults(map)
      setBulkSummary({ succeeded: res.succeeded, failed: res.failed })
      // Successful rows fall off the candidate registry server-side;
      // refresh to reflect that without forcing the operator to click.
      await refresh()
    } catch (exc) {
      const detail =
        exc instanceof ApiError
          ? (exc.parsed as { detail?: string } | null)?.detail ?? exc.body
          : exc instanceof Error
            ? exc.message
            : String(exc)
      setError(detail || "batch approve failed")
    } finally {
      setBusy(false)
    }
  }, [selected, refresh])

  if (authLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div
          className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2"
          data-testid="batch-merge-auth-loading"
        >
          <Loader2 size={14} className="animate-spin" />
          Verifying operator session…
        </div>
      </main>
    )
  }

  if (!isAdmin) {
    return (
      <main
        className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)] p-6"
        data-testid="batch-merge-forbidden"
      >
        <div className="max-w-md w-full rounded border border-[var(--destructive)]/40 bg-[var(--card)] p-6 font-mono">
          <div className="flex items-center gap-2 text-[var(--destructive)] mb-2">
            <ShieldAlert size={16} />
            <span className="text-sm font-semibold">403 — admin required</span>
          </div>
          <p className="text-xs text-[var(--muted-foreground)] leading-relaxed mb-4">
            /admin/batch-merge posts the human +2 vote required by
            CLAUDE.md L1. Only role <code>admin</code> or higher can
            confirm AI-Reviewer-tagged candidates.
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
      data-testid="batch-merge-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-center justify-between mb-6">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link
                href="/"
                className="hover:text-[var(--foreground)] inline-flex items-center gap-1"
              >
                <ArrowLeft size={10} /> dashboard
              </Link>
              <ChevronRight size={10} />
              <span>admin</span>
              <ChevronRight size={10} />
              <span className="text-[var(--foreground)]">batch merge</span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <GitMerge size={20} />
              Batch-merge candidates
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              AI Reviewer auto-tagged these patchsets as low-risk after
              passing all 5 gates. Multi-select and confirm to bulk-+2
              — the human +2 is still you, the AI just spot-checks.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="batch-merge-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        <section
          className="mb-4 grid grid-cols-1 md:grid-cols-3 gap-2 text-xs font-mono"
          data-testid="batch-merge-filters"
        >
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted-foreground)]">
              agent_class
            </span>
            <input
              type="text"
              value={agentClass}
              onChange={(e) => setAgentClass(e.target.value)}
              placeholder="e.g. subscription-claude"
              className="rounded border border-[var(--border)] bg-[var(--card)] px-2 py-1"
              data-testid="batch-merge-filter-agent-class"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted-foreground)]">tier</span>
            <input
              type="text"
              value={tier}
              onChange={(e) => setTier(e.target.value)}
              placeholder="S | M | L | X"
              className="rounded border border-[var(--border)] bg-[var(--card)] px-2 py-1"
              data-testid="batch-merge-filter-tier"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted-foreground)]">
              file substring
            </span>
            <input
              type="text"
              value={fileGlob}
              onChange={(e) => setFileGlob(e.target.value)}
              placeholder="docs/ or backend/agents/"
              className="rounded border border-[var(--border)] bg-[var(--card)] px-2 py-1"
              data-testid="batch-merge-filter-file-glob"
            />
          </label>
        </section>

        {error && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)] flex items-center gap-2"
            data-testid="batch-merge-error"
          >
            <CircleAlert size={12} />
            <span>{error}</span>
          </div>
        )}

        {bulkSummary && (
          <div
            className="mb-4 rounded border border-[var(--border)] bg-[var(--card)] p-3 text-xs font-mono flex items-center gap-2"
            data-testid="batch-merge-bulk-summary"
          >
            <ShieldCheck size={12} />
            <span>
              {bulkSummary.succeeded} approved, {bulkSummary.failed} failed.
            </span>
          </div>
        )}

        <div className="rounded border border-[var(--border)] bg-[var(--card)] overflow-hidden">
          <table
            className="w-full text-xs font-mono"
            data-testid="batch-merge-table"
          >
            <thead className="bg-[var(--secondary)]/40">
              <tr>
                <th className="text-left p-2 w-8">
                  <input
                    type="checkbox"
                    aria-label="select all"
                    checked={allChecked}
                    ref={(el) => {
                      if (el) el.indeterminate = !allChecked && someChecked
                    }}
                    onChange={toggleAll}
                    data-testid="batch-merge-select-all"
                  />
                </th>
                <th className="text-left p-2">change</th>
                <th className="text-left p-2">bot</th>
                <th className="text-left p-2">file class</th>
                <th className="text-right p-2">size</th>
                <th className="text-left p-2">tagged</th>
                <th className="text-left p-2">result</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && !loading && (
                <tr>
                  <td
                    colSpan={7}
                    className="p-4 text-center text-[var(--muted-foreground)]"
                    data-testid="batch-merge-empty"
                  >
                    No candidates currently tagged. The AI Reviewer is
                    keeping up with the queue.
                  </td>
                </tr>
              )}
              {rows.map((row) => {
                const result = rowResults[row.change_id]
                return (
                  <tr
                    key={row.change_id}
                    className="border-t border-[var(--border)]"
                    data-testid={`batch-merge-row-${row.change_id}`}
                  >
                    <td className="p-2 align-top">
                      <input
                        type="checkbox"
                        aria-label={`select ${row.change_id}`}
                        checked={selected.has(row.change_id)}
                        onChange={() => toggleOne(row.change_id)}
                        data-testid={`batch-merge-select-${row.change_id}`}
                      />
                    </td>
                    <td className="p-2 align-top">
                      <div className="flex flex-col">
                        <span className="font-semibold text-[var(--foreground)]">
                          {row.subject || row.change_id}
                        </span>
                        <span className="text-[10px] text-[var(--muted-foreground)]">
                          {row.project} · {row.change_id.slice(0, 12)}
                        </span>
                      </div>
                    </td>
                    <td className="p-2 align-top">{row.bot}</td>
                    <td className="p-2 align-top">{row.file_class}</td>
                    <td className="p-2 align-top text-right">
                      +{row.insertions}/-{row.deletions}
                    </td>
                    <td className="p-2 align-top">
                      {formatRelative(row.tagged_at)}
                    </td>
                    <td className="p-2 align-top">
                      {result ? (
                        result.ok ? (
                          <span
                            className="text-[var(--neural-blue)] inline-flex items-center gap-1"
                            data-testid={`batch-merge-result-ok-${row.change_id}`}
                          >
                            <ShieldCheck size={12} /> +2 posted
                          </span>
                        ) : (
                          <span
                            className="text-[var(--destructive)] inline-flex items-center gap-1"
                            data-testid={`batch-merge-result-fail-${row.change_id}`}
                          >
                            <CircleAlert size={12} /> {result.reason || "failed"}
                          </span>
                        )
                      ) : (
                        <span className="text-[var(--muted-foreground)] inline-flex items-center gap-1">
                          <Sparkles size={12} /> {row.ai_summary || "AI-approved"}
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        <div className="mt-4 flex items-center justify-end gap-2">
          <span
            className="text-[10px] font-mono text-[var(--muted-foreground)]"
            data-testid="batch-merge-selected-count"
          >
            {selected.size} selected
          </span>
          <button
            type="button"
            onClick={() => void onApprove()}
            disabled={busy || selected.size === 0}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--neural-blue)] text-[var(--background)] text-xs font-mono disabled:opacity-50"
            data-testid="batch-merge-approve"
          >
            {busy ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <ShieldCheck size={12} />
            )}
            Confirm and +2 selected
          </button>
        </div>
      </div>
    </main>
  )
}

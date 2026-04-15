"use client"

/**
 * RunHistory panel — list of recent workflow runs.
 *
 * Pipeline Timeline tracks NPI phase progression — that's a
 * different concept than a workflow_run, so we don't try to
 * navigate the operator there. Instead, clicking a row expands
 * inline to show the run's steps (fetched on demand from
 * /workflow/runs/{id}). Click again to collapse.
 *
 * Polls every 15 s — recent activity changes shape constantly while
 * a fresh DAG is mid-flight; not bothering with SSE because the
 * operator isn't usually staring at this panel during steady state.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import {
  CheckCircle2, Clock3, History, Loader2, XCircle, AlertCircle,
  Pause, ListTodo, ChevronRight, ChevronDown,
} from "lucide-react"
import {
  listWorkflowRuns, getWorkflowRun,
  type WorkflowRunSummary, type WorkflowStepDetail,
} from "@/lib/api"

const POLL_MS = 15_000

type StatusFilter = "all" | "running" | "completed" | "failed" | "halted"

const STATUS_TONE: Record<string, { color: string; Icon: typeof CheckCircle2 }> = {
  running:   { color: "var(--neural-cyan,#67e8f9)",     Icon: Loader2 },
  completed: { color: "var(--validation-emerald,#10b981)", Icon: CheckCircle2 },
  failed:    { color: "var(--destructive)",              Icon: XCircle },
  halted:    { color: "var(--fui-orange,#f59e0b)",       Icon: Pause },
  pending:   { color: "var(--muted-foreground,#94a3b8)", Icon: Clock3 },
}

function tone(status: string) {
  return STATUS_TONE[status] ?? {
    color: "var(--muted-foreground,#94a3b8)", Icon: AlertCircle,
  }
}

function ageString(ts: number | null): string {
  if (!ts) return "—"
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

function durationString(started: number | null, completed: number | null): string {
  if (!started) return "—"
  const end = completed ?? Date.now() / 1000
  const sec = Math.max(0, Math.floor(end - started))
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
}

export function RunHistoryPanel() {
  const [filter, setFilter] = useState<StatusFilter>("all")
  const [runs, setRuns] = useState<WorkflowRunSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)
  // Inline expansion: which run is open + cached step list per id
  // so a re-collapse + re-expand within a poll cycle doesn't re-fetch.
  const [openId, setOpenId] = useState<string | null>(null)
  const [details, setDetails] = useState<Record<string, {
    steps: WorkflowStepDetail[]
    in_flight: boolean
  } | { error: string } | "loading">>({})

  const refresh = useCallback(async () => {
    try {
      const list = await listWorkflowRuns({
        status: filter === "all" ? undefined : filter,
        limit: 50,
      })
      if (!mountedRef.current) return
      setRuns(list)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [filter])

  useEffect(() => {
    mountedRef.current = true
    void refresh()
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => {
      mountedRef.current = false
      clearInterval(t)
    }
  }, [refresh])

  const toggleRun = useCallback(async (runId: string) => {
    // Toggle off: collapse without losing the cache.
    if (openId === runId) {
      setOpenId(null)
      return
    }
    setOpenId(runId)
    // Already cached → no fetch.
    if (details[runId] && details[runId] !== "loading") return
    setDetails((d) => ({ ...d, [runId]: "loading" }))
    try {
      const detail = await getWorkflowRun(runId)
      if (!mountedRef.current) return
      setDetails((d) => ({
        ...d,
        [runId]: { steps: detail.steps, in_flight: detail.in_flight },
      }))
    } catch (exc) {
      if (!mountedRef.current) return
      setDetails((d) => ({
        ...d, [runId]: { error: exc instanceof Error ? exc.message : String(exc) },
      }))
    }
  }, [openId, details])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Run History"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <History className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            RUN HISTORY
          </h2>
        </div>
        <div role="tablist" aria-label="Status filter" className="flex rounded border border-[var(--border)] overflow-hidden">
          {(["all", "running", "completed", "failed"] as StatusFilter[]).map((s) => (
            <button
              key={s}
              type="button"
              role="tab"
              aria-selected={filter === s}
              onClick={() => setFilter(s)}
              className={`text-[10px] font-mono px-2 py-0.5 ${
                filter === s
                  ? "bg-[var(--neural-cyan,#67e8f9)] text-black"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--muted)]"
              }`}
            >
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </header>

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--destructive)] truncate" title={error}>
          ⚠ {error}
        </div>
      )}

      {runs === null && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground)]">
          Loading…
        </div>
      )}

      {runs?.length === 0 && (
        <div className="px-3 py-6 flex flex-col items-center gap-1 text-center font-mono text-xs text-[var(--muted-foreground)]">
          <ListTodo size={20} aria-hidden />
          No runs yet — submit a DAG to populate this list.
        </div>
      )}

      {runs && runs.length > 0 && (
        <ul className="divide-y divide-[var(--neural-border,rgba(148,163,184,0.2))] max-h-[420px] overflow-y-auto">
          {runs.map((r) => {
            const t = tone(r.status)
            const Icon = t.Icon
            const open = openId === r.id
            const Chevron = open ? ChevronDown : ChevronRight
            return (
              <li key={r.id} data-run-id={r.id} data-status={r.status}>
                <div
                  className="px-3 py-2 flex items-center gap-2 hover:bg-white/5 cursor-pointer"
                  onClick={() => void toggleRun(r.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault()
                      void toggleRun(r.id)
                    }
                  }}
                  role="button"
                  tabIndex={0}
                  aria-expanded={open}
                  aria-label={`run ${r.id} status ${r.status}`}
                >
                  <Chevron size={10} className="text-[var(--muted-foreground)] shrink-0" aria-hidden />
                  <Icon
                    size={14}
                    className={r.status === "running" ? "animate-spin shrink-0" : "shrink-0"}
                    style={{ color: t.color }}
                    aria-hidden
                  />
                  <div className="flex-1 min-w-0 grid grid-cols-[1fr_auto_auto] gap-2 items-center">
                    <span
                      className="font-mono text-[11px] text-[var(--foreground)] truncate"
                      title={r.id}
                    >
                      {r.id}
                    </span>
                    <span
                      className="font-mono text-[10px] uppercase tracking-wider tabular-nums"
                      style={{ color: t.color }}
                    >
                      {r.status}
                    </span>
                    <span
                      className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums"
                      title={`started ${r.started_at}`}
                    >
                      {durationString(r.started_at, r.completed_at)} · {ageString(r.started_at)}
                    </span>
                  </div>
                </div>
                {open && (
                  <RunDetail data={details[r.id]} />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}


// ─── Inline expansion: step list per run ────────────────────────

function RunDetail({
  data,
}: {
  data: { steps: WorkflowStepDetail[]; in_flight: boolean } | { error: string } | "loading" | undefined
}) {
  if (data === "loading" || data === undefined) {
    return (
      <div className="px-7 py-2 font-mono text-[10px] text-[var(--muted-foreground)] flex items-center gap-1">
        <Loader2 size={10} className="animate-spin" /> Loading steps…
      </div>
    )
  }
  if ("error" in data) {
    return (
      <div className="px-7 py-2 font-mono text-[10px] text-[var(--destructive)]">
        ⚠ {data.error}
      </div>
    )
  }
  if (data.steps.length === 0) {
    return (
      <div className="px-7 py-2 font-mono text-[10px] text-[var(--muted-foreground)]">
        No steps recorded yet.
      </div>
    )
  }
  return (
    <ol className="px-7 py-2 space-y-1 bg-white/[0.03]" aria-label="run steps">
      {data.steps.map((s, i) => {
        const failed = !!s.error
        const done = s.is_done
        const dur = durationString(s.started_at, s.completed_at)
        return (
          <li
            key={s.id}
            className="flex items-start gap-2 font-mono text-[10px]"
            data-step-id={s.id}
            data-step-status={failed ? "failed" : done ? "completed" : "pending"}
          >
            <span className="text-[var(--muted-foreground)] tabular-nums shrink-0" style={{ width: 22 }}>
              {String(i + 1).padStart(2, "0")}
            </span>
            <span
              className="font-semibold shrink-0"
              style={{ color: failed ? "var(--destructive)" : done ? "var(--validation-emerald,#10b981)" : "var(--muted-foreground)" }}
            >
              {failed ? "✗" : done ? "✓" : "·"}
            </span>
            <span className="flex-1 min-w-0 text-[var(--foreground)] break-all">
              {s.key}
              {failed && (
                <span className="ml-2 text-[var(--destructive)]">{s.error}</span>
              )}
            </span>
            <span className="text-[var(--muted-foreground)] tabular-nums shrink-0">
              {dur}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

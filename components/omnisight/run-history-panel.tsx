"use client"

/**
 * RunHistory panel — list of recent workflow runs.
 *
 * Pipeline Timeline shows the *current* run's steps; this panel
 * shows what's been run lately, regardless of state. Filter by
 * status, click a row to focus that run in the timeline (uses the
 * same `omnisight:navigate` event the rest of the cross-panel
 * choreography uses).
 *
 * Polls every 15 s — recent activity changes shape constantly while
 * a fresh DAG is mid-flight, but the operator isn't usually staring
 * at this panel during steady state.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import {
  CheckCircle2, Clock3, History, Loader2, XCircle, AlertCircle,
  Pause, ListTodo, ArrowRight,
} from "lucide-react"
import { listWorkflowRuns, type WorkflowRunSummary } from "@/lib/api"

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

  const focusInTimeline = (runId: string) => {
    if (typeof window === "undefined") return
    // Pipeline Timeline panel doesn't yet take a run-id focus (it
    // shows the active run by default). Best we can do today is
    // navigate; a follow-up can extend Timeline to honour a focus
    // hint via a custom event the same way DagCanvas → Form works.
    window.dispatchEvent(new CustomEvent("omnisight:navigate", {
      detail: { panel: "timeline" },
    }))
    window.dispatchEvent(new CustomEvent("omnisight:timeline-focus-run", {
      detail: { runId },
    }))
  }

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
            return (
              <li
                key={r.id}
                className="px-3 py-2 flex items-center gap-2 hover:bg-white/5 cursor-pointer"
                onClick={() => focusInTimeline(r.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault()
                    focusInTimeline(r.id)
                  }
                }}
                role="button"
                tabIndex={0}
                aria-label={`run ${r.id} status ${r.status}`}
                data-run-id={r.id}
                data-status={r.status}
              >
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
                <ArrowRight size={10} className="text-[var(--muted-foreground)] shrink-0" aria-hidden />
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

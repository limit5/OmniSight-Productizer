"use client"

/**
 * RunHistory panel — list of recent workflow runs, optionally grouped
 * under project_run parents when aggregation data is available.
 *
 * B7 (#207): adds project_run aggregation. When project runs exist,
 * the panel shows collapsed parent rows with summary stats; clicking
 * expands to show child workflow_runs. Falls back to flat list when
 * no project runs are loaded.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import {
  CheckCircle2, Clock3, History, Loader2, XCircle, AlertCircle,
  Pause, ListTodo, ChevronRight, ChevronDown, FolderOpen,
  RotateCcw, Ban,
} from "lucide-react"
import {
  getWorkflowRun, listProjectRuns,
  retryWorkflowRun, cancelWorkflowRun,
  type WorkflowRunSummary, type WorkflowStepDetail,
  type ProjectRun,
} from "@/lib/api"
import { useWorkflows } from "@/hooks/use-workflows"

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

export function RunHistoryPanel({ projectId }: { projectId?: string }) {
  const [filter, setFilter] = useState<StatusFilter>("all")
  // Q.3-SUB-1 (#297): workflow_runs state + SSE patch lives in
  // useWorkflows; the panel keeps only the project_run aggregation
  // state here (separate endpoint, unaffected by workflow_updated).
  const {
    runs,
    error: runsError,
    refresh: refreshRuns,
  } = useWorkflows({
    status: filter === "all" ? undefined : filter,
    limit: 50,
    pollMs: POLL_MS,
  })
  const [projectRuns, setProjectRuns] = useState<ProjectRun[] | null>(null)
  const [localError, setError] = useState<string | null>(null)
  const error = localError ?? runsError
  const mountedRef = useRef(true)
  const [openId, setOpenId] = useState<string | null>(null)
  const [openParentId, setOpenParentId] = useState<string | null>(null)
  const [details, setDetails] = useState<Record<string, {
    steps: WorkflowStepDetail[]
    in_flight: boolean
  } | { error: string } | "loading">>({})

  const refresh = useCallback(async () => {
    await refreshRuns()
    if (projectId) {
      try {
        const prs = await listProjectRuns(projectId)
        if (!mountedRef.current) return
        setProjectRuns(prs)
      } catch {
        // Non-fatal: fall back to flat list
      }
    }
  }, [refreshRuns, projectId])

  useEffect(() => {
    mountedRef.current = true
    // useWorkflows owns the flat-list refresh + SSE subscription;
    // we only need to drive project_runs polling here.
    if (!projectId) {
      return () => { mountedRef.current = false }
    }
    void (async () => {
      try {
        const prs = await listProjectRuns(projectId)
        if (!mountedRef.current) return
        setProjectRuns(prs)
      } catch { /* non-fatal */ }
    })()
    const t = setInterval(() => {
      void (async () => {
        try {
          const prs = await listProjectRuns(projectId)
          if (!mountedRef.current) return
          setProjectRuns(prs)
        } catch { /* non-fatal */ }
      })()
    }, POLL_MS)
    return () => {
      mountedRef.current = false
      clearInterval(t)
    }
  }, [projectId])

  const toggleRun = useCallback(async (runId: string) => {
    if (openId === runId) {
      setOpenId(null)
      return
    }
    setOpenId(runId)
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

  const toggleParent = useCallback((parentId: string) => {
    setOpenParentId((prev) => prev === parentId ? null : parentId)
  }, [])

  const [conflictMsg, setConflictMsg] = useState<string | null>(null)
  const [actionBusy, setActionBusy] = useState<string | null>(null)

  const handleRetry = useCallback(async (run: WorkflowRunSummary) => {
    setActionBusy(run.id)
    setConflictMsg(null)
    try {
      await retryWorkflowRun(run.id, run.version)
      void refresh()
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      if (msg.includes("409")) {
        setConflictMsg("另一處已修改，請重新整理 (conflict: resource modified elsewhere)")
      } else {
        setError(msg)
      }
    } finally {
      setActionBusy(null)
    }
  }, [refresh])

  const handleCancel = useCallback(async (run: WorkflowRunSummary) => {
    setActionBusy(run.id)
    setConflictMsg(null)
    try {
      await cancelWorkflowRun(run.id, run.version)
      void refresh()
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      if (msg.includes("409")) {
        setConflictMsg("另一處已修改，請重新整理 (conflict: resource modified elsewhere)")
      } else {
        setError(msg)
      }
    } finally {
      setActionBusy(null)
    }
  }, [refresh])

  const hasProjectRuns = projectRuns && projectRuns.length > 0

  const filteredProjectRuns = hasProjectRuns
    ? projectRuns!.filter((pr) => {
        if (filter === "all") return true
        return pr.children.some((c) => c.status === filter)
      })
    : null

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
      {conflictMsg && (
        <div
          className="px-3 py-1.5 font-mono text-[10px] text-[var(--fui-orange,#f59e0b)] bg-[var(--fui-orange,#f59e0b)]/10 flex items-center justify-between"
          role="alert"
        >
          <span>⚠ {conflictMsg}</span>
          <button
            type="button"
            className="underline ml-2"
            onClick={() => { setConflictMsg(null); void refresh() }}
          >
            重新整理
          </button>
        </div>
      )}

      {runs === null && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground)]">
          Loading…
        </div>
      )}

      {runs?.length === 0 && !hasProjectRuns && (
        <div className="px-3 py-6 flex flex-col items-center gap-1 text-center font-mono text-xs text-[var(--muted-foreground)]">
          <ListTodo size={20} aria-hidden />
          No runs yet — submit a DAG to populate this list.
        </div>
      )}

      {/* Project run aggregation view */}
      {filteredProjectRuns && filteredProjectRuns.length > 0 && (
        <ul className="divide-y divide-[var(--neural-border,rgba(148,163,184,0.2))] max-h-[420px] overflow-y-auto" data-testid="project-runs-list">
          {filteredProjectRuns.map((pr) => {
            const parentOpen = openParentId === pr.id
            const Chevron = parentOpen ? ChevronDown : ChevronRight
            const s = pr.summary
            return (
              <li key={pr.id} data-project-run-id={pr.id}>
                <div
                  className="px-3 py-2 flex items-center gap-2 hover:bg-white/5 cursor-pointer"
                  onClick={() => toggleParent(pr.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault()
                      toggleParent(pr.id)
                    }
                  }}
                  role="button"
                  tabIndex={0}
                  aria-expanded={parentOpen}
                  aria-label={`project run ${pr.label || pr.id}`}
                >
                  <Chevron size={10} className="text-[var(--muted-foreground)] shrink-0" aria-hidden />
                  <FolderOpen size={14} className="text-[var(--neural-cyan,#67e8f9)] shrink-0" aria-hidden />
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <span className="font-mono text-[11px] text-[var(--foreground)] truncate">
                      {pr.label || pr.id}
                    </span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums shrink-0">
                      {ageString(pr.created_at)}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0" data-testid={`summary-${pr.id}`}>
                    <span className="font-mono text-[10px] tabular-nums text-[var(--muted-foreground)]">
                      {s.total}
                    </span>
                    {s.completed > 0 && (
                      <span className="font-mono text-[10px] tabular-nums" style={{ color: "var(--validation-emerald,#10b981)" }}>
                        {s.completed}✓
                      </span>
                    )}
                    {s.failed > 0 && (
                      <span className="font-mono text-[10px] tabular-nums" style={{ color: "var(--destructive)" }}>
                        {s.failed}✗
                      </span>
                    )}
                    {s.running > 0 && (
                      <span className="font-mono text-[10px] tabular-nums" style={{ color: "var(--neural-cyan,#67e8f9)" }}>
                        {s.running}⟳
                      </span>
                    )}
                  </div>
                </div>

                {parentOpen && (
                  <ul className="pl-6 bg-white/[0.02] divide-y divide-[var(--neural-border,rgba(148,163,184,0.1))]" aria-label="child workflow runs">
                    {pr.children
                      .filter((c) => filter === "all" || c.status === filter)
                      .map((c) => {
                        const t = tone(c.status)
                        const Icon = t.Icon
                        const childOpen = openId === c.id
                        const ChildChevron = childOpen ? ChevronDown : ChevronRight
                        return (
                          <li key={c.id} data-run-id={c.id} data-status={c.status}>
                            <div
                              className="px-3 py-1.5 flex items-center gap-2 hover:bg-white/5 cursor-pointer"
                              onClick={() => void toggleRun(c.id)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" || e.key === " ") {
                                  e.preventDefault()
                                  void toggleRun(c.id)
                                }
                              }}
                              role="button"
                              tabIndex={0}
                              aria-expanded={childOpen}
                              aria-label={`run ${c.id} status ${c.status}`}
                            >
                              <ChildChevron size={10} className="text-[var(--muted-foreground)] shrink-0" aria-hidden />
                              <Icon
                                size={14}
                                className={c.status === "running" ? "animate-spin shrink-0" : "shrink-0"}
                                style={{ color: t.color }}
                                aria-hidden
                              />
                              <div className="flex-1 min-w-0 grid grid-cols-[1fr_auto_auto] gap-2 items-center">
                                <span className="font-mono text-[11px] text-[var(--foreground)] truncate" title={c.id}>
                                  {c.id}
                                </span>
                                <span className="font-mono text-[10px] uppercase tracking-wider tabular-nums" style={{ color: t.color }}>
                                  {c.status}
                                </span>
                                <span className="font-mono text-[10px] text-[var(--muted-foreground)] tabular-nums" title={`started ${c.started_at}`}>
                                  {durationString(c.started_at, c.completed_at)} · {ageString(c.started_at)}
                                </span>
                              </div>
                              <RunActions run={c} busy={actionBusy} onRetry={handleRetry} onCancel={handleCancel} />
                            </div>
                            {childOpen && <RunDetail data={details[c.id]} />}
                          </li>
                        )
                      })}
                  </ul>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {/* Flat list fallback (no project runs or ungrouped runs) */}
      {!hasProjectRuns && runs && runs.length > 0 && (
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
                  <RunActions run={r} busy={actionBusy} onRetry={handleRetry} onCancel={handleCancel} />
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


function RunActions({
  run,
  busy,
  onRetry,
  onCancel,
}: {
  run: WorkflowRunSummary
  busy: string | null
  onRetry: (r: WorkflowRunSummary) => void
  onCancel: (r: WorkflowRunSummary) => void
}) {
  const isBusy = busy === run.id
  if (run.status === "running") {
    return (
      <button
        type="button"
        disabled={isBusy}
        className="shrink-0 ml-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-[var(--destructive)] text-[var(--destructive)] hover:bg-[var(--destructive)]/10 disabled:opacity-40"
        onClick={(e) => { e.stopPropagation(); onCancel(run) }}
        aria-label={`cancel run ${run.id}`}
      >
        {isBusy ? <Loader2 size={10} className="animate-spin inline" /> : <Ban size={10} className="inline" />}{" "}CANCEL
      </button>
    )
  }
  if (run.status === "failed" || run.status === "halted") {
    return (
      <button
        type="button"
        disabled={isBusy}
        className="shrink-0 ml-1 px-1.5 py-0.5 rounded text-[10px] font-mono border border-[var(--neural-cyan,#67e8f9)] text-[var(--neural-cyan,#67e8f9)] hover:bg-[var(--neural-cyan,#67e8f9)]/10 disabled:opacity-40"
        onClick={(e) => { e.stopPropagation(); onRetry(run) }}
        aria-label={`retry run ${run.id}`}
      >
        {isBusy ? <Loader2 size={10} className="animate-spin inline" /> : <RotateCcw size={10} className="inline" />}{" "}RETRY
      </button>
    )
  }
  return null
}

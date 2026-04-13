"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { AlertOctagon, AlertTriangle, Check, History, Info, RotateCcw, X, Zap } from "lucide-react"
import {
  type DecisionPayload,
  type DecisionSeverity,
  type SSEEvent,
  approveDecision,
  listDecisions,
  rejectDecision,
  subscribeEvents,
  triggerSweep,
  undoDecision,
} from "@/lib/api"

/**
 * Phase 48C — Decision Dashboard.
 *
 * Two tabs: Pending (actionable) + History (recent resolved). Decisions
 * arrive via SSE (decision_pending / auto_executed / resolved / undone);
 * user can approve/reject pending or undo resolved. A countdown bar
 * shows time remaining before timeout_default kicks in.
 */

const SEVERITY_META: Record<DecisionSeverity, { label: string; color: string; Icon: typeof Info }> = {
  info: { label: "INFO", color: "var(--muted-foreground, #94a3b8)", Icon: Info },
  routine: { label: "ROUTINE", color: "var(--neural-blue, #60a5fa)", Icon: Info },
  risky: { label: "RISKY", color: "#eab308", Icon: AlertTriangle },
  destructive: { label: "DESTRUCTIVE", color: "var(--critical-red, #ef4444)", Icon: AlertOctagon },
}

function secondsLeft(deadline_at: number | null): number | null {
  if (!deadline_at) return null
  return Math.max(0, Math.floor(deadline_at - Date.now() / 1000))
}

export function DecisionDashboard() {
  const [tab, setTab] = useState<"pending" | "history">("pending")
  const [pending, setPending] = useState<DecisionPayload[]>([])
  const [history, setHistory] = useState<DecisionPayload[]>([])
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(Date.now())

  const refresh = useCallback(async () => {
    try {
      const [p, h] = await Promise.all([
        listDecisions("pending", 100),
        listDecisions("history", 50),
      ])
      setPending(p.items)
      setHistory(h.items.slice().reverse())
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const es = subscribeEvents((ev: SSEEvent) => {
      if (
        ev.event === "decision_pending" ||
        ev.event === "decision_auto_executed" ||
        ev.event === "decision_resolved" ||
        ev.event === "decision_undone"
      ) {
        void refresh()
      }
    })
    return () => es.close()
  }, [refresh])

  // Countdown tick
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const doApprove = async (d: DecisionPayload, option_id: string) => {
    setBusy((b) => ({ ...b, [d.id]: true }))
    try {
      await approveDecision(d.id, option_id)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy((b) => ({ ...b, [d.id]: false }))
    }
  }
  const doReject = async (d: DecisionPayload) => {
    setBusy((b) => ({ ...b, [d.id]: true }))
    try {
      await rejectDecision(d.id)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy((b) => ({ ...b, [d.id]: false }))
    }
  }
  const doUndo = async (d: DecisionPayload) => {
    setBusy((b) => ({ ...b, [d.id]: true }))
    try {
      await undoDecision(d.id)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy((b) => ({ ...b, [d.id]: false }))
    }
  }
  const doSweep = async () => {
    try {
      await triggerSweep()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }

  const items = tab === "pending" ? pending : history
  const pendingCount = pending.length

  return (
    <section
      className="holo-glass-simple corner-brackets-full flex flex-col rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Decision Dashboard"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Zap className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            DECISION QUEUE
          </h2>
          {pendingCount > 0 && (
            <span
              className="font-mono text-[10px] px-1.5 py-0.5 rounded-sm bg-[var(--critical-red,#ef4444)] text-white"
              aria-label={`${pendingCount} pending decisions`}
            >
              {pendingCount}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setTab("pending")}
            className={`px-2 py-0.5 font-mono text-[10px] tracking-wider rounded-sm transition-colors ${
              tab === "pending"
                ? "bg-[var(--neural-cyan,#67e8f9)] text-black"
                : "text-[var(--muted-foreground,#94a3b8)] hover:bg-white/5"
            }`}
          >
            PENDING
          </button>
          <button
            onClick={() => setTab("history")}
            className={`px-2 py-0.5 font-mono text-[10px] tracking-wider rounded-sm transition-colors ${
              tab === "history"
                ? "bg-[var(--neural-cyan,#67e8f9)] text-black"
                : "text-[var(--muted-foreground,#94a3b8)] hover:bg-white/5"
            }`}
          >
            <History className="inline w-3 h-3 mr-0.5" />HISTORY
          </button>
          <button
            onClick={() => void doSweep()}
            className="ml-1 px-2 py-0.5 font-mono text-[10px] tracking-wider rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-white hover:bg-white/5"
            title="Trigger timeout sweep (resolves expired pending decisions)"
          >
            SWEEP
          </button>
        </div>
      </header>

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--critical-red,#ef4444)] border-b border-[var(--neural-border,rgba(148,163,184,0.2))]">
          {error}
        </div>
      )}

      <ul className="flex-1 min-h-[120px] max-h-[360px] overflow-y-auto divide-y divide-[var(--neural-border,rgba(148,163,184,0.15))]">
        {items.length === 0 ? (
          <li className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
            {tab === "pending" ? "No pending decisions." : "No history yet."}
          </li>
        ) : (
          items.map((d) => (
            <DecisionRow
              key={d.id}
              d={d}
              now={now}
              busy={!!busy[d.id]}
              onApprove={doApprove}
              onReject={doReject}
              onUndo={doUndo}
            />
          ))
        )}
      </ul>
    </section>
  )
}

function DecisionRow(props: {
  d: DecisionPayload
  now: number
  busy: boolean
  onApprove: (d: DecisionPayload, option_id: string) => void | Promise<void>
  onReject: (d: DecisionPayload) => void | Promise<void>
  onUndo: (d: DecisionPayload) => void | Promise<void>
}) {
  const { d, busy, onApprove, onReject, onUndo } = props
  const meta = SEVERITY_META[d.severity] || SEVERITY_META.routine
  const { Icon } = meta
  const remaining = useMemo(() => secondsLeft(d.deadline_at), [d.deadline_at, props.now])
  const isPending = d.status === "pending"
  const canUndo = d.status === "approved" || d.status === "auto_executed" || d.status === "timeout_default"

  return (
    <li className="px-3 py-2">
      <div className="flex items-start gap-2">
        <Icon className="w-4 h-4 mt-0.5 shrink-0" style={{ color: meta.color }} aria-hidden />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="font-mono text-[9px] px-1 rounded-sm"
              style={{ backgroundColor: `${meta.color}22`, color: meta.color }}
            >
              {meta.label}
            </span>
            <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)]">
              {d.kind}
            </span>
            <span
              className="font-mono text-[9px] uppercase tracking-wider text-[var(--muted-foreground,#94a3b8)]"
              title={d.resolver ? `resolved by ${d.resolver}` : ""}
            >
              {d.status}
            </span>
            {remaining !== null && isPending && (
              <span
                className="font-mono text-[9px] tabular-nums"
                style={{ color: remaining < 10 ? "var(--critical-red,#ef4444)" : "var(--muted-foreground,#94a3b8)" }}
              >
                {remaining}s
              </span>
            )}
          </div>
          <div className="mt-0.5 text-xs font-medium text-[var(--foreground,#e2e8f0)] break-words">
            {d.title}
          </div>
          {d.detail && (
            <div className="mt-0.5 text-[11px] text-[var(--muted-foreground,#94a3b8)] break-words">
              {d.detail}
            </div>
          )}
          {isPending && d.options.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {d.options.map((opt) => (
                <button
                  key={opt.id}
                  disabled={busy}
                  onClick={() => void onApprove(d, opt.id)}
                  className={`px-2 py-0.5 font-mono text-[10px] tracking-wider rounded-sm border transition-colors ${
                    opt.id === d.default_option_id
                      ? "bg-[var(--neural-cyan,#67e8f9)] text-black border-transparent"
                      : "border-[var(--neural-border,rgba(148,163,184,0.35))] text-[var(--foreground,#e2e8f0)] hover:bg-white/5"
                  } ${busy ? "cursor-wait opacity-60" : "cursor-pointer"}`}
                  title={opt.description || ""}
                >
                  <Check className="inline w-3 h-3 mr-0.5" />
                  {opt.label}
                </button>
              ))}
              <button
                disabled={busy}
                onClick={() => void onReject(d)}
                className="px-2 py-0.5 font-mono text-[10px] tracking-wider rounded-sm border border-[var(--critical-red,#ef4444)] text-[var(--critical-red,#ef4444)] hover:bg-[color:var(--critical-red,#ef4444)]/10 transition-colors"
              >
                <X className="inline w-3 h-3 mr-0.5" />REJECT
              </button>
            </div>
          )}
          {!isPending && canUndo && (
            <div className="mt-2">
              <button
                disabled={busy}
                onClick={() => void onUndo(d)}
                className="px-2 py-0.5 font-mono text-[10px] tracking-wider rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-white hover:bg-white/5"
              >
                <RotateCcw className="inline w-3 h-3 mr-0.5" />UNDO
              </button>
            </div>
          )}
        </div>
      </div>
    </li>
  )
}

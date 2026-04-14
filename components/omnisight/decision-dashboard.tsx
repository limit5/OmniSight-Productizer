"use client"

import { useCallback, useEffect, useRef, useState } from "react"
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
 * Phase 48C (post-audit) — Decision Dashboard.
 *
 * Changes from the original implementation:
 *   - Uses shared SSE manager (via subscribeEvents); no dedicated connection.
 *   - Local merge on SSE events instead of a 150-item refetch every tick.
 *   - AbortController cancels in-flight refetches on unmount.
 *   - Inline seconds-left computation — no useMemo with unstable deps.
 *   - Countdown tick owns its own useEffect; unrelated to refresh deps.
 *   - Sweep button tracks its own loading state.
 */

const SEVERITY_META: Record<DecisionSeverity, { label: string; color: string; Icon: typeof Info }> = {
  info: { label: "INFO", color: "var(--muted-foreground, #94a3b8)", Icon: Info },
  routine: { label: "ROUTINE", color: "var(--neural-blue, #60a5fa)", Icon: Info },
  risky: { label: "RISKY", color: "#eab308", Icon: AlertTriangle },
  destructive: { label: "DESTRUCTIVE", color: "var(--critical-red, #ef4444)", Icon: AlertOctagon },
}

const HISTORY_CAP = 50

function sortNewestFirst(items: DecisionPayload[]): DecisionPayload[] {
  return [...items].sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
}

export function DecisionDashboard() {
  const [tab, setTab] = useState<"pending" | "history">("pending")
  const [pending, setPending] = useState<DecisionPayload[]>([])
  const [history, setHistory] = useState<DecisionPayload[]>([])
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [sweeping, setSweeping] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [initialLoaded, setInitialLoaded] = useState(false)
  const [now, setNow] = useState(Date.now())

  const mountedRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)

  const initialLoad = useCallback(async () => {
    // Cancel any prior in-flight before starting a new pass.
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    try {
      const [p, h] = await Promise.all([
        listDecisions("pending", 100),
        listDecisions("history", HISTORY_CAP),
      ])
      if (!mountedRef.current || ac.signal.aborted) return
      setPending(sortNewestFirst(p.items))
      setHistory(sortNewestFirst(h.items))
      setError(null)
      setInitialLoaded(true)
    } catch (exc) {
      if (!mountedRef.current || ac.signal.aborted) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  // SSE local-merge handlers — never refetch for single-item updates.
  const mergePending = useCallback((d: DecisionPayload) => {
    setPending((prev) => {
      const without = prev.filter((x) => x.id !== d.id)
      return sortNewestFirst([d, ...without])
    })
  }, [])
  const markResolved = useCallback((d: DecisionPayload) => {
    setPending((prev) => prev.filter((x) => x.id !== d.id))
    setHistory((prev) => {
      const without = prev.filter((x) => x.id !== d.id)
      return sortNewestFirst([d, ...without]).slice(0, HISTORY_CAP)
    })
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void initialLoad()
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event === "decision_pending") {
        mergePending(ev.data)
      } else if (
        ev.event === "decision_auto_executed" ||
        ev.event === "decision_resolved" ||
        ev.event === "decision_undone"
      ) {
        markResolved(ev.data)
      }
    })
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
      sub.close()
    }
  }, [initialLoad, mergePending, markResolved])

  // Countdown tick — own effect, 1 Hz, independent of refresh or SSE.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const withRowBusy = async (id: string, fn: () => Promise<unknown>) => {
    setBusy((b) => ({ ...b, [id]: true }))
    try {
      await fn()
    } catch (exc) {
      if (mountedRef.current) setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      if (mountedRef.current) setBusy((b) => ({ ...b, [id]: false }))
    }
  }

  const doApprove = (d: DecisionPayload, option_id: string) =>
    withRowBusy(d.id, () => approveDecision(d.id, option_id))
  const doReject = (d: DecisionPayload) => withRowBusy(d.id, () => rejectDecision(d.id))
  const doUndo = (d: DecisionPayload) => withRowBusy(d.id, () => undoDecision(d.id))

  const doSweep = async () => {
    if (sweeping) return
    setSweeping(true)
    setError(null)
    try {
      await triggerSweep()
    } catch (exc) {
      if (mountedRef.current) setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      if (mountedRef.current) setSweeping(false)
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
            disabled={sweeping}
            className={`ml-1 px-2 py-0.5 font-mono text-[10px] tracking-wider rounded-sm ${
              sweeping
                ? "text-[var(--muted-foreground,#64748b)] cursor-wait"
                : "text-[var(--muted-foreground,#94a3b8)] hover:text-white hover:bg-white/5"
            }`}
            title="Trigger timeout sweep (resolves expired pending decisions)"
          >
            {sweeping ? "SWEEP…" : "SWEEP"}
          </button>
        </div>
      </header>

      {error && (
        <div className="px-3 py-1.5 flex items-center justify-between gap-2 font-mono text-[10px] text-[var(--critical-red,#ef4444)] border-b border-[var(--neural-border,rgba(148,163,184,0.2))]">
          <span className="truncate">{error}</span>
          <button
            onClick={() => void initialLoad()}
            className="px-1.5 py-0.5 rounded-sm border border-current hover:bg-current/10"
          >
            RETRY
          </button>
        </div>
      )}

      <ul className="flex-1 min-h-[120px] max-h-[360px] overflow-y-auto divide-y divide-[var(--neural-border,rgba(148,163,184,0.15))]">
        {!initialLoaded ? (
          <li className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
            Loading…
          </li>
        ) : items.length === 0 ? (
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
  const { d, now, busy, onApprove, onReject, onUndo } = props
  const meta = SEVERITY_META[d.severity] || SEVERITY_META.routine
  const { Icon } = meta
  // Inline — cheap pure function, no memo needed.
  const remaining =
    d.deadline_at == null ? null : Math.max(0, Math.floor(d.deadline_at - now / 1000))
  const isPending = d.status === "pending"
  const canUndo =
    d.status === "approved" || d.status === "auto_executed" || d.status === "timeout_default"

  // Phase 50D: deep-link highlight. `/?decision=<id>` scrolls the matching
  // row into view and ring-pulses it briefly.
  const rowRef = useRef<HTMLLIElement | null>(null)
  const [focusRing, setFocusRing] = useState(false)
  useEffect(() => {
    if (typeof window === "undefined") return
    const target = new URLSearchParams(window.location.search).get("decision")
    if (target !== d.id) return
    rowRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })
    setFocusRing(true)
    const t = setTimeout(() => setFocusRing(false), 3500)
    return () => clearTimeout(t)
  }, [d.id])

  return (
    <li
      ref={rowRef}
      data-testid={`decision-row-${d.id}`}
      className={`px-3 py-2 transition-shadow ${focusRing ? "ring-2 ring-[var(--neural-cyan,#67e8f9)] rounded-sm" : ""}`}
    >
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

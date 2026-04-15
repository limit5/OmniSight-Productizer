"use client"

/**
 * Phase 50C — Notification Toast.
 *
 * Overlay-style, short-lived counterpart to the persistent
 * NotificationCenter. Shows a toast for every incoming `decision_pending`
 * whose severity is risky or destructive, with approve / reject buttons
 * inline and a countdown bar until the decision's deadline_at.
 *
 * Keyboard: A approve default · R reject · Esc dismiss.
 * Multiple toasts stack bottom-up; oldest closest to the content.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { AlertOctagon, AlertTriangle, Check, X } from "lucide-react"
import {
  type DecisionPayload,
  type DecisionSeverity,
  type SSEEvent,
  approveDecision,
  rejectDecision,
  subscribeEvents,
} from "@/lib/api"

const TRIGGER_SEVERITIES: ReadonlySet<DecisionSeverity> = new Set(["risky", "destructive"])
const MAX_TOASTS = 3
// Fallback auto-dismiss if the decision has no deadline_at.
const DEFAULT_TIMEOUT_MS = 30_000
const TICK_MS = 250

interface ToastItem {
  decision: DecisionPayload
  createdAt: number   // ms
  deadlineAt: number  // ms
}

function severityStyle(sev: DecisionSeverity) {
  if (sev === "destructive") {
    return {
      color: "var(--critical-red,#ef4444)",
      Icon: AlertOctagon,
      label: "DESTRUCTIVE",
    }
  }
  return {
    color: "var(--fui-orange,#f59e0b)",
    Icon: AlertTriangle,
    label: "RISKY",
  }
}

export function ToastCenter() {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  // B3: count of high-severity pending decisions that arrived while the
  // stack was already full at MAX_TOASTS. Surfaced as a "+N more" chip
  // so operators notice they missed one.
  const [overflow, setOverflow] = useState(0)
  const [now, setNow] = useState<number>(() => Date.now())
  const focusedRef = useRef<string | null>(null)

  const dismiss = useCallback((id: string) => {
    setToasts((cur) => {
      const next = cur.filter((t) => t.decision.id !== id)
      if (next.length === 0) setOverflow(0)
      return next
    })
  }, [])

  const handleApprove = useCallback(async (t: ToastItem) => {
    const opt = t.decision.default_option_id || t.decision.options[0]?.id
    if (!opt) return
    dismiss(t.decision.id)
    try { await approveDecision(t.decision.id, opt) } catch { /* visual only */ }
  }, [dismiss])

  const handleReject = useCallback(async (t: ToastItem) => {
    dismiss(t.decision.id)
    try { await rejectDecision(t.decision.id) } catch { /* visual only */ }
  }, [dismiss])

  // SSE subscription — adds a toast for high-severity pending decisions
  // and clears one when its corresponding decision resolves elsewhere.
  useEffect(() => {
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event === "decision_pending") {
        const d = ev.data as DecisionPayload
        if (!TRIGGER_SEVERITIES.has(d.severity)) return
        setToasts((cur) => {
          if (cur.some((t) => t.decision.id === d.id)) return cur  // dedupe
          // deadline_at is unix seconds. Defend against NaN / negative /
          // ms-by-mistake payloads that would otherwise NaN the countdown.
          const now = Date.now()
          const raw = typeof d.deadline_at === "number" && Number.isFinite(d.deadline_at)
            ? d.deadline_at
            : 0
          // Heuristic: if value looks like milliseconds (>1e12), treat as ms.
          const deadlineMs = raw > 1e12 ? raw : raw * 1000
          const item: ToastItem = {
            decision: d,
            createdAt: now,
            deadlineAt: deadlineMs > now ? deadlineMs : now + DEFAULT_TIMEOUT_MS,
          }
          // Keep newest-first but cap at MAX_TOASTS. If capacity was
          // already full, surface the dropped count as overflow chip.
          if (cur.length >= MAX_TOASTS) {
            setOverflow((n) => n + 1)
          }
          return [item, ...cur].slice(0, MAX_TOASTS)
        })
      } else if (ev.event === "decision_resolved" || ev.event === "decision_auto_executed") {
        const id = (ev.data as { id: string }).id
        setToasts((cur) => cur.filter((t) => t.decision.id !== id))
      }
    })
    return () => sub.close()
  }, [])

  // Tick for countdown + auto-dismiss on deadline passage.
  // Suspend while the tab is hidden — no point burning CPU/battery on a
  // timer the user cannot see, and the countdown catches up on resume.
  useEffect(() => {
    if (toasts.length === 0) return
    let timer: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (timer) return
      timer = setInterval(() => {
        const n = Date.now()
        setNow(n)
        setToasts((cur) => cur.filter((x) => x.deadlineAt > n))
      }, TICK_MS)
    }
    const stop = () => { if (timer) { clearInterval(timer); timer = null } }
    const onVis = () => (document.hidden ? stop() : start())
    if (!document.hidden) start()
    document.addEventListener("visibilitychange", onVis)
    return () => { stop(); document.removeEventListener("visibilitychange", onVis) }
  }, [toasts.length])

  // Keyboard: A / R / Esc. Only the focused (newest) toast receives them,
  // matching the usual toast UX — users can Tab through otherwise.
  useEffect(() => {
    if (toasts.length === 0) return
    const onKey = (e: KeyboardEvent) => {
      // Ignore when the user is typing in an input/textarea.
      const tag = (e.target as HTMLElement | null)?.tagName
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return
      const focused = focusedRef.current
      const target = toasts.find((t) => t.decision.id === focused) || toasts[0]
      if (!target) return
      if (e.key === "a" || e.key === "A") {
        e.preventDefault(); void handleApprove(target)
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault(); void handleReject(target)
      } else if (e.key === "Escape") {
        e.preventDefault(); dismiss(target.decision.id)
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [toasts, handleApprove, handleReject, dismiss])

  if (toasts.length === 0) return null

  return (
    <div
      aria-live="assertive"
      aria-atomic="true"
      aria-label="decision toasts"
      className="fixed bottom-4 right-4 z-[60] flex flex-col-reverse gap-2 w-[min(360px,calc(100vw-2rem))] pointer-events-none"
    >
      {overflow > 0 && (
        <div
          data-testid="toast-overflow-chip"
          className="pointer-events-auto self-end px-2 py-1 rounded-sm font-mono text-[10px] tracking-wider bg-[var(--critical-red,#ef4444)] text-white shadow-lg"
          role="status"
          aria-label={`${overflow} additional decisions awaiting review`}
        >
          +{overflow} MORE PENDING
        </div>
      )}
      {/* eslint-disable-next-line react-hooks/refs -- focusedRef read during render for non-critical visual hint; converting to state would add re-renders on every hover */}
      {toasts.map((t) => {
        const total = t.deadlineAt - t.createdAt
        const remaining = Math.max(0, t.deadlineAt - now)
        const pct = total > 0 ? Math.max(0, Math.min(100, (remaining / total) * 100)) : 0
        const s = severityStyle(t.decision.severity)
        const { Icon } = s
        const isFocused = focusedRef.current === t.decision.id
        return (
          <div
            key={t.decision.id}
            data-testid={`toast-${t.decision.id}`}
            onMouseEnter={() => { focusedRef.current = t.decision.id }}
            onFocusCapture={() => { focusedRef.current = t.decision.id }}
            role="alert"
            className="pointer-events-auto holo-glass-simple corner-brackets-full rounded-sm border backdrop-blur-sm"
            style={{
              borderColor: s.color,
              boxShadow: `0 8px 28px -10px ${s.color}, 0 0 0 1px ${s.color}, inset 0 0 28px -18px ${s.color}`,
              transform: isFocused ? "translateY(-1px)" : undefined,
              transition: "transform 120ms ease-out",
            }}
          >
            <div className="flex items-start gap-2 p-2.5 pb-1.5">
              <Icon className="w-4 h-4 shrink-0 mt-0.5" style={{ color: s.color }} aria-hidden />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span
                    className="font-mono text-[9px] tracking-[0.25em] font-bold"
                    style={{ color: s.color }}
                  >
                    {s.label}
                  </span>
                  <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] truncate">
                    {t.decision.kind}
                  </span>
                </div>
                <div className="font-mono font-bold text-[12px] tracking-[0.04em] leading-tight text-[var(--foreground,#e2e8f0)] break-words">
                  {t.decision.title}
                </div>
                {t.decision.detail && (
                  <div className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] leading-tight mt-0.5 line-clamp-2">
                    {t.decision.detail}
                  </div>
                )}
              </div>
              <button
                onClick={() => dismiss(t.decision.id)}
                aria-label="dismiss"
                className="p-0.5 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)] hover:bg-white/5 shrink-0"
              >
                <X className="w-3.5 h-3.5" aria-hidden />
              </button>
            </div>

            <div className="flex items-center gap-1.5 px-2.5 pb-2">
              <button
                onClick={() => void handleApprove(t)}
                className="flex items-center gap-1 font-mono text-[10px] tracking-wider px-2 py-1 rounded-sm border border-[var(--validation-emerald,#10b981)] text-[var(--validation-emerald,#10b981)] hover:bg-[var(--validation-emerald,#10b981)]/10"
                aria-label="approve default"
              >
                <Check className="w-3 h-3" aria-hidden /> APPROVE {t.decision.default_option_id && `· ${t.decision.default_option_id}`}
              </button>
              <button
                onClick={() => void handleReject(t)}
                className="flex items-center gap-1 font-mono text-[10px] tracking-wider px-2 py-1 rounded-sm border border-[var(--critical-red,#ef4444)] text-[var(--critical-red,#ef4444)] hover:bg-[var(--critical-red,#ef4444)]/10"
              >
                <X className="w-3 h-3" aria-hidden /> REJECT
              </button>
              <span
                className="ml-auto font-mono text-[11px] tabular-nums font-semibold"
                style={{
                  color: remaining < 10000 ? "var(--critical-red,#ef4444)" : "var(--muted-foreground,#94a3b8)",
                  animation: remaining < 10000 ? "toast-urgent-pulse 1s ease-in-out infinite" : undefined,
                }}
                aria-label={`${Math.ceil(remaining / 1000)} seconds remaining`}
              >
                {Math.ceil(remaining / 1000)}s
              </span>
              <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] tabular-nums">
                A·R·Esc
              </span>
            </div>

            {/* Countdown bar */}
            <div
              aria-hidden
              className="h-[3px] w-full bg-white/5"
              data-testid={`toast-bar-${t.decision.id}`}
            >
              <div
                className="h-full transition-[width]"
                style={{
                  width: `${pct}%`,
                  background: remaining < 10000 ? "var(--critical-red,#ef4444)" : s.color,
                }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

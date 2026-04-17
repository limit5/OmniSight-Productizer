"use client"

/**
 * R0 (#306) — PEP Live Feed Panel.
 *
 * Real-time display of every Policy Enforcement Point decision flowing
 * through the tool_executor. Subscribes to the `pep.decision` SSE
 * channel + hydrates on mount via `GET /pep/live`.
 *
 * Each row carries:
 *   timestamp · agent · tool · command (truncated) · decision badge.
 * HELD rows expand inline with Approve / Reject buttons; the click
 * piggy-backs on the existing `/decisions/{id}/approve|reject` endpoint
 * because every HELD PEP event has a linked decision_engine `decision_id`.
 *
 * Filters (agent / decision / tool) and header stats are computed
 * client-side from the in-memory ring so they stay instant.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Check, Shield, ShieldAlert, ShieldCheck, ShieldX,
  ChevronDown, ChevronRight, RefreshCw, X,
} from "lucide-react"
import {
  type PepDecisionEvent, type PepAction, type PepStats,
  type PepBreakerStatus,
  getPepLive, approveDecision, rejectDecision,
  subscribeEvents, type SSEEvent,
} from "@/lib/api"

const MAX_RING = 300

const ACTION_META: Record<PepAction, { label: string; color: string; Icon: typeof Shield }> = {
  auto_allow: { label: "AUTO", color: "var(--validation-emerald, #10b981)", Icon: ShieldCheck },
  hold: { label: "HELD", color: "var(--fui-orange, #f59e0b)", Icon: ShieldAlert },
  deny: { label: "DENY", color: "var(--critical-red, #ef4444)", Icon: ShieldX },
}

function tsLabel(ts: number): string {
  if (!ts) return "—"
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  })
}

function truncate(s: string, n = 80): string {
  if (!s) return ""
  return s.length > n ? s.slice(0, n) + "…" : s
}

type DecisionFilter = "all" | PepAction

export function PepLiveFeed() {
  const [items, setItems] = useState<PepDecisionEvent[]>([])
  const [stats, setStats] = useState<PepStats>({ auto_allowed: 0, held: 0, denied: 0, total: 0 })
  const [breaker, setBreaker] = useState<PepBreakerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [agentFilter, setAgentFilter] = useState<string>("")
  const [toolFilter, setToolFilter] = useState<string>("")
  const [decisionFilter, setDecisionFilter] = useState<DecisionFilter>("all")
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState<Record<string, boolean>>({})

  const initialLoad = useCallback(async () => {
    setLoading(true)
    try {
      const snap = await getPepLive(MAX_RING)
      setItems(snap.recent)
      setStats(snap.stats)
      setBreaker(snap.breaker)
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void initialLoad()
  }, [initialLoad])

  // SSE — prepend new decisions, dedupe on id (same id may appear twice
  // for a HELD row that later resolves to approve/deny).
  useEffect(() => {
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event !== "pep.decision") return
      const d = ev.data as PepDecisionEvent
      setItems((prev) => {
        const without = prev.filter((x) => x.id !== d.id)
        return [d, ...without].slice(0, MAX_RING)
      })
      setStats((s) => {
        const next = { ...s }
        next.total += 1
        if (d.action === "auto_allow") next.auto_allowed += 1
        else if (d.action === "hold") next.held += 1
        else next.denied += 1
        return next
      })
    })
    return () => sub.close()
  }, [])

  // Auto-refresh breaker once per 10s (fallback — no dedicated SSE yet)
  useEffect(() => {
    const iv = setInterval(async () => {
      try {
        const snap = await getPepLive(1)
        setBreaker(snap.breaker)
      } catch { /* best-effort */ }
    }, 10_000)
    return () => clearInterval(iv)
  }, [])

  const agentOptions = useMemo(() => {
    const set = new Set<string>()
    for (const d of items) if (d.agent_id) set.add(d.agent_id)
    return Array.from(set).sort()
  }, [items])

  const toolOptions = useMemo(() => {
    const set = new Set<string>()
    for (const d of items) if (d.tool) set.add(d.tool)
    return Array.from(set).sort()
  }, [items])

  const filtered = useMemo(() => {
    return items.filter((d) => {
      if (decisionFilter !== "all" && d.action !== decisionFilter) return false
      if (agentFilter && d.agent_id !== agentFilter) return false
      if (toolFilter && d.tool !== toolFilter) return false
      return true
    })
  }, [items, agentFilter, toolFilter, decisionFilter])

  const handleApprove = useCallback(async (d: PepDecisionEvent) => {
    if (!d.decision_id) return
    setBusy((b) => ({ ...b, [d.id]: true }))
    try {
      await approveDecision(d.decision_id, "approve")
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy((b) => ({ ...b, [d.id]: false }))
    }
  }, [])

  const handleReject = useCallback(async (d: PepDecisionEvent) => {
    if (!d.decision_id) return
    setBusy((b) => ({ ...b, [d.id]: true }))
    try {
      await rejectDecision(d.decision_id)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy((b) => ({ ...b, [d.id]: false }))
    }
  }, [])

  return (
    <section
      className="holo-glass-simple corner-brackets-full flex flex-col rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="PEP Live Feed"
      data-testid="pep-live-feed"
    >
      {/* Header */}
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Shield className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            PEP LIVE FEED
          </h2>
          {breaker?.open && (
            <span
              className="font-mono text-[10px] px-1.5 py-0.5 rounded-sm bg-[var(--critical-red,#ef4444)] text-white tabular-nums"
              title={`Breaker open (${breaker.cooldown_remaining}s cooldown) — ${breaker.last_reason}`}
            >
              BREAKER OPEN · {breaker.cooldown_remaining}s
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 font-mono text-[10px]">
          <span className="text-[var(--validation-emerald,#10b981)] tabular-nums">
            {stats.auto_allowed} auto
          </span>
          <span className="text-[var(--fui-orange,#f59e0b)] tabular-nums">
            {stats.held} held
          </span>
          <span className="text-[var(--critical-red,#ef4444)] tabular-nums">
            {stats.denied} deny
          </span>
          <button
            onClick={() => void initialLoad()}
            aria-label="refresh"
            className="p-1 rounded-sm text-[var(--muted-foreground,#94a3b8)] hover:text-white hover:bg-white/5"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} aria-hidden />
          </button>
        </div>
      </header>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-1.5 px-3 py-1.5 border-b border-[var(--neural-border,rgba(148,163,184,0.2))] font-mono text-[10px]">
        {(["all", "auto_allow", "hold", "deny"] as DecisionFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => setDecisionFilter(f)}
            className={`px-2 py-0.5 rounded-sm tracking-wider transition-colors ${
              decisionFilter === f
                ? "bg-[var(--neural-cyan,#67e8f9)] text-black"
                : "text-[var(--muted-foreground,#94a3b8)] hover:bg-white/5"
            }`}
            data-testid={`pep-filter-${f}`}
          >
            {f === "all" ? "ALL" : ACTION_META[f as PepAction].label}
          </button>
        ))}
        <span className="mx-1 text-[var(--muted-foreground,#64748b)]">·</span>
        <select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="bg-black/20 border border-[var(--neural-border)] rounded-sm px-1 py-0.5 max-w-[160px]"
          aria-label="filter by agent"
        >
          <option value="">all agents</option>
          {agentOptions.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <select
          value={toolFilter}
          onChange={(e) => setToolFilter(e.target.value)}
          className="bg-black/20 border border-[var(--neural-border)] rounded-sm px-1 py-0.5 max-w-[160px]"
          aria-label="filter by tool"
        >
          <option value="">all tools</option>
          {toolOptions.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--critical-red,#ef4444)] border-b border-[var(--neural-border,rgba(148,163,184,0.2))]">
          {error}
        </div>
      )}

      {/* Rows */}
      <ul className="flex-1 min-h-[120px] max-h-[420px] overflow-y-auto divide-y divide-[var(--neural-border,rgba(148,163,184,0.15))]">
        {filtered.length === 0 && !loading && (
          <li className="px-3 py-8 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
            NO DECISIONS YET
          </li>
        )}
        {filtered.map((d) => {
          const meta = ACTION_META[d.action]
          const Icon = meta.Icon
          const isExpanded = !!expanded[d.id]
          return (
            <li
              key={d.id}
              className="px-3 py-1.5 hover:bg-white/[0.02]"
              data-testid={`pep-row-${d.id}`}
              data-action={d.action}
            >
              <button
                className="w-full flex items-center gap-2 text-left"
                onClick={() => setExpanded((e) => ({ ...e, [d.id]: !isExpanded }))}
              >
                {isExpanded
                  ? <ChevronDown className="w-3 h-3 shrink-0 text-[var(--muted-foreground,#94a3b8)]" aria-hidden />
                  : <ChevronRight className="w-3 h-3 shrink-0 text-[var(--muted-foreground,#94a3b8)]" aria-hidden />}
                <span className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] tabular-nums shrink-0">
                  {tsLabel(d.ts)}
                </span>
                <span className="font-mono text-[10px] tracking-wider shrink-0" style={{ color: "var(--neural-cyan,#67e8f9)" }}>
                  {d.agent_id || "—"}
                </span>
                <span className="font-mono text-[11px] font-bold text-[var(--foreground,#e2e8f0)] shrink-0">
                  {d.tool}
                </span>
                <span className="font-mono text-[10px] text-[var(--muted-foreground,#64748b)] truncate flex-1 min-w-0">
                  {truncate(d.command, 100)}
                </span>
                <span
                  className="font-mono text-[9px] px-1.5 py-0.5 rounded-sm shrink-0"
                  style={{
                    background: `${meta.color}22`,
                    color: meta.color,
                    border: `1px solid ${meta.color}55`,
                  }}
                  data-testid={`pep-badge-${d.action}`}
                >
                  <Icon className="inline w-3 h-3 mr-0.5" aria-hidden />
                  {meta.label}
                </span>
              </button>

              {isExpanded && (
                <div className="mt-1.5 ml-5 p-2 rounded-sm bg-black/30 font-mono text-[10px] space-y-1">
                  <div>
                    <span className="text-[var(--muted-foreground,#94a3b8)]">Command: </span>
                    <span className="text-[var(--foreground,#e2e8f0)] break-all">{d.command || "(no command)"}</span>
                  </div>
                  <div className="flex gap-3">
                    <span>
                      <span className="text-[var(--muted-foreground,#94a3b8)]">Tier: </span>
                      <span>{d.tier}</span>
                    </span>
                    <span>
                      <span className="text-[var(--muted-foreground,#94a3b8)]">Impact: </span>
                      <span
                        className="uppercase tracking-wider"
                        style={{ color: d.impact_scope === "destructive" || d.impact_scope === "prod" ? "var(--critical-red,#ef4444)" : "var(--muted-foreground,#94a3b8)" }}
                      >
                        {d.impact_scope || "—"}
                      </span>
                    </span>
                    <span>
                      <span className="text-[var(--muted-foreground,#94a3b8)]">Rule: </span>
                      <span>{d.rule || "—"}</span>
                    </span>
                  </div>
                  {d.reason && (
                    <div>
                      <span className="text-[var(--muted-foreground,#94a3b8)]">Reason: </span>
                      <span>{d.reason}</span>
                    </div>
                  )}
                  {d.degraded && (
                    <div className="text-[var(--critical-red,#ef4444)]">
                      ⚠ Degraded: PEP circuit open — failed closed.
                    </div>
                  )}
                  {d.action === "hold" && d.decision_id && (
                    <div className="flex items-center gap-2 pt-1.5">
                      <button
                        onClick={(e) => { e.stopPropagation(); void handleApprove(d) }}
                        disabled={!!busy[d.id]}
                        className="flex items-center gap-1 px-2 py-1 rounded-sm border border-[var(--validation-emerald,#10b981)] text-[var(--validation-emerald,#10b981)] hover:bg-[var(--validation-emerald,#10b981)]/10 disabled:opacity-50"
                        data-testid={`pep-approve-${d.id}`}
                      >
                        <Check className="w-3 h-3" aria-hidden /> APPROVE
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); void handleReject(d) }}
                        disabled={!!busy[d.id]}
                        className="flex items-center gap-1 px-2 py-1 rounded-sm border border-[var(--critical-red,#ef4444)] text-[var(--critical-red,#ef4444)] hover:bg-[var(--critical-red,#ef4444)]/10 disabled:opacity-50"
                        data-testid={`pep-reject-${d.id}`}
                      >
                        <X className="w-3 h-3" aria-hidden /> REJECT
                      </button>
                      <span className="text-[var(--muted-foreground,#64748b)] text-[9px] ml-1">
                        decision_id: {d.decision_id.slice(0, 14)}…
                      </span>
                    </div>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

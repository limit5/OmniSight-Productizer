"use client"

/**
 * Phase 60 — Project Forecast Panel (v0 prototype).
 *
 * 6 KPI cards (TASKS / AGENTS / HOURS / TOKENS / USD / CONFIDENCE)
 * plus a foldable breakdown showing per-phase task counts and
 * Profile sensitivity. Reads `/api/v1/system/forecast` (5min cache;
 * POST /forecast/recompute to bust).
 *
 * Like every other dashboard pill, every dynamic-width cell is in a
 * fixed-dimension box (Phase 50-Layout rules).
 */

import { useCallback, useEffect, useState } from "react"
import {
  ChevronDown, ChevronUp, ListChecks, Users, Clock3, Cpu, DollarSign,
  Gauge, RefreshCw, BarChart3,
} from "lucide-react"
import { PanelHelp } from "@/components/omnisight/panel-help"

interface Forecast {
  project_name: string
  target_platform: string
  project_track: string
  tasks: { total: number; by_phase: Record<string, number>; by_track: string }
  agents: { total: number; by_type: string[] }
  duration: { total_hours: number; optimistic_hours: number; pessimistic_hours: number }
  tokens: { total: number; by_tier: Record<string, number> }
  cost: { total_usd: number; provider: string; by_tier_usd: Record<string, number> }
  confidence: number
  method: string
  profile_sensitivity: Array<{ profile: string; hours: number; multiplier: number }>
  generated_at: number
}

const COMPACT = (n: number, suffix = ""): string => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M${suffix}`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k${suffix}`
  return `${n}${suffix}`
}

export function ForecastPanel() {
  const [data, setData] = useState<Forecast | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState(false)

  const fetchForecast = useCallback(async (recompute = false) => {
    setBusy(true)
    setError(null)
    try {
      const path = recompute ? "/api/v1/system/forecast/recompute" : "/api/v1/system/forecast"
      const res = await fetch(path, { method: recompute ? "POST" : "GET", cache: "no-store" })
      if (!res.ok) throw new Error(`API ${res.status}`)
      setData(await res.json())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { void fetchForecast(false) }, [fetchForecast])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Project Forecast"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2 min-w-0">
          <BarChart3 className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)] shrink-0" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)] truncate">
            FORECAST
          </h2>
          <PanelHelp doc="panels-overview" />
          {data && (
            <span
              className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] truncate"
              style={{ maxWidth: 200 }}
              title={`${data.project_name} · track=${data.project_track} · target=${data.target_platform}`}
              aria-label={`Project ${data.project_name}, track ${data.project_track}, target ${data.target_platform}`}
            >
              {data.project_name}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={() => void fetchForecast(true)}
          disabled={busy}
          className="flex items-center gap-1 px-2 py-0.5 rounded-sm font-mono text-[10px] tracking-wider border border-[var(--neural-cyan,#67e8f9)]/40 text-[var(--neural-cyan,#67e8f9)] hover:bg-[var(--neural-cyan,#67e8f9)]/10 disabled:opacity-40 shrink-0"
          aria-label="recompute forecast"
        >
          <RefreshCw className={`w-3 h-3 ${busy ? "animate-spin" : ""}`} aria-hidden />
          RECOMPUTE
        </button>
      </header>

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--critical-red,#ef4444)] truncate" title={error}>
          ⚠ {error}
        </div>
      )}

      {!data && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
          Loading forecast…
        </div>
      )}

      {data && (
        <>
          {/* 6 KPI cards row */}
          <div className="grid grid-cols-3 lg:grid-cols-6 gap-2 px-3 py-3">
            <KpiCard icon={ListChecks} label="TASKS"      value={String(data.tasks.total)}                   sub={data.tasks.by_track} />
            <KpiCard icon={Users}      label="AGENTS"     value={String(data.agents.total)}                  sub={`${data.agents.by_type.length} roles`} />
            <KpiCard icon={Clock3}     label="HOURS"      value={`${data.duration.total_hours}h`}            sub={`${data.duration.optimistic_hours}–${data.duration.pessimistic_hours}h`} />
            <KpiCard icon={Cpu}        label="TOKENS"     value={COMPACT(data.tokens.total)}                 sub={`prem/def/bud`} />
            <KpiCard icon={DollarSign} label="USD"        value={`$${data.cost.total_usd.toFixed(2)}`}       sub={data.cost.provider} />
            <KpiCard icon={Gauge}      label="CONFIDENCE" value={`${Math.round(data.confidence * 100)}%`}    sub={data.method} />
          </div>

          {/* Toggle */}
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="w-full flex items-center justify-center gap-1 px-3 py-1 font-mono text-[10px] tracking-wider text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--neural-cyan,#67e8f9)] border-t border-[var(--neural-border,rgba(148,163,184,0.35))]"
          >
            {expanded ? <ChevronUp className="w-3 h-3" aria-hidden /> : <ChevronDown className="w-3 h-3" aria-hidden />}
            {expanded ? "HIDE BREAKDOWN" : "SHOW BREAKDOWN"}
          </button>

          {expanded && (
            <div className="px-3 py-3 grid grid-cols-1 lg:grid-cols-2 gap-3 border-t border-[var(--neural-border,rgba(148,163,184,0.35))]">
              {/* Phase breakdown */}
              <div>
                <div className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground,#94a3b8)] mb-1.5">
                  TASKS BY NPI PHASE
                </div>
                <ul className="space-y-1 font-mono text-[11px]">
                  {Object.entries(data.tasks.by_phase).map(([phase, n]) => {
                    const pct = data.tasks.total > 0 ? (n / data.tasks.total) * 100 : 0
                    return (
                      <li key={phase} className="flex items-center gap-2">
                        <span
                          className="text-[var(--muted-foreground,#94a3b8)] uppercase tracking-wider"
                          style={{ width: 80 }}
                        >
                          {phase}
                        </span>
                        <div className="flex-1 h-2 bg-white/5 rounded-sm overflow-hidden">
                          <div
                            className="h-full bg-[var(--neural-cyan,#67e8f9)]"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <span
                          className="tabular-nums text-right text-[var(--foreground,#e2e8f0)]"
                          style={{ width: 28 }}
                        >
                          {n}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              </div>

              {/* Profile sensitivity */}
              <div>
                <div className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground,#94a3b8)] mb-1.5">
                  PROFILE × HOURS
                </div>
                <ul className="space-y-1 font-mono text-[11px]">
                  {data.profile_sensitivity.map((s) => {
                    const max = Math.max(...data.profile_sensitivity.map((p) => p.hours))
                    const pct = max > 0 ? (s.hours / max) * 100 : 0
                    const color =
                      s.profile === "BALANCED" ? "var(--neural-cyan,#67e8f9)"
                      : s.profile === "AUTONOMOUS" ? "var(--validation-emerald,#10b981)"
                      : s.profile === "STRICT" ? "var(--fui-orange,#f59e0b)"
                      : "var(--critical-red,#ef4444)"
                    return (
                      <li key={s.profile} className="flex items-center gap-2">
                        <span
                          className="uppercase tracking-wider"
                          style={{ width: 90, color }}
                        >
                          {s.profile}
                        </span>
                        <div className="flex-1 h-2 bg-white/5 rounded-sm overflow-hidden">
                          <div className="h-full" style={{ width: `${pct}%`, background: color }} />
                        </div>
                        <span
                          className="tabular-nums text-right text-[var(--foreground,#e2e8f0)]"
                          style={{ width: 50 }}
                        >
                          {s.hours}h
                        </span>
                      </li>
                    )
                  })}
                </ul>
              </div>

              {/* Cost per tier */}
              <div className="lg:col-span-2">
                <div className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground,#94a3b8)] mb-1.5">
                  COST BY MODEL TIER ({data.cost.provider})
                </div>
                <ul className="space-y-1 font-mono text-[11px]">
                  {Object.entries(data.cost.by_tier_usd).map(([tier, usd]) => (
                    <li key={tier} className="flex items-center gap-2">
                      <span className="uppercase tracking-wider text-[var(--muted-foreground,#94a3b8)]" style={{ width: 90 }}>
                        {tier}
                      </span>
                      <span className="tabular-nums text-[var(--muted-foreground,#94a3b8)]" style={{ width: 90 }}>
                        {COMPACT(data.tokens.by_tier[tier] ?? 0)} tok
                      </span>
                      <span className="tabular-nums text-[var(--foreground,#e2e8f0)]">
                        ${usd.toFixed(4)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  )
}

function KpiCard({
  icon: Icon, label, value, sub,
}: { icon: typeof ListChecks; label: string; value: string; sub: string }) {
  return (
    <div className="flex flex-col items-start gap-0.5 p-2 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.2))] bg-white/5"
      style={{ minHeight: 72 }}>
      <div className="flex items-center gap-1 font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
        <Icon className="w-3 h-3" aria-hidden />
        {label}
      </div>
      <div className="font-mono text-[18px] font-semibold text-[var(--neural-cyan,#67e8f9)] tabular-nums leading-none mt-1">
        {value}
      </div>
      <div className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] truncate w-full" title={sub}>
        {sub}
      </div>
    </div>
  )
}

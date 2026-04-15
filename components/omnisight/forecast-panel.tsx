"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import {
  ChevronDown, ChevronUp, ListChecks, Users, Clock3, Cpu, DollarSign,
  Gauge, RefreshCw, BarChart3, TrendingUp, TrendingDown, Minus,
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

interface IntentField {
  value: string
  confidence: number
}

interface SpecDetail {
  spec: {
    target_arch: IntentField
    framework: IntentField
    hardware_required: IntentField
    target_os: IntentField
    project_type: IntentField
    [key: string]: IntentField | string | unknown[]
  }
}

interface ForecastDelta {
  hours: number
  tokens: number
  reason: string
}

const COMPACT = (n: number, suffix = ""): string => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M${suffix}`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k${suffix}`
  return `${n}${suffix}`
}

const DEBOUNCE_MS = 800

export function ForecastPanel() {
  const [data, setData] = useState<Forecast | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [delta, setDelta] = useState<ForecastDelta | null>(null)
  const prevRef = useRef<Forecast | null>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fetchForecast = useCallback(async (recompute = false) => {
    setBusy(true)
    setError(null)
    try {
      const path = recompute ? "/api/v1/system/forecast/recompute" : "/api/v1/system/forecast"
      const res = await fetch(path, { method: recompute ? "POST" : "GET", cache: "no-store" })
      if (!res.ok) throw new Error(`API ${res.status}`)
      const next = (await res.json()) as Forecast
      setData((prev) => {
        if (prev) {
          prevRef.current = prev
          const dHours = +(next.duration.total_hours - prev.duration.total_hours).toFixed(1)
          const dTokens = next.tokens.total - prev.tokens.total
          if (dHours !== 0 || dTokens !== 0) {
            const parts: string[] = []
            if (next.target_platform !== prev.target_platform) parts.push(`platform: ${prev.target_platform} → ${next.target_platform}`)
            if (next.project_track !== prev.project_track) parts.push(`track: ${prev.project_track} → ${next.project_track}`)
            setDelta({ hours: dHours, tokens: dTokens, reason: parts.join("; ") || "spec changed" })
          } else {
            setDelta(null)
          }
        }
        return next
      })
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { void fetchForecast(false) }, [fetchForecast])

  useEffect(() => {
    if (typeof window === "undefined") return
    const onSpecUpdated = (e: Event) => {
      const detail = (e as CustomEvent<SpecDetail>).detail
      if (!detail?.spec) return

      const arch = detail.spec.target_arch?.value
      const framework = detail.spec.framework?.value

      if ((!arch || arch === "unknown") && (!framework || framework === "unknown")) return

      if (debounceRef.current) clearTimeout(debounceRef.current)
      debounceRef.current = setTimeout(() => {
        void fetchForecast(true)
      }, DEBOUNCE_MS)
    }
    window.addEventListener("omnisight:spec-updated", onSpecUpdated as EventListener)
    return () => {
      window.removeEventListener("omnisight:spec-updated", onSpecUpdated as EventListener)
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [fetchForecast])

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
          {error}
        </div>
      )}

      {!data && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
          Loading forecast...
        </div>
      )}

      {data && (
        <>
          {/* Delta banner */}
          {delta && (
            <div
              className="flex items-center gap-2 px-3 py-1.5 font-mono text-[10px] border-b border-[var(--neural-border,rgba(148,163,184,0.35))]"
              role="status"
              aria-label="Forecast delta"
              style={{
                background: delta.hours > 0
                  ? "rgba(239,68,68,0.08)"
                  : delta.hours < 0
                    ? "rgba(16,185,129,0.08)"
                    : "rgba(148,163,184,0.08)",
              }}
            >
              {delta.hours > 0 ? (
                <TrendingUp className="w-3 h-3 text-[var(--critical-red,#ef4444)]" aria-hidden />
              ) : delta.hours < 0 ? (
                <TrendingDown className="w-3 h-3 text-[var(--validation-emerald,#10b981)]" aria-hidden />
              ) : (
                <Minus className="w-3 h-3 text-[var(--muted-foreground,#94a3b8)]" aria-hidden />
              )}
              <span className="tabular-nums">
                <span
                  className={
                    delta.hours > 0
                      ? "text-[var(--critical-red,#ef4444)]"
                      : delta.hours < 0
                        ? "text-[var(--validation-emerald,#10b981)]"
                        : "text-[var(--muted-foreground,#94a3b8)]"
                  }
                >
                  {delta.hours > 0 ? "+" : ""}{delta.hours}h
                </span>
                {" / "}
                <span
                  className={
                    delta.tokens > 0
                      ? "text-[var(--critical-red,#ef4444)]"
                      : delta.tokens < 0
                        ? "text-[var(--validation-emerald,#10b981)]"
                        : "text-[var(--muted-foreground,#94a3b8)]"
                  }
                >
                  {delta.tokens > 0 ? "+" : ""}{COMPACT(delta.tokens)} tok
                </span>
              </span>
              {delta.reason && (
                <span className="text-[var(--muted-foreground,#94a3b8)] truncate" title={delta.reason}>
                  {delta.reason}
                </span>
              )}
              <button
                type="button"
                onClick={() => setDelta(null)}
                className="ml-auto text-[var(--muted-foreground,#94a3b8)] hover:text-[var(--foreground,#e2e8f0)]"
                aria-label="dismiss delta"
              >
                x
              </button>
            </div>
          )}

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
                  PROFILE x HOURS
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

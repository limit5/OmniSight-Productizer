"use client"

/**
 * L1-04 — Ops Summary Panel.
 *
 * Polls /api/v1/ops/summary every 10 s. Intended for a quick-glance
 * operational check: spend burn-rate, any active freeze, pending
 * Decision Engine load, SSE bus pressure, watchdog liveness.
 *
 * Deliberately minimal. Zero new deps. No charts — just 6 KPIs and
 * a 🔴 / 🟢 dot. A Grafana dashboard is the right home for history;
 * this is the "is anything on fire right now?" glance.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { Activity, DollarSign, Flame, Radio, Shield, Clock3 } from "lucide-react"
import { getOpsSummary, type OpsSummary } from "@/lib/api"

const POLL_MS = 10_000

export function OpsSummaryPanel() {
  const [data, setData] = useState<OpsSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const info = await getOpsSummary()
      if (!mountedRef.current) return
      setData(info)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh()
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => {
      mountedRef.current = false
      clearInterval(t)
    }
  }, [refresh])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Ops Summary"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Shield className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            OPS SUMMARY
          </h2>
        </div>
        <StatusDot data={data} error={error} />
      </header>

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--critical-red,#ef4444)] truncate" title={error}>
          ⚠ {error}
        </div>
      )}

      {!data && !error && (
        <div className="px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]">
          Loading…
        </div>
      )}

      {data && (
        <div className="grid grid-cols-2 gap-2 p-3">
          <Kpi icon={DollarSign} label="SPEND (hr)"
               value={`$${data.hourly_cost_usd.toFixed(4)}`}
               tone={data.token_frozen ? "bad" : "ok"} />
          <Kpi icon={DollarSign} label="SPEND (day)"
               value={`$${data.daily_cost_usd.toFixed(2)}`}
               tone={data.token_frozen ? "bad" : "ok"} />
          <Kpi icon={Flame} label="BUDGET"
               value={data.budget_level}
               tone={data.token_frozen ? "bad" : data.budget_level === "normal" ? "ok" : "warn"} />
          <Kpi icon={Activity} label="DECISIONS"
               value={String(data.decisions_pending)}
               tone={data.decisions_pending === 0 ? "ok" : data.decisions_pending > 10 ? "warn" : "info"} />
          <Kpi icon={Radio} label="SSE SUBS"
               value={String(data.sse_subscribers)}
               tone="info" />
          <Kpi icon={Clock3} label="WATCHDOG"
               value={data.watchdog_age_s === null ? "—" : `${Math.round(data.watchdog_age_s)}s`}
               tone={data.watchdog_age_s === null || data.watchdog_age_s > 120 ? "warn" : "ok"} />
        </div>
      )}
    </section>
  )
}

type Tone = "ok" | "warn" | "bad" | "info"

const TONE_CLASS: Record<Tone, string> = {
  ok:   "text-[var(--validation-emerald,#10b981)]",
  warn: "text-[var(--fui-orange,#f59e0b)]",
  bad:  "text-[var(--critical-red,#ef4444)]",
  info: "text-[var(--neural-cyan,#67e8f9)]",
}

function Kpi({
  icon: Icon, label, value, tone,
}: { icon: typeof Activity; label: string; value: string; tone: Tone }) {
  return (
    <div className="flex flex-col items-start gap-0.5 p-2 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.2))] bg-white/5">
      <div className="flex items-center gap-1 font-mono text-[9px] tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
        <Icon className="w-3 h-3" aria-hidden />
        {label}
      </div>
      <div className={`font-mono text-[14px] font-semibold tabular-nums leading-none mt-0.5 ${TONE_CLASS[tone]}`}>
        {value}
      </div>
    </div>
  )
}

function StatusDot({ data, error }: { data: OpsSummary | null; error: string | null }) {
  let color = "var(--muted-foreground,#94a3b8)"
  let label = "loading"
  if (error) {
    color = "var(--critical-red,#ef4444)"
    label = "error"
  } else if (data) {
    if (data.token_frozen) {
      color = "var(--critical-red,#ef4444)"
      label = "frozen"
    } else if (data.budget_level !== "normal" || (data.watchdog_age_s !== null && data.watchdog_age_s > 120)) {
      color = "var(--fui-orange,#f59e0b)"
      label = "degraded"
    } else {
      color = "var(--validation-emerald,#10b981)"
      label = "healthy"
    }
  }
  return (
    <span
      className="inline-flex items-center gap-1 font-mono text-[10px]"
      style={{ color }}
      aria-label={`status: ${label}`}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
      {label}
    </span>
  )
}

"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { Gauge, Gem, Rabbit, Settings2 } from "lucide-react"
import {
  type BudgetStrategyId,
  type BudgetTuning,
  type SSEEvent,
  getBudgetStrategy,
  setBudgetStrategy,
  subscribeEvents,
} from "@/lib/api"

/**
 * Phase 48D — Budget Strategy Panel.
 *
 * 4-card selector (Quality / Balanced / Cost Saver / Sprint).
 * Switching PUTs /budget-strategy; SSE budget_strategy_changed syncs
 * from other tabs. Each card shows the 5 tuning knobs so the operator
 * can see why the system will behave differently.
 */

const STRATEGY_META: Record<
  BudgetStrategyId,
  { label: string; hint: string; Icon: typeof Gem; color: string }
> = {
  quality: { label: "QUALITY", hint: "Premium 模型 · 3 次重試 · 不降級", Icon: Gem, color: "#a78bfa" },
  balanced: { label: "BALANCED", hint: "預設 · 2 次重試 · 90% 降級", Icon: Gauge, color: "var(--neural-blue,#60a5fa)" },
  cost_saver: { label: "COST SAVER", hint: "Budget 模型 · 1 次重試 · 70% 降級", Icon: Settings2, color: "#34d399" },
  sprint: { label: "SPRINT", hint: "並行優先 · 95% 降級", Icon: Rabbit, color: "#fbbf24" },
}

const ORDER: BudgetStrategyId[] = ["quality", "balanced", "cost_saver", "sprint"]

export function BudgetStrategyPanel() {
  const [current, setCurrent] = useState<BudgetStrategyId>("balanced")
  const [tuning, setTuning] = useState<BudgetTuning | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const info = await getBudgetStrategy()
      if (!mountedRef.current) return
      setCurrent(info.strategy)
      setTuning(info.tuning)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh()
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event === "budget_strategy_changed") {
        if (!mountedRef.current) return
        setCurrent(ev.data.strategy)
        setTuning(ev.data.tuning)
      }
    })
    return () => {
      mountedRef.current = false
      sub.close()
    }
  }, [refresh])

  const pick = async (s: BudgetStrategyId) => {
    if (s === current || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await setBudgetStrategy(s)
      setCurrent(res.strategy)
      setTuning(res.tuning)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Budget Strategy"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Gauge className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            BUDGET STRATEGY
          </h2>
        </div>
        <span className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
          {STRATEGY_META[current].hint}
        </span>
      </header>

      {error && (
        <div className="px-3 py-1.5 font-mono text-[10px] text-[var(--critical-red,#ef4444)]">
          {error}
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-2 p-2">
        {ORDER.map((s) => {
          const meta = STRATEGY_META[s]
          const active = s === current
          const { Icon } = meta
          return (
            <button
              key={s}
              role="radio"
              aria-checked={active}
              disabled={busy}
              onClick={() => void pick(s)}
              className={`flex flex-col items-start gap-1 p-2 rounded-sm border transition-colors text-left ${
                active
                  ? "border-transparent text-black"
                  : "border-[var(--neural-border,rgba(148,163,184,0.35))] text-[var(--foreground,#e2e8f0)] hover:bg-white/5"
              } ${busy ? "cursor-wait opacity-70" : "cursor-pointer"}`}
              style={active ? { backgroundColor: meta.color } : undefined}
              title={meta.hint}
            >
              <div className="flex items-center gap-1">
                <Icon className="w-3.5 h-3.5" style={{ color: active ? "#000" : meta.color }} aria-hidden />
                <span className="font-mono text-[11px] tracking-wider font-bold">{meta.label}</span>
              </div>
              <span className={`font-mono text-[9px] leading-tight ${active ? "text-black/80" : "text-[var(--muted-foreground,#94a3b8)]"}`}>
                {meta.hint}
              </span>
            </button>
          )
        })}
      </div>

      {tuning && (
        <div className="px-3 py-2 grid grid-cols-2 lg:grid-cols-5 gap-2 border-t border-[var(--neural-border,rgba(148,163,184,0.35))] font-mono text-[10px]">
          <TuningCell label="TIER" value={tuning.model_tier.toUpperCase()} />
          <TuningCell label="RETRIES" value={String(tuning.max_retries)} />
          <TuningCell label="DOWNGRADE" value={`${tuning.downgrade_at_usage_pct}%`} />
          <TuningCell label="FREEZE" value={`${tuning.freeze_at_usage_pct}%`} />
          <TuningCell label="PARALLEL" value={tuning.prefer_parallel ? "YES" : "NO"} />
        </div>
      )}
    </section>
  )
}

function TuningCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[var(--muted-foreground,#94a3b8)]">{label}</span>
      <span className="tabular-nums text-[var(--foreground,#e2e8f0)]">{value}</span>
    </div>
  )
}

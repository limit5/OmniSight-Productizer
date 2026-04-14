"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { Gauge, Gem, Rabbit, Settings2 } from "lucide-react"
import { PanelHelp } from "@/components/omnisight/panel-help"
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

  // R3-B-#35: auto-clear stale error after 10 s. Without this the red
  // error bar stays forever even after the user successfully switched
  // strategies via a different path, making the UI feel stuck.
  useEffect(() => {
    if (!error) return
    const t = setTimeout(() => setError(null), 10_000)
    return () => clearTimeout(t)
  }, [error])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Budget Strategy"
      data-tour="budget"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Gauge className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            BUDGET STRATEGY
          </h2>
          <PanelHelp doc="budget-strategies" />
        </div>
        <span
          className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] truncate text-right"
          style={{ maxWidth: 240 }}
          title={STRATEGY_META[current].hint}
        >
          {STRATEGY_META[current].hint}
        </span>
      </header>

      {error && (
        <div className="px-3 py-1.5 flex items-center justify-between gap-2 font-mono text-[10px] text-[var(--critical-red,#ef4444)]">
          <span className="truncate">{error}</span>
          <button
            onClick={() => void refresh()}
            className="px-1.5 py-0.5 rounded-sm border border-current hover:bg-current/10"
          >
            RETRY
          </button>
        </div>
      )}

      <div
        className="grid grid-cols-2 gap-2 p-2"
        role="radiogroup"
        aria-labelledby="budget-strategy-label"
      >
        <span id="budget-strategy-label" className="sr-only">Budget Strategy</span>
        {ORDER.map((s, idx) => {
          const meta = STRATEGY_META[s]
          const active = s === current
          const { Icon } = meta
          const slotId = String(idx + 1).padStart(2, "0")
          return (
            <button
              key={s}
              role="radio"
              aria-checked={active}
              disabled={busy}
              onClick={() => void pick(s)}
              className={`group relative flex flex-col gap-1.5 p-2.5 pt-3 rounded-sm border text-left min-w-0 overflow-hidden transition-all duration-200 ${
                active
                  ? "border-transparent"
                  : "border-[var(--neural-border,rgba(148,163,184,0.35))] hover:border-[color:var(--card-accent)] hover:bg-white/5"
              } ${busy ? "cursor-wait opacity-70" : "cursor-pointer"}`}
              style={{
                // `--card-accent` is consumed by the border/hover rules and the
                // inner glyphs so each card gets its own accent without a
                // style explosion.
                ["--card-accent" as string]: meta.color,
                backgroundColor: active ? meta.color : undefined,
                boxShadow: active
                  ? `0 0 0 1px ${meta.color}, 0 0 18px -4px ${meta.color}, inset 0 0 24px -12px rgba(0,0,0,0.6)`
                  : undefined,
                color: active ? "#020617" : undefined,
              }}
              title={meta.hint}
            >
              {/* Corner brackets — tiny sci-fi targeting reticle */}
              <span aria-hidden className="pointer-events-none absolute inset-1 flex justify-between">
                <span className={`w-1.5 h-1.5 border-l border-t ${active ? "border-black/50" : "border-[color:var(--card-accent)] opacity-60 group-hover:opacity-100"}`} />
                <span className={`w-1.5 h-1.5 border-r border-t ${active ? "border-black/50" : "border-[color:var(--card-accent)] opacity-60 group-hover:opacity-100"}`} />
              </span>
              <span aria-hidden className="pointer-events-none absolute inset-1 top-auto h-1.5 flex justify-between">
                <span className={`w-1.5 h-1.5 border-l border-b ${active ? "border-black/50" : "border-[color:var(--card-accent)] opacity-60 group-hover:opacity-100"}`} />
                <span className={`w-1.5 h-1.5 border-r border-b ${active ? "border-black/50" : "border-[color:var(--card-accent)] opacity-60 group-hover:opacity-100"}`} />
              </span>

              {/* Slot id + active pulse dot */}
              <div className="flex items-center justify-between font-mono text-[8px] tracking-[0.25em] opacity-70">
                <span>SLOT_{slotId}</span>
                <span className="flex items-center gap-1">
                  {active && (
                    <span
                      aria-hidden
                      className="inline-block w-1.5 h-1.5 rounded-full animate-pulse"
                      style={{ backgroundColor: "#020617" }}
                    />
                  )}
                  <span>{active ? "ACTIVE" : "STANDBY"}</span>
                </span>
              </div>

              {/* Icon + full title — single line, fluid-sized so it never
                  has to wrap per-character at any column width. */}
              <div className="flex items-center gap-1.5 w-full min-w-0">
                <Icon
                  className="w-4 h-4 shrink-0"
                  style={{ color: active ? "#020617" : meta.color }}
                  aria-hidden
                />
                <span className="font-mono font-bold leading-tight tracking-[0.08em] text-[11px] xl:text-[12px] min-w-0 flex-1 whitespace-normal [overflow-wrap:normal] [word-break:keep-all]">
                  {meta.label}
                </span>
              </div>

              {/* Divider scanline — colored on active, accent-muted otherwise */}
              <span
                aria-hidden
                className="block h-px w-full"
                style={{
                  background: active
                    ? "linear-gradient(90deg, rgba(0,0,0,0.45), rgba(0,0,0,0) 80%)"
                    : `linear-gradient(90deg, var(--card-accent), transparent 70%)`,
                  opacity: active ? 0.5 : 0.35,
                }}
              />

              <span
                className={`font-mono text-[9.5px] leading-tight whitespace-normal break-words w-full ${
                  active ? "text-black/75" : "text-[var(--muted-foreground,#94a3b8)]"
                }`}
              >
                {meta.hint}
              </span>
            </button>
          )
        })}
      </div>

      {tuning && (
        <div className="px-3 py-2 grid grid-cols-2 lg:grid-cols-5 gap-2 border-t border-[var(--neural-border,rgba(148,163,184,0.35))] font-mono text-[10px]">
          <TuningCell label="TIER"      value={tuning.model_tier.toUpperCase()} hint="fast | balanced | strong" />
          <TuningCell label="RETRIES"   value={String(tuning.max_retries)}      hint="0–5" />
          <TuningCell label="DOWNGRADE" value={`${tuning.downgrade_at_usage_pct}%`} hint="0–100% token usage" />
          <TuningCell label="FREEZE"    value={`${tuning.freeze_at_usage_pct}%`}   hint="0–100% token usage" />
          <TuningCell label="PARALLEL"  value={tuning.prefer_parallel ? "YES" : "NO"} hint="YES | NO" />
        </div>
      )}
    </section>
  )
}

// B17: knob cells now surface the valid range / option set via title tooltip
// and an sr-only span so keyboard/AT users learn the vocabulary without a
// docs lookup.
function TuningCell({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex flex-col" title={hint ? `${label}: ${hint}` : undefined}>
      <span className="text-[var(--muted-foreground,#94a3b8)]">{label}</span>
      <span className="tabular-nums text-[var(--foreground,#e2e8f0)]">{value}</span>
      {hint && <span className="sr-only">Valid range: {hint}</span>}
    </div>
  )
}

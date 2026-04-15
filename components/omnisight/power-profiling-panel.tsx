"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import {
  Battery, BatteryCharging, BatteryFull, BatteryLow, BatteryMedium,
  BatteryWarning, Cpu, Gauge, Moon, Power, RefreshCw, Sun, ToggleLeft,
  ToggleRight, Zap,
} from "lucide-react"
import { PanelHelp } from "@/components/omnisight/panel-help"

interface PowerDomain {
  domain_id: string
  name: string
  description: string
  typical_active_ma: number
  typical_sleep_ma: number
}

interface FeatureToggle {
  toggle_id: string
  name: string
  description: string
  domains_affected: string[]
  extra_draw_ma: number
}

interface BudgetItem {
  toggle_id: string
  name: string
  enabled: boolean
  extra_draw_ma: number
  mah_per_day: number
  lifetime_impact_hours: number
}

interface FeatureBudget {
  base_avg_current_ma: number
  total_avg_current_ma: number
  base_lifetime_hours: number
  adjusted_lifetime_hours: number
  total_mah_per_day: number
  items: BudgetItem[]
}

interface SleepState {
  state_id: string
  name: string
  description: string
  typical_draw_pct: number
  wake_latency_ms: number
  order: number
}

function batteryIcon(lifetimeH: number) {
  if (lifetimeH >= 168) return <BatteryFull className="h-5 w-5 text-green-500" />
  if (lifetimeH >= 72) return <BatteryMedium className="h-5 w-5 text-lime-500" />
  if (lifetimeH >= 24) return <BatteryLow className="h-5 w-5 text-yellow-500" />
  return <BatteryWarning className="h-5 w-5 text-red-500" />
}

function formatHours(h: number): string {
  if (!isFinite(h)) return "∞"
  if (h >= 24) return `${(h / 24).toFixed(1)}d`
  return `${h.toFixed(1)}h`
}

function formatMah(mah: number): string {
  if (mah >= 1000) return `${(mah / 1000).toFixed(1)}Ah`
  return `${mah.toFixed(0)}mAh`
}

export function PowerProfilingPanel() {
  const [domains, setDomains] = useState<PowerDomain[]>([])
  const [features, setFeatures] = useState<FeatureToggle[]>([])
  const [sleepStates, setSleepStates] = useState<SleepState[]>([])
  const [budget, setBudget] = useState<FeatureBudget | null>(null)
  const [enabled, setEnabled] = useState<Set<string>>(new Set())
  const [batteryMah, setBatteryMah] = useState(3000)
  const [chemistry, setChemistry] = useState("li_ion")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<"budget" | "domains" | "states">("budget")
  const mountedRef = useRef(true)

  const apiBase = "/api/v1/power"

  const fetchData = useCallback(async () => {
    try {
      const [domRes, featRes, stateRes] = await Promise.all([
        fetch(`${apiBase}/domains`),
        fetch(`${apiBase}/features`),
        fetch(`${apiBase}/sleep-states`),
      ])
      if (domRes.ok) {
        const d = await domRes.json()
        if (mountedRef.current) setDomains(d.items || [])
      }
      if (featRes.ok) {
        const f = await featRes.json()
        if (mountedRef.current) setFeatures(f.items || [])
      }
      if (stateRes.ok) {
        const s = await stateRes.json()
        if (mountedRef.current) setSleepStates(s.items || [])
      }
    } catch (exc) {
      if (mountedRef.current) setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  const computeBudget = useCallback(async (enabledSet: Set<string>) => {
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`${apiBase}/budget`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled_features: Array.from(enabledSet),
          battery: { capacity_mah: batteryMah, chemistry },
          base_duty_cycle: {
            active_pct: 20, idle_pct: 30, sleep_pct: 50,
            active_current_ma: 500, idle_current_ma: 50, sleep_current_ma: 2,
          },
        }),
      })
      if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`)
      const data = await res.json()
      if (mountedRef.current) setBudget(data)
    } catch (exc) {
      if (mountedRef.current) setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      if (mountedRef.current) setBusy(false)
    }
  }, [batteryMah, chemistry])

  useEffect(() => {
    mountedRef.current = true
    void fetchData()
    return () => { mountedRef.current = false }
  }, [fetchData])

  useEffect(() => {
    void computeBudget(enabled)
  }, [enabled, computeBudget])

  const toggleFeature = (id: string) => {
    setEnabled(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BatteryCharging className="h-5 w-5 text-emerald-500" />
          <h3 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">
            Power &amp; Battery Profiling
          </h3>
          <PanelHelp tip="C11: Power profiling — mAh/day per feature toggle, battery lifetime model, sleep-state analysis" />
        </div>
        <button
          onClick={() => { void fetchData(); void computeBudget(enabled) }}
          className="rounded p-1 text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-600 dark:hover:bg-zinc-800"
          title="Refresh"
        >
          <RefreshCw className={`h-4 w-4 ${busy ? "animate-spin" : ""}`} />
        </button>
      </div>

      {error && (
        <div className="mb-3 rounded bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-900/30 dark:text-red-300">
          {error}
        </div>
      )}

      {/* Battery config */}
      <div className="mb-3 flex items-center gap-3 text-xs">
        <label className="flex items-center gap-1 text-zinc-500 dark:text-zinc-400">
          <Battery className="h-3.5 w-3.5" />
          <input
            type="number"
            value={batteryMah}
            onChange={e => setBatteryMah(Number(e.target.value) || 3000)}
            className="w-16 rounded border border-zinc-300 bg-transparent px-1 py-0.5 text-center text-xs dark:border-zinc-600"
          /> mAh
        </label>
        <select
          value={chemistry}
          onChange={e => setChemistry(e.target.value)}
          className="rounded border border-zinc-300 bg-transparent px-1 py-0.5 text-xs dark:border-zinc-600"
        >
          <option value="li_ion">Li-Ion</option>
          <option value="li_po">Li-Po</option>
          <option value="lifepo4">LiFePO4</option>
          <option value="nimh">NiMH</option>
        </select>
      </div>

      {/* Tabs */}
      <div className="mb-3 flex gap-1 border-b border-zinc-200 dark:border-zinc-700">
        {(["budget", "domains", "states"] as const).map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-xs font-medium transition ${
              tab === t
                ? "border-b-2 border-emerald-500 text-emerald-600 dark:text-emerald-400"
                : "text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300"
            }`}
          >
            {t === "budget" ? "mAh/day Budget" : t === "domains" ? "Power Domains" : "Sleep States"}
          </button>
        ))}
      </div>

      {/* Budget tab */}
      {tab === "budget" && budget && (
        <div>
          {/* Summary bar */}
          <div className="mb-3 grid grid-cols-3 gap-2">
            <div className="rounded-lg bg-zinc-50 p-2 text-center dark:bg-zinc-800">
              <div className="flex items-center justify-center gap-1">
                {batteryIcon(budget.adjusted_lifetime_hours)}
              </div>
              <div className="mt-1 text-lg font-bold text-zinc-800 dark:text-zinc-100">
                {formatHours(budget.adjusted_lifetime_hours)}
              </div>
              <div className="text-[10px] text-zinc-400">Lifetime</div>
            </div>
            <div className="rounded-lg bg-zinc-50 p-2 text-center dark:bg-zinc-800">
              <Zap className="mx-auto h-5 w-5 text-amber-500" />
              <div className="mt-1 text-lg font-bold text-zinc-800 dark:text-zinc-100">
                {budget.total_avg_current_ma.toFixed(0)} mA
              </div>
              <div className="text-[10px] text-zinc-400">Avg Draw</div>
            </div>
            <div className="rounded-lg bg-zinc-50 p-2 text-center dark:bg-zinc-800">
              <Gauge className="mx-auto h-5 w-5 text-blue-500" />
              <div className="mt-1 text-lg font-bold text-zinc-800 dark:text-zinc-100">
                {formatMah(budget.total_mah_per_day)}
              </div>
              <div className="text-[10px] text-zinc-400">mAh/day</div>
            </div>
          </div>

          {/* Feature toggles */}
          <div className="space-y-1">
            {budget.items.map(item => (
              <div
                key={item.toggle_id}
                className={`flex items-center justify-between rounded-lg px-3 py-2 text-xs transition ${
                  item.enabled
                    ? "bg-emerald-50 dark:bg-emerald-900/20"
                    : "bg-zinc-50 dark:bg-zinc-800/50"
                }`}
              >
                <div className="flex items-center gap-2">
                  <button onClick={() => toggleFeature(item.toggle_id)}>
                    {item.enabled
                      ? <ToggleRight className="h-4 w-4 text-emerald-500" />
                      : <ToggleLeft className="h-4 w-4 text-zinc-400" />}
                  </button>
                  <span className="font-medium text-zinc-700 dark:text-zinc-300">{item.name}</span>
                </div>
                <div className="flex items-center gap-3 text-zinc-500 dark:text-zinc-400">
                  <span>+{item.extra_draw_ma.toFixed(0)} mA</span>
                  <span className="w-16 text-right">{item.mah_per_day.toFixed(0)} mAh/d</span>
                  {item.lifetime_impact_hours > 0 && (
                    <span className="text-red-400">-{formatHours(item.lifetime_impact_hours)}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Power Domains tab */}
      {tab === "domains" && (
        <div className="space-y-1">
          {domains.map(d => (
            <div
              key={d.domain_id}
              className="flex items-center justify-between rounded-lg bg-zinc-50 px-3 py-2 text-xs dark:bg-zinc-800/50"
            >
              <div className="flex items-center gap-2">
                <Cpu className="h-3.5 w-3.5 text-blue-500" />
                <span className="font-medium text-zinc-700 dark:text-zinc-300">{d.name}</span>
              </div>
              <div className="flex gap-3 text-zinc-500 dark:text-zinc-400">
                <span className="flex items-center gap-1">
                  <Sun className="h-3 w-3 text-amber-400" /> {d.typical_active_ma} mA
                </span>
                <span className="flex items-center gap-1">
                  <Moon className="h-3 w-3 text-indigo-400" /> {d.typical_sleep_ma} mA
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Sleep States tab */}
      {tab === "states" && (
        <div className="space-y-1">
          {sleepStates.map(s => (
            <div
              key={s.state_id}
              className="flex items-center justify-between rounded-lg bg-zinc-50 px-3 py-2 text-xs dark:bg-zinc-800/50"
            >
              <div className="flex items-center gap-2">
                <Power className="h-3.5 w-3.5 text-violet-500" />
                <div>
                  <span className="font-medium text-zinc-700 dark:text-zinc-300">{s.name}</span>
                  <span className="ml-2 text-zinc-400">{s.typical_draw_pct}% draw</span>
                </div>
              </div>
              <span className="text-zinc-500 dark:text-zinc-400">
                wake: {s.wake_latency_ms}ms
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

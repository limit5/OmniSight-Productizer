"use client"

import { useCallback, useEffect, useState } from "react"
import {
  type OperationMode,
  type SSEEvent,
  getOperationMode,
  setOperationMode,
  subscribeEvents,
} from "@/lib/api"

/**
 * OperationMode selector — 4-pill segmented control.
 * Shown in the global header. Syncs with the backend via SSE
 * (mode_changed) and surfaces the current in_flight / parallel_cap.
 */

const MODE_META: Record<OperationMode, { label: string; hint: string; color: string }> = {
  manual: { label: "MANUAL", hint: "每步要人批准", color: "var(--neural-cyan, #67e8f9)" },
  supervised: { label: "SUPERVISED", hint: "常規自動，風險要批准", color: "var(--neural-blue, #60a5fa)" },
  full_auto: { label: "FULL AUTO", hint: "除破壞性外全自動", color: "var(--neural-amber, #fbbf24)" },
  turbo: { label: "TURBO", hint: "倒數計時後執行一切", color: "var(--neural-red, #f87171)" },
}

const MODE_ORDER: OperationMode[] = ["manual", "supervised", "full_auto", "turbo"]

interface Props {
  compact?: boolean
}

export function ModeSelector({ compact = false }: Props) {
  const [mode, setMode] = useState<OperationMode>("supervised")
  const [cap, setCap] = useState(2)
  const [inFlight, setInFlight] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const info = await getOperationMode()
      setMode(info.mode)
      setCap(info.parallel_cap)
      setInFlight(info.in_flight)
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    void refresh()
    // SSE: live-update when another client changes mode
    const es = subscribeEvents((ev: SSEEvent) => {
      if (ev.event === "mode_changed") {
        setMode(ev.data.mode)
        setCap(ev.data.parallel_cap)
        setInFlight(ev.data.in_flight)
      }
    })
    return () => es.close()
  }, [refresh])

  // Poll in_flight every 5s since there's no dedicated SSE for it
  useEffect(() => {
    const t = setInterval(() => void refresh(), 5000)
    return () => clearInterval(t)
  }, [refresh])

  const handlePick = async (next: OperationMode) => {
    if (next === mode || busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await setOperationMode(next)
      setMode(res.mode)
      setCap(res.parallel_cap)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={`flex items-center gap-1 ${compact ? "text-[10px]" : "text-xs"}`}
      role="radiogroup"
      aria-label="Operation Mode"
      title={error ?? MODE_META[mode].hint}
    >
      <span className="text-[var(--neural-muted, #64748b)] hidden md:inline">MODE</span>
      <div className="flex items-center border border-[var(--neural-border, rgba(148,163,184,0.35))] rounded-sm overflow-hidden">
        {MODE_ORDER.map((m) => {
          const active = m === mode
          const meta = MODE_META[m]
          return (
            <button
              key={m}
              role="radio"
              aria-checked={active}
              disabled={busy}
              onClick={() => void handlePick(m)}
              className={`px-2 py-0.5 font-mono tracking-wider transition-colors ${
                active
                  ? "text-black font-bold"
                  : "text-[var(--neural-muted, #94a3b8)] hover:text-white hover:bg-white/5"
              } ${busy ? "cursor-wait opacity-70" : "cursor-pointer"}`}
              style={active ? { backgroundColor: meta.color } : undefined}
              title={meta.hint}
            >
              {compact ? meta.label[0] : meta.label}
            </button>
          )
        })}
      </div>
      <span
        className="font-mono text-[var(--neural-muted, #94a3b8)] ml-1"
        aria-label="parallel slots"
        title={`in-flight / cap; over-cap agents drain on next mode-tightening`}
      >
        {inFlight}/{cap}
      </span>
    </div>
  )
}

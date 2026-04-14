"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import {
  type OperationMode,
  type SSEEvent,
  getOperationMode,
  setOperationMode,
  subscribeEvents,
} from "@/lib/api"
import { PanelHelp } from "@/components/omnisight/panel-help"

/**
 * OperationMode selector — 4-pill segmented control.
 * Shown in the global header. Syncs with the backend via SSE
 * (mode_changed) and surfaces the current in_flight / parallel_cap.
 */

// Each compact label is a distinct 3-letter stem so M/S/F/T misreads
// on mobile go away (MAN / SUP / AUT / TRB).
const MODE_META: Record<OperationMode, { label: string; compact: string; hint: string; color: string }> = {
  manual:     { label: "MANUAL",     compact: "MAN", hint: "每步要人批准",          color: "var(--neural-cyan, #67e8f9)" },
  supervised: { label: "SUPERVISED", compact: "SUP", hint: "常規自動，風險要批准",  color: "var(--neural-blue, #60a5fa)" },
  full_auto:  { label: "FULL AUTO",  compact: "AUT", hint: "除破壞性外全自動",      color: "var(--neural-amber, #fbbf24)" },
  turbo:      { label: "TURBO",      compact: "TRB", hint: "倒數計時後執行一切",    color: "var(--neural-red, #f87171)" },
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
  // "Engaging" burst animation — toggled true for ~420ms whenever
  // the active mode flips, then cleared so CSS can replay next time.
  const [engaging, setEngaging] = useState(false)
  const prevModeRef = useRef<OperationMode>(mode)
  useEffect(() => {
    if (prevModeRef.current === mode) return
    prevModeRef.current = mode
    setEngaging(true)
    const t = setTimeout(() => setEngaging(false), 430)
    return () => clearTimeout(t)
  }, [mode])

  const mountedRef = useRef(true)
  const abortRef = useRef<AbortController | null>(null)

  const refresh = useCallback(async () => {
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    try {
      const info = await getOperationMode()
      if (!mountedRef.current || ac.signal.aborted) return
      setMode(info.mode)
      setCap(info.parallel_cap)
      setInFlight(info.in_flight)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current || ac.signal.aborted) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  // Keep a ref to the latest refresh so the 5 s interval (which only
  // mounts once) can always call the current closure without restarting.
  const refreshRef = useRef(refresh)
  useEffect(() => { refreshRef.current = refresh }, [refresh])

  // SSE + initial load. Fires once on mount.
  useEffect(() => {
    mountedRef.current = true
    void refreshRef.current()
    const sub = subscribeEvents((ev: SSEEvent) => {
      if (ev.event === "mode_changed") {
        if (!mountedRef.current) return
        setMode(ev.data.mode)
        setCap(ev.data.parallel_cap)
        setInFlight(ev.data.in_flight)
      }
    })
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
      sub.close()
    }
  }, [])

  // Poll in_flight every 5 s — single interval for component lifetime.
  useEffect(() => {
    const t = setInterval(() => { void refreshRef.current() }, 5000)
    return () => clearInterval(t)
  }, [])

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
      className={`flex items-center gap-1.5 ${compact ? "text-[10px]" : "text-xs"}`}
      role="radiogroup"
      aria-labelledby="operation-mode-label"
      title={error ?? MODE_META[mode].hint}
    >
      <span
        id="operation-mode-label"
        className="mode-label font-mono hidden md:inline"
      >
        MODE
      </span>
      <PanelHelp doc="operation-modes" />

      <div className="mode-frame flex items-stretch">
        {MODE_ORDER.map((m, idx) => {
          const active = m === mode
          const meta = MODE_META[m]
          return (
            <div key={m} className="flex items-stretch">
              {idx > 0 && <span className="mode-divider" aria-hidden />}
              <button
                role="radio"
                aria-checked={active}
                disabled={busy}
                onClick={() => void handlePick(m)}
                data-active={active}
                data-engaging={active && engaging ? "true" : "false"}
                className={`mode-pill px-2.5 py-0.5 font-mono tracking-[0.18em] transition-[background,color,box-shadow] ${
                  busy ? "cursor-wait opacity-70" : "cursor-pointer"
                }`}
                style={active
                  ? ({ backgroundColor: meta.color, ["--mode-color" as string]: meta.color } as React.CSSProperties)
                  : undefined}
                title={meta.hint}
              >
                {active && <span className="mode-pill-scan" aria-hidden />}
                {active && <span className="mode-pill-edge" aria-hidden />}
                <span className="mode-pill-label" aria-label={meta.label}>
                  {compact ? meta.compact : meta.label}
                </span>
              </button>
            </div>
          )
        })}
      </div>
      <span
        className="mode-lcd font-mono text-[10px] ml-1"
        data-load={inFlight > 0 ? "true" : "false"}
        aria-label="parallel slots"
        title={`in-flight / cap; over-cap agents drain on next mode-tightening`}
      >
        {inFlight}/{cap}
      </span>
      {error && (
        <span
          role="alert"
          className="font-mono text-[10px] text-[var(--critical-red,#ef4444)] animate-pulse"
          title={error}
        >
          ⚠ {error.length > 40 ? error.slice(0, 40) + "…" : error}
        </span>
      )}
    </div>
  )
}

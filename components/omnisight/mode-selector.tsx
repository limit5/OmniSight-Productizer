"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { AlertOctagon, RotateCcw, X } from "lucide-react"
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

  // Error popover open state — separate from `error` value so closing
  // doesn't clear the underlying state.
  const [errorOpen, setErrorOpen] = useState(false)
  const errorPopRef = useRef<HTMLDivElement | null>(null)
  const errorBadgeRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (!errorOpen) return
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node
      if (errorPopRef.current?.contains(t) || errorBadgeRef.current?.contains(t)) return
      setErrorOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setErrorOpen(false) }
    document.addEventListener("mousedown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDoc)
      document.removeEventListener("keydown", onKey)
    }
  }, [errorOpen])
  // Auto-clear error display when refresh succeeds (error becomes null).
  useEffect(() => { if (!error) setErrorOpen(false) }, [error])
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
      data-tour="mode"
    >
      <span
        id="operation-mode-label"
        className="mode-label font-mono hidden md:inline"
      >
        MODE
      </span>
      <PanelHelp doc="operation-modes" tourAnchor />

      <div className="mode-frame flex items-stretch relative">
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
        {/* Error badge — absolutely positioned on the mode-frame so it
         * never affects the surrounding flex layout. Click opens a
         * popover with the full message + RETRY. */}
        {error && (
          <button
            ref={errorBadgeRef}
            type="button"
            onClick={() => setErrorOpen((v) => !v)}
            aria-label={`Mode error — click for details`}
            aria-expanded={errorOpen}
            aria-haspopup="dialog"
            title={error}
            className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full flex items-center justify-center z-20 shadow-md"
            style={{
              background: "var(--critical-red,#ef4444)",
              color: "white",
              animation: "toast-urgent-pulse 1.4s ease-in-out infinite",
              boxShadow: "0 0 0 2px var(--background,#010409), 0 0 8px rgba(239,68,68,0.6)",
            }}
          >
            <AlertOctagon className="w-2.5 h-2.5" aria-hidden />
          </button>
        )}
        {error && errorOpen && (
          <div
            ref={errorPopRef}
            role="dialog"
            aria-label="Mode error details"
            className="absolute right-0 top-full mt-2 z-50 w-[min(320px,calc(100vw-2rem))] holo-glass-simple rounded-sm border border-[var(--critical-red,#ef4444)]/60 shadow-lg p-3 font-mono text-[11px]"
          >
            <div className="flex items-center justify-between mb-1.5">
              <span className="tracking-wider font-semibold text-[var(--critical-red,#ef4444)]">
                MODE API ERROR
              </span>
              <button
                type="button"
                onClick={() => setErrorOpen(false)}
                aria-label="close"
                className="text-[var(--muted-foreground,#94a3b8)] hover:text-white"
              >
                <X className="w-3 h-3" aria-hidden />
              </button>
            </div>
            <div className="text-[var(--foreground,#e2e8f0)] mb-2 break-words leading-snug max-h-40 overflow-y-auto">
              {error}
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => { setErrorOpen(false); void refresh() }}
                className="flex items-center gap-1 px-2 py-1 rounded-sm border border-[var(--neural-cyan,#67e8f9)] text-[var(--neural-cyan,#67e8f9)] hover:bg-[var(--neural-cyan,#67e8f9)]/10"
              >
                <RotateCcw className="w-3 h-3" aria-hidden />
                RETRY
              </button>
            </div>
          </div>
        )}
      </div>
      <span
        className="mode-lcd font-mono text-[10px] ml-1"
        data-load={inFlight > 0 ? "true" : "false"}
        aria-label="parallel slots"
        title={`in-flight / cap; over-cap agents drain on next mode-tightening`}
      >
        {inFlight}/{cap}
      </span>
    </div>
  )
}

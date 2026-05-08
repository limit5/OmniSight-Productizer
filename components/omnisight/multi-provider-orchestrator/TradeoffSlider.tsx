"use client"

/**
 * OP-41 / MP.W4.5 — Cheap/Fast tradeoff slider.
 *
 * Keeps the primitive intentionally small: the parent owns the routing
 * value, while this component restores the last operator preference from
 * localStorage and mirrors future changes back to the same key.
 */

import { useState } from "react"
import { CircleDollarSign, Gauge } from "lucide-react"

import { cn } from "@/lib/utils"

export interface TradeoffSliderProps {
  value: number
  onChange: (value: number) => void
  className?: string
}

export const TRADEOFF_SLIDER_STORAGE_KEY = "mp_tradeoff_slider_v1"

function normalizeTradeoffValue(value: number): number {
  if (!Number.isFinite(value)) return 0.5
  return Math.min(1, Math.max(0, Math.round(value * 100) / 100))
}

function readStoredTradeoffValue(): number | null {
  if (typeof window === "undefined") return null

  try {
    const stored = window.localStorage.getItem(TRADEOFF_SLIDER_STORAGE_KEY)
    if (stored === null) return null

    const parsed = Number.parseFloat(stored)
    return Number.isFinite(parsed) ? normalizeTradeoffValue(parsed) : null
  } catch {
    return null
  }
}

function writeStoredTradeoffValue(value: number) {
  if (typeof window === "undefined") return

  try {
    window.localStorage.setItem(TRADEOFF_SLIDER_STORAGE_KEY, value.toFixed(2))
  } catch {
    // Storage may be unavailable in private browsing or locked-down shells.
  }
}

export function TradeoffSlider({
  value,
  onChange,
  className,
}: TradeoffSliderProps) {
  const normalizedValue = normalizeTradeoffValue(value)
  const [displayValue, setDisplayValue] = useState(() => {
    return readStoredTradeoffValue() ?? normalizedValue
  })
  const percentFast = Math.round(displayValue * 100)

  function handleChange(nextRawValue: string) {
    const nextValue = normalizeTradeoffValue(Number.parseFloat(nextRawValue))
    setDisplayValue(nextValue)
    writeStoredTradeoffValue(nextValue)
    onChange(nextValue)
  }

  return (
    <div
      className={cn(
        "rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)]/80 px-3 py-3",
        className,
      )}
      data-testid="mp-tradeoff-slider"
    >
      <div className="mb-2 flex items-center justify-between gap-3 font-mono text-[10px] uppercase text-[var(--muted-foreground,#94a3b8)]">
        <span className="inline-flex items-center gap-1">
          <CircleDollarSign className="h-3.5 w-3.5 text-emerald-300" aria-hidden />
          Cheap
        </span>
        <span
          className="text-xs tabular-nums text-[var(--foreground,#e2e8f0)]"
          data-testid="mp-tradeoff-slider-value"
        >
          {percentFast}% fast
        </span>
        <span className="inline-flex items-center gap-1">
          Fast
          <Gauge className="h-3.5 w-3.5 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
        </span>
      </div>

      <input
        aria-label="Cheap vs Fast tradeoff"
        className="h-2 w-full cursor-pointer accent-[var(--neural-cyan,#67e8f9)]"
        data-testid="mp-tradeoff-slider-input"
        max={1}
        min={0}
        onChange={(event) => handleChange(event.currentTarget.value)}
        step={0.01}
        type="range"
        value={displayValue}
      />
    </div>
  )
}

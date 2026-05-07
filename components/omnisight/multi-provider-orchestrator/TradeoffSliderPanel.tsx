"use client"

/**
 * MP.W6.5 - War Room tradeoff slider panel.
 *
 * Mirrors the Provider Constellation Cheap/Fast slider visual language
 * while exposing finer-grained War Room controls for later panel wiring.
 * The component is pure presentation over props except for its optional
 * uncontrolled value fallback.
 */

import { useId, useMemo, useState } from "react"
import { CircleDollarSign, Gauge, SlidersHorizontal } from "lucide-react"

import { Slider } from "@/components/ui/slider"
import { cn } from "@/lib/utils"

export interface TradeoffSliderMark {
  value: number
  label: string
}

export interface TradeoffSliderPreset extends TradeoffSliderMark {
  description?: string
}

export interface TradeoffSliderConfig {
  min: number
  max: number
  step: number
  marks: ReadonlyArray<TradeoffSliderMark>
  presets: ReadonlyArray<TradeoffSliderPreset>
}

export interface TradeoffSliderPanelProps {
  value?: number
  defaultValue?: number
  config?: Partial<TradeoffSliderConfig>
  title?: string
  description?: string
  cheapLabel?: string
  fastLabel?: string
  valueFormatter?: (value: number) => string
  onValueChange?: (value: number) => void
  className?: string
  "data-testid"?: string
}

const DEFAULT_CONFIG: TradeoffSliderConfig = {
  min: 0,
  max: 100,
  step: 5,
  marks: [
    { value: 0, label: "Cheapest" },
    { value: 25, label: "Cost lean" },
    { value: 50, label: "Balanced" },
    { value: 75, label: "Speed lean" },
    { value: 100, label: "Fastest" },
  ],
  presets: [
    {
      value: 20,
      label: "Budget",
      description: "Prefer lower-cost providers for non-urgent work.",
    },
    {
      value: 50,
      label: "Balanced",
      description: "Keep cost and completion time evenly weighted.",
    },
    {
      value: 80,
      label: "Rush",
      description: "Favor faster completion when quota allows it.",
    },
  ],
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(max, Math.max(min, value))
}

function nearestStep(value: number, min: number, step: number): number {
  if (!Number.isFinite(step) || step <= 0) return value
  return min + Math.round((value - min) / step) * step
}

function defaultValueFormatter(value: number): string {
  return `${value.toFixed(0)}% fast`
}

function normalizeConfig(
  config: Partial<TradeoffSliderConfig> | undefined,
): TradeoffSliderConfig {
  const min = Number.isFinite(config?.min) ? Number(config?.min) : DEFAULT_CONFIG.min
  const max = Number.isFinite(config?.max) ? Number(config?.max) : DEFAULT_CONFIG.max
  const safeMin = Math.min(min, max)
  const safeMax = Math.max(min, max)
  const step =
    Number.isFinite(config?.step) && Number(config?.step) > 0
      ? Number(config?.step)
      : DEFAULT_CONFIG.step

  return {
    min: safeMin,
    max: safeMax,
    step,
    marks: config?.marks ?? DEFAULT_CONFIG.marks,
    presets: config?.presets ?? DEFAULT_CONFIG.presets,
  }
}

function normalizeValue(value: number, config: TradeoffSliderConfig): number {
  return clamp(
    nearestStep(value, config.min, config.step),
    config.min,
    config.max,
  )
}

function positionForValue(value: number, config: TradeoffSliderConfig): number {
  const span = config.max - config.min
  if (span <= 0) return 0
  return ((clamp(value, config.min, config.max) - config.min) / span) * 100
}

function markAlignmentClass(position: number): string {
  if (position <= 0) return "left-0 text-left"
  if (position >= 100) return "right-0 text-right"
  return "translate-x-[-50%] text-center"
}

function presetTestId(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "-")
}

export function TradeoffSliderPanel({
  value,
  defaultValue = 50,
  config,
  title = "Tradeoff Slider",
  description = "Tune routing between low-cost providers and faster completion.",
  cheapLabel = "Cheap",
  fastLabel = "Fast",
  valueFormatter = defaultValueFormatter,
  onValueChange,
  className,
  "data-testid": testId = "mp-tradeoff-slider-panel",
}: TradeoffSliderPanelProps) {
  const labelId = useId()
  const resolvedConfig = useMemo(() => normalizeConfig(config), [config])
  const [internalValue, setInternalValue] = useState(() =>
    normalizeValue(defaultValue, resolvedConfig),
  )
  const currentValue = normalizeValue(value ?? internalValue, resolvedConfig)
  const valueLabel = valueFormatter(currentValue)

  function setNextValue(nextValue: number) {
    const normalized = normalizeValue(nextValue, resolvedConfig)
    if (value === undefined) {
      setInternalValue(normalized)
    }
    onValueChange?.(normalized)
  }

  return (
    <section
      data-testid={testId}
      className={cn(
        "holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)]/80",
        className,
      )}
      aria-labelledby={labelId}
    >
      <header className="flex items-center justify-between gap-3 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <SlidersHorizontal
            className="h-4 w-4 text-[var(--neural-cyan,#67e8f9)]"
            aria-hidden
          />
          <h2 id={labelId} className="truncate font-mono text-sm text-[var(--neural-cyan,#67e8f9)]">
            {title}
          </h2>
        </div>
        <span
          data-testid={`${testId}-value`}
          className="shrink-0 font-mono text-xs text-[var(--foreground,#e2e8f0)]"
        >
          {valueLabel}
        </span>
      </header>

      <div className="flex flex-col gap-4 px-3 py-3">
        <p className="text-xs leading-5 text-[var(--muted-foreground,#94a3b8)]">
          {description}
        </p>

        <div>
          <div className="mb-2 flex items-center justify-between font-mono text-[10px] uppercase text-[var(--muted-foreground,#94a3b8)]">
            <span>{cheapLabel}</span>
            <span>{fastLabel}</span>
          </div>
          <div className="flex w-full items-center gap-3 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)]/80 px-3 py-2">
            <CircleDollarSign className="h-4 w-4 text-emerald-300" aria-hidden />
            <Slider
              data-testid={`${testId}-slider`}
              aria-labelledby={labelId}
              aria-valuetext={valueLabel}
              min={resolvedConfig.min}
              max={resolvedConfig.max}
              step={resolvedConfig.step}
              value={[currentValue]}
              onValueChange={(values) => {
                const nextValue = values[0]
                if (typeof nextValue === "number") setNextValue(nextValue)
              }}
              className="[&_[data-slot=slider-range]]:bg-[var(--neural-cyan,#67e8f9)] [&_[data-slot=slider-thumb]]:border-[var(--neural-cyan,#67e8f9)] [&_[data-slot=slider-track]]:bg-white/10"
            />
            <Gauge
              className="h-4 w-4 text-[var(--neural-cyan,#67e8f9)]"
              aria-hidden
            />
          </div>
        </div>

        <div
          className="relative h-5 font-mono text-[10px] uppercase text-[var(--muted-foreground,#94a3b8)]"
          aria-hidden
        >
          {resolvedConfig.marks.map((mark) => {
            const position = positionForValue(mark.value, resolvedConfig)
            return (
              <span
                key={`${mark.label}-${mark.value}`}
                className={cn(
                  "absolute top-0 max-w-24 truncate whitespace-nowrap",
                  markAlignmentClass(position),
                )}
                style={{ left: position > 0 && position < 100 ? `${position}%` : undefined }}
              >
                {mark.label}
              </span>
            )
          })}
        </div>

        {resolvedConfig.presets.length > 0 ? (
          <div className="grid grid-cols-[repeat(auto-fit,minmax(6.5rem,1fr))] gap-2">
            {resolvedConfig.presets.map((preset) => {
              const presetValue = normalizeValue(preset.value, resolvedConfig)
              const active = presetValue === currentValue
              return (
                <button
                  key={`${preset.label}-${preset.value}`}
                  type="button"
                  data-testid={`${testId}-preset-${presetTestId(preset.label)}`}
                  data-active={active ? "true" : "false"}
                  onClick={() => setNextValue(presetValue)}
                  title={preset.description}
                  className={cn(
                    "min-h-9 rounded-sm border px-2 py-1 text-left font-mono text-[11px] uppercase transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--artifact-purple)] focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                    active
                      ? "border-[var(--neural-cyan,#67e8f9)] bg-[var(--neural-cyan,#67e8f9)]/15 text-[var(--neural-cyan,#67e8f9)]"
                      : "border-[var(--neural-border,rgba(148,163,184,0.35))] bg-white/[0.03] text-[var(--muted-foreground,#94a3b8)] hover:border-[var(--neural-cyan,#67e8f9)]/65 hover:text-[var(--foreground,#e2e8f0)]",
                  )}
                >
                  <span className="block truncate">{preset.label}</span>
                  <span className="block text-[10px] tabular-nums opacity-75">
                    {valueFormatter(presetValue)}
                  </span>
                </button>
              )
            })}
          </div>
        ) : null}
      </div>
    </section>
  )
}

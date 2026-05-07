"use client"

/**
 * MP.W4.1 - Provider Constellation daily-mode shell.
 *
 * ADR-0007 defines the primary multi-provider planning surface as:
 * center Project Core, four corner provider Energy Spheres, connection
 * beams, and a Cheap/Fast tradeoff slider. This file owns only that
 * main view frame. The later MP.W4 tickets replace the render slots
 * with focused components without changing the page-level geometry.
 *
 * Module-global state audit: all module-level values are immutable
 * layout constants. The component is pure presentation over props and
 * keeps no React state.
 */

import type { ReactNode } from "react"
import {
  Activity,
  CircleDollarSign,
  Gauge,
  Network,
  Zap,
} from "lucide-react"

import { cn } from "@/lib/utils"

export type ProviderConstellationSlot =
  | "top-left"
  | "top-right"
  | "bottom-left"
  | "bottom-right"

export type ProviderConstellationQuotaState =
  | "healthy"
  | "watch"
  | "critical"
  | "unavailable"

export interface ProviderConstellationProvider {
  id: string
  name: string
  slot: ProviderConstellationSlot
  allocationPercent: number
  quotaState: ProviderConstellationQuotaState
  activityLevel?: number
}

export interface ProviderConstellationTaskSummary {
  title: string
  taskCount: number
  estimatedTokens: number
  estimatedCostUsd?: number
}

export interface ProviderConstellationProps {
  providers: ReadonlyArray<ProviderConstellationProvider>
  taskSummary: ProviderConstellationTaskSummary
  tradeoffValue: number
  className?: string
  renderProvider?: (provider: ProviderConstellationProvider) => ReactNode
  renderProjectCore?: (summary: ProviderConstellationTaskSummary) => ReactNode
  renderConnectionBeam?: (provider: ProviderConstellationProvider) => ReactNode
  renderTradeoffSlider?: (value: number) => ReactNode
}

const SLOT_CLASS: Record<ProviderConstellationSlot, string> = {
  "top-left": "left-3 top-3 sm:left-5 sm:top-5",
  "top-right": "right-3 top-3 sm:right-5 sm:top-5",
  "bottom-left": "bottom-20 left-3 sm:bottom-24 sm:left-5",
  "bottom-right": "bottom-20 right-3 sm:bottom-24 sm:right-5",
}

const QUOTA_CLASS: Record<ProviderConstellationQuotaState, string> = {
  healthy: "border-emerald-400/70 bg-emerald-400/10 text-emerald-200",
  watch: "border-amber-400/70 bg-amber-400/10 text-amber-200",
  critical: "border-rose-400/70 bg-rose-400/10 text-rose-200",
  unavailable: "border-slate-400/45 bg-slate-400/10 text-slate-300",
}

const QUOTA_LABEL: Record<ProviderConstellationQuotaState, string> = {
  healthy: "Healthy",
  watch: "Watch",
  critical: "Critical",
  unavailable: "No subscription",
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(100, Math.max(0, value))
}

function formatTokens(tokens: number): string {
  if (!Number.isFinite(tokens)) return "0"
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(2)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`
  return Math.max(0, Math.trunc(tokens)).toLocaleString()
}

function formatCost(cost: number | undefined): string {
  if (cost === undefined || !Number.isFinite(cost)) return "Estimate pending"
  return `$${cost.toFixed(cost >= 1 ? 2 : 3)}`
}

function DefaultProviderSphere({
  provider,
}: {
  provider: ProviderConstellationProvider
}) {
  const allocation = clampPercent(provider.allocationPercent)
  const activity = clampPercent((provider.activityLevel ?? 0) * 100)

  return (
    <div
      className={cn(
        "flex h-28 w-28 flex-col items-center justify-center rounded-full border p-3 text-center shadow-[0_0_28px_rgba(56,189,248,0.16)] sm:h-32 sm:w-32",
        QUOTA_CLASS[provider.quotaState],
      )}
    >
      <div className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.16em]">
        <Activity className="h-3 w-3" aria-hidden />
        <span>{QUOTA_LABEL[provider.quotaState]}</span>
      </div>
      <div className="mt-2 max-w-full truncate text-sm font-semibold">
        {provider.name}
      </div>
      <div className="mt-1 font-mono text-lg">{allocation.toFixed(0)}%</div>
      <div className="mt-1 h-1 w-16 overflow-hidden rounded-full bg-white/15">
        <div
          className="h-full rounded-full bg-current"
          style={{ width: `${activity}%` }}
        />
      </div>
    </div>
  )
}

function DefaultProjectCore({
  summary,
}: {
  summary: ProviderConstellationTaskSummary
}) {
  return (
    <div className="flex h-40 w-40 flex-col items-center justify-center rounded-full border border-[var(--neural-cyan,#67e8f9)]/65 bg-[var(--background,#020617)]/80 p-4 text-center shadow-[0_0_48px_rgba(103,232,249,0.22)] sm:h-48 sm:w-48">
      <Network className="h-7 w-7 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
      <h3 className="mt-3 max-w-full truncate text-sm font-semibold text-[var(--foreground,#e2e8f0)]">
        {summary.title}
      </h3>
      <div className="mt-2 grid grid-cols-2 gap-2 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
        <span>{summary.taskCount} TASKS</span>
        <span>{formatTokens(summary.estimatedTokens)} TOK</span>
      </div>
      <div className="mt-2 font-mono text-xs text-[var(--neural-cyan,#67e8f9)]">
        {formatCost(summary.estimatedCostUsd)}
      </div>
    </div>
  )
}

function DefaultConnectionBeam({
  provider,
}: {
  provider: ProviderConstellationProvider
}) {
  const allocation = clampPercent(provider.allocationPercent)
  return (
    <div
      className={cn(
        "pointer-events-none absolute hidden h-px origin-center bg-[var(--neural-cyan,#67e8f9)]/35 shadow-[0_0_16px_rgba(103,232,249,0.45)] lg:block",
        provider.slot === "top-left" && "left-[18%] top-[27%] w-[25%] rotate-[24deg]",
        provider.slot === "top-right" && "right-[18%] top-[27%] w-[25%] -rotate-[24deg]",
        provider.slot === "bottom-left" && "bottom-[30%] left-[18%] w-[25%] -rotate-[24deg]",
        provider.slot === "bottom-right" && "bottom-[30%] right-[18%] w-[25%] rotate-[24deg]",
      )}
      style={{ opacity: 0.18 + allocation / 140 }}
      aria-hidden
    />
  )
}

function DefaultTradeoffSlider({ value }: { value: number }) {
  const pct = clampPercent(value)
  return (
    <div className="flex w-full items-center gap-3 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)]/80 px-3 py-2">
      <CircleDollarSign className="h-4 w-4 text-emerald-300" aria-hidden />
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-white/10">
        <div
          className="h-full rounded-full bg-[var(--neural-cyan,#67e8f9)]"
          style={{ width: `${pct}%` }}
        />
      </div>
      <Gauge className="h-4 w-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
    </div>
  )
}

export function ProviderConstellation({
  providers,
  taskSummary,
  tradeoffValue,
  className,
  renderProvider,
  renderProjectCore,
  renderConnectionBeam,
  renderTradeoffSlider,
}: ProviderConstellationProps) {
  const orderedProviders = providers.slice(0, 4)

  return (
    <section
      className={cn(
        "holo-glass-simple corner-brackets-full relative min-h-[620px] overflow-hidden rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]",
        className,
      )}
      aria-label="Provider Constellation"
    >
      <header className="flex items-center justify-between border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Zap className="h-4 w-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="truncate font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            PROVIDER CONSTELLATION
          </h2>
        </div>
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
          Daily mode
        </span>
      </header>

      <div className="relative min-h-[560px] px-3 pb-20 pt-5 sm:px-5 sm:pb-24">
        <div
          className="absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(103,232,249,0.12),transparent_42%)]"
          aria-hidden
        />
        {orderedProviders.map((provider) => (
          <div key={`${provider.id}-beam`}>
            {renderConnectionBeam ? (
              renderConnectionBeam(provider)
            ) : (
              <DefaultConnectionBeam provider={provider} />
            )}
          </div>
        ))}

        <div className="absolute left-1/2 top-1/2 z-10 -translate-x-1/2 -translate-y-1/2">
          {renderProjectCore ? (
            renderProjectCore(taskSummary)
          ) : (
            <DefaultProjectCore summary={taskSummary} />
          )}
        </div>

        {orderedProviders.map((provider) => (
          <div
            key={provider.id}
            className={cn("absolute z-20", SLOT_CLASS[provider.slot])}
          >
            {renderProvider ? (
              renderProvider(provider)
            ) : (
              <DefaultProviderSphere provider={provider} />
            )}
          </div>
        ))}

        <div className="absolute inset-x-3 bottom-3 z-30 sm:inset-x-5 sm:bottom-5">
          <div className="mb-2 flex items-center justify-between font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
            <span>Cheap</span>
            <span>Fast</span>
          </div>
          {renderTradeoffSlider ? (
            renderTradeoffSlider(tradeoffValue)
          ) : (
            <DefaultTradeoffSlider value={tradeoffValue} />
          )}
        </div>
      </div>
    </section>
  )
}

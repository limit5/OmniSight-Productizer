"use client"

/**
 * MP.W6.3 - Cost calculator per-task breakdown.
 *
 * ADR-0007 defines the war-room Cost Calculator as the operator-facing
 * "$ vs time before dispatch" panel. This component owns only the
 * presentation and deterministic per-task aggregation for that panel.
 * The caller supplies task/provider estimates from the orchestrator.
 *
 * Module-global state audit: all module-level values are immutable
 * labels/classes. The component is pure presentation over props and
 * keeps no React state.
 */

import type { JSX } from "react"
import {
  Clock3,
  CircleDollarSign,
  Gauge,
  ListChecks,
  Route,
} from "lucide-react"

import { cn } from "@/lib/utils"

export type CostCalculatorStrategy = "cheap" | "balanced" | "fast"

export type CostCalculatorQuotaState =
  | "healthy"
  | "watch"
  | "critical"
  | "unavailable"

export interface CostCalculatorProviderOption {
  providerKey: string
  providerName: string
  estimatedCostUsd: number
  estimatedMinutes: number
  quotaState: CostCalculatorQuotaState
  recommended?: boolean
}

export interface CostCalculatorTaskEstimate {
  id: string
  title: string
  agentClass: string
  estimatedTokens: number
  options: readonly CostCalculatorProviderOption[]
}

export interface CostCalculatorTaskBreakdown {
  task: CostCalculatorTaskEstimate
  selected: CostCalculatorProviderOption | null
  cheapest: CostCalculatorProviderOption | null
  fastest: CostCalculatorProviderOption | null
  alternatives: readonly CostCalculatorProviderOption[]
}

export interface CostCalculatorTotals {
  estimatedCostUsd: number
  estimatedMinutes: number
  estimatedTokens: number
  pricedTaskCount: number
  totalTaskCount: number
  providerMix: readonly CostCalculatorProviderMix[]
}

export interface CostCalculatorProviderMix {
  providerKey: string
  providerName: string
  taskCount: number
  estimatedCostUsd: number
  estimatedMinutes: number
}

export interface CostCalculatorPanelProps {
  tasks: readonly CostCalculatorTaskEstimate[]
  strategy?: CostCalculatorStrategy
  className?: string
}

const STRATEGY_LABEL: Record<CostCalculatorStrategy, string> = {
  cheap: "Cheap",
  balanced: "Balanced",
  fast: "Fast",
}

const QUOTA_CLASS: Record<CostCalculatorQuotaState, string> = {
  healthy: "border-emerald-400/55 bg-emerald-400/10 text-emerald-200",
  watch: "border-amber-400/60 bg-amber-400/10 text-amber-200",
  critical: "border-rose-400/60 bg-rose-400/10 text-rose-200",
  unavailable: "border-slate-400/45 bg-slate-400/10 text-slate-300",
}

const QUOTA_LABEL: Record<CostCalculatorQuotaState, string> = {
  healthy: "Healthy",
  watch: "Watch",
  critical: "Critical",
  unavailable: "Unavailable",
}

function finiteOrZero(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0
}

function formatCost(cost: number): string {
  const safe = finiteOrZero(cost)
  if (safe >= 1) return `$${safe.toFixed(2)}`
  return `$${safe.toFixed(3)}`
}

function formatTokens(tokens: number): string {
  const safe = finiteOrZero(tokens)
  if (safe >= 1_000_000) return `${(safe / 1_000_000).toFixed(2)}M`
  if (safe >= 1_000) return `${(safe / 1_000).toFixed(1)}K`
  return Math.trunc(safe).toLocaleString()
}

function formatMinutes(minutes: number): string {
  const safe = finiteOrZero(minutes)
  if (safe >= 60) return `${(safe / 60).toFixed(1)}h`
  return `${Math.ceil(safe)}m`
}

function pickLowest(
  options: readonly CostCalculatorProviderOption[],
  field: "estimatedCostUsd" | "estimatedMinutes",
): CostCalculatorProviderOption | null {
  if (options.length === 0) return null
  return options.reduce((best, option) =>
    finiteOrZero(option[field]) < finiteOrZero(best[field]) ? option : best,
  )
}

function pickBalanced(
  options: readonly CostCalculatorProviderOption[],
): CostCalculatorProviderOption | null {
  if (options.length === 0) return null
  const recommended = options.find((option) => option.recommended)
  if (recommended) return recommended

  const maxCost = Math.max(
    ...options.map((option) => finiteOrZero(option.estimatedCostUsd)),
    0,
  )
  const maxMinutes = Math.max(
    ...options.map((option) => finiteOrZero(option.estimatedMinutes)),
    0,
  )
  return options.reduce((best, option) => {
    const optionScore =
      (maxCost > 0 ? finiteOrZero(option.estimatedCostUsd) / maxCost : 0) +
      (maxMinutes > 0 ? finiteOrZero(option.estimatedMinutes) / maxMinutes : 0)
    const bestScore =
      (maxCost > 0 ? finiteOrZero(best.estimatedCostUsd) / maxCost : 0) +
      (maxMinutes > 0 ? finiteOrZero(best.estimatedMinutes) / maxMinutes : 0)
    return optionScore < bestScore ? option : best
  })
}

function selectOption(
  options: readonly CostCalculatorProviderOption[],
  strategy: CostCalculatorStrategy,
): CostCalculatorProviderOption | null {
  if (strategy === "cheap") return pickLowest(options, "estimatedCostUsd")
  if (strategy === "fast") return pickLowest(options, "estimatedMinutes")
  return pickBalanced(options)
}

export function buildTaskCostBreakdown(
  tasks: readonly CostCalculatorTaskEstimate[],
  strategy: CostCalculatorStrategy = "balanced",
): CostCalculatorTaskBreakdown[] {
  return tasks.map((task) => {
    const selected = selectOption(task.options, strategy)
    return {
      task,
      selected,
      cheapest: pickLowest(task.options, "estimatedCostUsd"),
      fastest: pickLowest(task.options, "estimatedMinutes"),
      alternatives: task.options.filter((option) => option !== selected),
    }
  })
}

export function summarizeCostBreakdown(
  rows: readonly CostCalculatorTaskBreakdown[],
): CostCalculatorTotals {
  const providerMix = new Map<string, CostCalculatorProviderMix>()
  let estimatedCostUsd = 0
  let estimatedMinutes = 0
  let estimatedTokens = 0
  let pricedTaskCount = 0

  for (const row of rows) {
    estimatedTokens += finiteOrZero(row.task.estimatedTokens)
    if (!row.selected) continue
    estimatedCostUsd += finiteOrZero(row.selected.estimatedCostUsd)
    estimatedMinutes += finiteOrZero(row.selected.estimatedMinutes)
    pricedTaskCount += 1

    const existing = providerMix.get(row.selected.providerKey)
    if (existing) {
      existing.taskCount += 1
      existing.estimatedCostUsd += finiteOrZero(row.selected.estimatedCostUsd)
      existing.estimatedMinutes += finiteOrZero(row.selected.estimatedMinutes)
    } else {
      providerMix.set(row.selected.providerKey, {
        providerKey: row.selected.providerKey,
        providerName: row.selected.providerName,
        taskCount: 1,
        estimatedCostUsd: finiteOrZero(row.selected.estimatedCostUsd),
        estimatedMinutes: finiteOrZero(row.selected.estimatedMinutes),
      })
    }
  }

  return {
    estimatedCostUsd,
    estimatedMinutes,
    estimatedTokens,
    pricedTaskCount,
    totalTaskCount: rows.length,
    providerMix: [...providerMix.values()].sort(
      (a, b) => b.taskCount - a.taskCount,
    ),
  }
}

export function CostCalculatorPanel({
  tasks,
  strategy = "balanced",
  className,
}: CostCalculatorPanelProps): JSX.Element {
  const breakdown = buildTaskCostBreakdown(tasks, strategy)
  const totals = summarizeCostBreakdown(breakdown)

  return (
    <section
      data-testid="mp-cost-calculator-panel"
      className={cn(
        "holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]",
        className,
      )}
      aria-labelledby="mp-cost-calculator-title"
    >
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <CircleDollarSign
            className="h-4 w-4 text-[var(--hardware-orange,#fb923c)]"
            aria-hidden
          />
          <h2
            id="mp-cost-calculator-title"
            className="truncate font-mono text-sm tracking-wider text-[var(--hardware-orange,#fb923c)]"
          >
            COST CALCULATOR
          </h2>
        </div>
        <span className="rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
          {STRATEGY_LABEL[strategy]} routing
        </span>
      </header>

      <div className="space-y-3 p-3">
        <SummaryStrip totals={totals} />
        <ProviderMix totals={totals} />
        <TaskRows rows={breakdown} />
      </div>
    </section>
  )
}

function SummaryStrip({ totals }: { totals: CostCalculatorTotals }): JSX.Element {
  return (
    <div className="grid gap-2 sm:grid-cols-4" data-testid="mp-cost-summary">
      <SummaryTile
        icon={<CircleDollarSign className="h-4 w-4" aria-hidden />}
        label="EST. COST"
        value={formatCost(totals.estimatedCostUsd)}
      />
      <SummaryTile
        icon={<Clock3 className="h-4 w-4" aria-hidden />}
        label="WALL TIME"
        value={formatMinutes(totals.estimatedMinutes)}
      />
      <SummaryTile
        icon={<Gauge className="h-4 w-4" aria-hidden />}
        label="TOKENS"
        value={formatTokens(totals.estimatedTokens)}
      />
      <SummaryTile
        icon={<ListChecks className="h-4 w-4" aria-hidden />}
        label="TASKS"
        value={`${totals.pricedTaskCount}/${totals.totalTaskCount}`}
      />
    </div>
  )
}

function SummaryTile({
  icon,
  label,
  value,
}: {
  icon: JSX.Element
  label: string
  value: string
}): JSX.Element {
  return (
    <div className="rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--secondary,rgba(15,23,42,0.72))] p-2">
      <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--muted-foreground,#94a3b8)]">
        {icon}
        <span>{label}</span>
      </div>
      <div className="mt-1 font-mono text-base font-semibold tabular-nums text-[var(--foreground,#e2e8f0)]">
        {value}
      </div>
    </div>
  )
}

function ProviderMix({ totals }: { totals: CostCalculatorTotals }): JSX.Element {
  if (totals.providerMix.length === 0) {
    return (
      <div
        data-testid="mp-cost-provider-mix-empty"
        className="rounded-sm border border-dashed border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-4 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]"
      >
        No provider estimates available.
      </div>
    )
  }

  return (
    <div
      data-testid="mp-cost-provider-mix"
      className="rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)]/45 p-2"
    >
      <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--muted-foreground,#94a3b8)]">
        <Route className="h-3.5 w-3.5" aria-hidden />
        <span>Provider mix</span>
      </div>
      <div className="grid gap-1.5 sm:grid-cols-2">
        {totals.providerMix.map((item) => (
          <div
            key={item.providerKey}
            data-testid={`mp-cost-provider-mix-${item.providerKey}`}
            className="flex min-w-0 items-center gap-2 rounded-sm bg-white/[0.03] px-2 py-1.5 font-mono text-[11px]"
          >
            <span className="truncate text-[var(--foreground,#e2e8f0)]">
              {item.providerName}
            </span>
            <span className="ml-auto shrink-0 text-[var(--muted-foreground,#94a3b8)]">
              {item.taskCount} tasks
            </span>
            <span className="shrink-0 tabular-nums text-[var(--hardware-orange,#fb923c)]">
              {formatCost(item.estimatedCostUsd)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function TaskRows({
  rows,
}: {
  rows: readonly CostCalculatorTaskBreakdown[]
}): JSX.Element {
  if (rows.length === 0) {
    return (
      <div
        data-testid="mp-cost-task-breakdown-empty"
        className="rounded-sm border border-dashed border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-6 text-center font-mono text-xs text-[var(--muted-foreground,#94a3b8)]"
      >
        Add tasks to estimate dispatch cost.
      </div>
    )
  }

  return (
    <ul
      data-testid="mp-cost-task-breakdown"
      className="m-0 list-none space-y-2 p-0"
    >
      {rows.map((row) => (
        <TaskRow key={row.task.id} row={row} />
      ))}
    </ul>
  )
}

function TaskRow({ row }: { row: CostCalculatorTaskBreakdown }): JSX.Element {
  const selected = row.selected
  const isCheapest = selected && row.cheapest === selected
  const isFastest = selected && row.fastest === selected

  return (
    <li
      data-testid={`mp-cost-task-row-${row.task.id}`}
      className="rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--secondary,rgba(15,23,42,0.72))] p-3"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="min-w-0 break-words text-sm font-semibold text-[var(--foreground,#e2e8f0)]">
              {row.task.title}
            </h3>
            <span className="rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--muted-foreground,#94a3b8)]">
              {row.task.agentClass}
            </span>
          </div>
          <div className="mt-1 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
            {formatTokens(row.task.estimatedTokens)} tokens
          </div>
        </div>

        {selected ? (
          <div className="grid min-w-[210px] grid-cols-3 gap-2 font-mono text-[11px]">
            <Metric label="Provider" value={selected.providerName} />
            <Metric label="Cost" value={formatCost(selected.estimatedCostUsd)} />
            <Metric label="Time" value={formatMinutes(selected.estimatedMinutes)} />
          </div>
        ) : (
          <span className="rounded-sm border border-dashed border-[var(--neural-border,rgba(148,163,184,0.35))] px-2 py-1 font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)]">
            Estimate pending
          </span>
        )}
      </div>

      {selected ? (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <QuotaBadge state={selected.quotaState} />
          {isCheapest ? <FlagBadge label="Cheapest" /> : null}
          {isFastest ? <FlagBadge label="Fastest" /> : null}
          {row.alternatives.slice(0, 3).map((option) => (
            <span
              key={option.providerKey}
              data-testid={`mp-cost-task-row-${row.task.id}-alt-${option.providerKey}`}
              className="rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]"
            >
              {option.providerName}: {formatCost(option.estimatedCostUsd)} /{" "}
              {formatMinutes(option.estimatedMinutes)}
            </span>
          ))}
        </div>
      ) : null}
    </li>
  )
}

function Metric({
  label,
  value,
}: {
  label: string
  value: string
}): JSX.Element {
  return (
    <div className="min-w-0 rounded-sm bg-white/[0.04] px-2 py-1">
      <div className="truncate text-[9px] uppercase tracking-[0.12em] text-[var(--muted-foreground,#94a3b8)]">
        {label}
      </div>
      <div className="truncate tabular-nums text-[var(--foreground,#e2e8f0)]">
        {value}
      </div>
    </div>
  )
}

function QuotaBadge({
  state,
}: {
  state: CostCalculatorQuotaState
}): JSX.Element {
  return (
    <span
      data-testid={`mp-cost-quota-${state}`}
      className={cn(
        "rounded-sm border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em]",
        QUOTA_CLASS[state],
      )}
    >
      {QUOTA_LABEL[state]}
    </span>
  )
}

function FlagBadge({ label }: { label: string }): JSX.Element {
  return (
    <span className="rounded-sm border border-[var(--neural-cyan,#67e8f9)]/55 bg-[var(--neural-cyan,#67e8f9)]/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--neural-cyan,#67e8f9)]">
      {label}
    </span>
  )
}

"use client"

import { useEffect } from "react"
import { CircleDollarSign, X } from "lucide-react"

import {
  Drawer,
  DrawerClose,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from "@/components/ui/drawer"
import type { ProviderConstellationProvider } from "./ProviderConstellation"

export interface CostBreakdownDrawerProps {
  open: boolean
  onClose: () => void
  providers: ProviderConstellationProvider[]
}

type ProviderCostSnapshot = ProviderConstellationProvider & {
  tokensIn?: number
  inputTokens?: number
  promptTokens?: number
  tokensOut?: number
  outputTokens?: number
  completionTokens?: number
  totalUsd?: number
  costUsd?: number
  totalCostUsd?: number
  estimatedCostUsd?: number
}

interface CostBreakdownRow {
  id: string
  name: string
  tokensIn: number
  tokensOut: number
  totalUsd: number
}

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
})
const TOKENS = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 })
const metricClass = "justify-self-end whitespace-nowrap tabular-nums"
const metricAccentClass = `${metricClass} text-[var(--hardware-orange,#fb923c)]`
const metricTextClass = `${metricClass} text-[var(--foreground,#e2e8f0)]`

function finiteOrZero(value: number | undefined): number {
  return Number.isFinite(value) ? Math.max(0, value as number) : 0
}

function firstFinite(...values: Array<number | undefined>): number {
  return finiteOrZero(values.find((value) => Number.isFinite(value)))
}

function formatTokens(tokens: number): string {
  return TOKENS.format(Math.trunc(finiteOrZero(tokens)))
}

function formatUsd(totalUsd: number): string {
  return USD.format(finiteOrZero(totalUsd))
}

function toCostRow(provider: ProviderConstellationProvider): CostBreakdownRow {
  const snapshot = provider as ProviderCostSnapshot
  return {
    id: provider.id,
    name: provider.name,
    tokensIn: firstFinite(snapshot.tokensIn, snapshot.inputTokens, snapshot.promptTokens),
    tokensOut: firstFinite(snapshot.tokensOut, snapshot.outputTokens, snapshot.completionTokens),
    totalUsd: firstFinite(snapshot.totalUsd, snapshot.costUsd, snapshot.totalCostUsd, snapshot.estimatedCostUsd),
  }
}

export function CostBreakdownDrawer({
  open,
  onClose,
  providers,
}: CostBreakdownDrawerProps) {
  useEffect(() => {
    if (!open) return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [onClose, open])

  const rows = providers.map(toCostRow)
  const totals = rows.reduce(
    (acc, row) => ({
      tokensIn: acc.tokensIn + row.tokensIn,
      tokensOut: acc.tokensOut + row.tokensOut,
      totalUsd: acc.totalUsd + row.totalUsd,
    }),
    { tokensIn: 0, tokensOut: 0, totalUsd: 0 },
  )

  return (
    <Drawer
      direction="right"
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) onClose()
      }}
    >
      <DrawerContent
        className="w-[min(92vw,28rem)] border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)] text-[var(--foreground,#e2e8f0)]"
        data-testid="mp-cost-breakdown-drawer"
        aria-describedby="mp-cost-breakdown-description"
      >
        <DrawerHeader
          className="border-b border-[var(--neural-border,rgba(148,163,184,0.35))]"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <DrawerTitle className="flex items-center gap-2 font-mono text-sm tracking-wider text-[var(--hardware-orange,#fb923c)]">
                <CircleDollarSign className="size-4 shrink-0" aria-hidden />
                COST BREAKDOWN
              </DrawerTitle>
              <DrawerDescription
                id="mp-cost-breakdown-description"
                className="mt-1 text-xs text-[var(--muted-foreground,#94a3b8)]"
              >
                Current session by provider
              </DrawerDescription>
            </div>
            <DrawerClose
              className="grid size-8 shrink-0 place-items-center rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] text-[var(--muted-foreground,#94a3b8)] transition-colors hover:text-[var(--foreground,#e2e8f0)]"
              aria-label="Close cost breakdown"
            >
              <X className="size-4" aria-hidden />
            </DrawerClose>
          </div>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          <div
            className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] gap-x-3 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] pb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--muted-foreground,#94a3b8)]"
          >
            <span>Provider</span>
            <span className="text-right">In</span>
            <span className="text-right">Out</span>
            <span className="text-right">USD</span>
          </div>

          <div className="divide-y divide-[var(--neural-border,rgba(148,163,184,0.22))]">
            {rows.map((row) => (
              <div
                key={row.id}
                data-testid={`mp-cost-breakdown-row-${row.id}`}
                className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] items-center gap-x-3 py-3 text-sm"
              >
                <span className="min-w-0 truncate font-medium">{row.name}</span>
                <Metric value={formatTokens(row.tokensIn)} />
                <Metric value={formatTokens(row.tokensOut)} />
                <Metric value={formatUsd(row.totalUsd)} accent />
              </div>
            ))}
          </div>
        </div>

        <footer
          data-testid="mp-cost-breakdown-sum-row"
          className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] gap-x-3 border-t border-[var(--hardware-orange,#fb923c)]/45 px-4 py-3 font-mono text-sm"
        >
          <span className="font-semibold text-[var(--hardware-orange,#fb923c)]">
            Total
          </span>
          <Metric value={formatTokens(totals.tokensIn)} />
          <Metric value={formatTokens(totals.tokensOut)} />
          <Metric value={formatUsd(totals.totalUsd)} accent />
        </footer>
      </DrawerContent>
    </Drawer>
  )
}

function Metric({ value, accent }: { value: string; accent?: boolean }) {
  return <span className={accent ? metricAccentClass : metricTextClass}>{value}</span>
}

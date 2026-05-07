"use client"

/**
 * MP.W6.2 - quota tracker panel with reset countdown.
 *
 * Mirrors the existing provider quota primitives instead of inventing a
 * second threshold model: computeProviderStatus owns tier selection, and
 * this panel expands that state into a war-room summary plus per-provider
 * reset countdown rows.
 *
 * Module-global state audit: module-level values are immutable labels and
 * style maps. The live countdown state is component-local and only ticks
 * while at least one future reset deadline is visible.
 */

import { useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  HelpCircle,
  RefreshCw,
} from "lucide-react"

import { cn } from "@/lib/utils"
import {
  computeProviderStatus,
  describeProviderStatus,
  ProviderStatusBadge,
  type ProviderStatusBadgeProps,
  type ProviderStatusResult,
  type ProviderStatusTier,
} from "@/components/omnisight/provider-status-badge"

export type QuotaTrackerPanelStatus = "ok" | "unsupported" | "error"

export type QuotaTrackerPanelQuota = Pick<
  ProviderStatusBadgeProps,
  | "status"
  | "reason"
  | "balanceRemaining"
  | "grantedTotal"
  | "currency"
  | "rateLimitRemainingRequests"
  | "rateLimitRemainingTokens"
  | "rateLimitLimitRequests"
  | "rateLimitLimitTokens"
  | "resetAtTs"
  | "loading"
>

export interface QuotaTrackerPanelProvider extends QuotaTrackerPanelQuota {
  id: string
  name: string
  retryAfterS?: number | null
  lastRefreshedAt?: number | null
}

export interface QuotaTrackerPanelProviderState {
  provider: QuotaTrackerPanelProvider
  result: ProviderStatusResult
  resetLabel: string
  resetSeconds: number | null
  balanceLabel: string
  requestLabel: string
  tokenLabel: string
  statusLabel: string
}

export interface QuotaTrackerPanelSummary {
  total: number
  healthy: number
  watch: number
  critical: number
  unavailable: number
  nextResetLabel: string
}

export interface QuotaTrackerPanelProps {
  providers: ReadonlyArray<QuotaTrackerPanelProvider>
  nowTs?: number
  className?: string
}

const TIER_LABEL: Record<ProviderStatusTier, string> = {
  green: "Healthy",
  yellow: "Watch",
  red: "Critical",
  gray: "Unavailable",
}

const TIER_CLASS: Record<ProviderStatusTier, string> = {
  green: "border-emerald-400/55 bg-emerald-400/10 text-emerald-200",
  yellow: "border-amber-400/60 bg-amber-400/10 text-amber-200",
  red: "border-rose-400/65 bg-rose-400/10 text-rose-200",
  gray: "border-slate-400/40 bg-slate-400/10 text-slate-300",
}

const TIER_ICON: Record<ProviderStatusTier, typeof CheckCircle2> = {
  green: CheckCircle2,
  yellow: AlertTriangle,
  red: AlertTriangle,
  gray: HelpCircle,
}

function finiteNumber(value: number | null | undefined): value is number {
  return value !== null && value !== undefined && Number.isFinite(value)
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(100, Math.max(0, value))
}

function formatCurrency(
  amount: number,
  currency: string | null | undefined,
): string {
  const prefix = currency === "CNY" ? "CNY " : "$"
  if (Math.abs(amount) >= 1) return `${prefix}${amount.toFixed(2)}`
  return `${prefix}${amount.toFixed(3)}`
}

function formatInteger(value: number | null | undefined): string {
  if (!finiteNumber(value)) return "-"
  return Math.trunc(value).toLocaleString()
}

function formatPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "-"
  return `${clampPercent(value).toFixed(0)}%`
}

export function formatQuotaResetCountdown(
  resetAtTs: number | null | undefined,
  retryAfterS: number | null | undefined,
  nowTs: number,
): { label: string; seconds: number | null } {
  if (finiteNumber(resetAtTs)) {
    const delta = resetAtTs - nowTs
    if (delta <= 0) return { label: "reset due", seconds: 0 }
    const seconds = Math.ceil(delta)
    if (seconds < 60) return { label: `${seconds}s`, seconds }
    if (seconds < 3600) {
      const minutes = Math.floor(seconds / 60)
      const rest = seconds % 60
      return { label: `${minutes}m ${rest.toString().padStart(2, "0")}s`, seconds }
    }
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    return { label: `${hours}h ${minutes.toString().padStart(2, "0")}m`, seconds }
  }

  if (finiteNumber(retryAfterS) && retryAfterS > 0) {
    const seconds = Math.ceil(retryAfterS)
    if (seconds < 60) return { label: `retry ~${seconds}s`, seconds }
    if (seconds < 3600) return { label: `retry ~${Math.ceil(seconds / 60)}m`, seconds }
    return { label: `retry ~${Math.ceil(seconds / 3600)}h`, seconds }
  }

  return { label: "reset unknown", seconds: null }
}

function formatBalance(provider: QuotaTrackerPanelProvider): string {
  if (!finiteNumber(provider.balanceRemaining)) return "-"
  const remaining = formatCurrency(provider.balanceRemaining, provider.currency)
  if (finiteNumber(provider.grantedTotal) && provider.grantedTotal > 0) {
    return `${remaining} / ${formatCurrency(provider.grantedTotal, provider.currency)}`
  }
  return remaining
}

function computeProviderState(
  provider: QuotaTrackerPanelProvider,
  nowTs: number,
): QuotaTrackerPanelProviderState {
  const statusProps: ProviderStatusBadgeProps = {
    provider: provider.name,
    status: provider.status,
    reason: provider.reason,
    balanceRemaining: provider.balanceRemaining,
    grantedTotal: provider.grantedTotal,
    currency: provider.currency,
    rateLimitRemainingRequests: provider.rateLimitRemainingRequests,
    rateLimitRemainingTokens: provider.rateLimitRemainingTokens,
    rateLimitLimitRequests: provider.rateLimitLimitRequests,
    rateLimitLimitTokens: provider.rateLimitLimitTokens,
    resetAtTs: provider.resetAtTs,
    loading: provider.loading,
  }
  const result = computeProviderStatus(statusProps)
  const reset = formatQuotaResetCountdown(
    provider.resetAtTs,
    provider.retryAfterS,
    nowTs,
  )

  return {
    provider,
    result,
    resetLabel: reset.label,
    resetSeconds: reset.seconds,
    balanceLabel: formatBalance(provider),
    requestLabel: formatInteger(provider.rateLimitRemainingRequests),
    tokenLabel: formatInteger(provider.rateLimitRemainingTokens),
    statusLabel: describeProviderStatus(statusProps, result),
  }
}

export function computeQuotaTrackerPanelState(
  providers: ReadonlyArray<QuotaTrackerPanelProvider>,
  nowTs: number,
): {
  providers: QuotaTrackerPanelProviderState[]
  summary: QuotaTrackerPanelSummary
} {
  const providerStates = providers.map((provider) =>
    computeProviderState(provider, nowTs),
  )
  const nextReset = providerStates
    .map((state) => state.resetSeconds)
    .filter((seconds): seconds is number => seconds !== null && seconds > 0)
    .sort((a, b) => a - b)[0]

  return {
    providers: providerStates,
    summary: {
      total: providerStates.length,
      healthy: providerStates.filter((state) => state.result.tier === "green").length,
      watch: providerStates.filter((state) => state.result.tier === "yellow").length,
      critical: providerStates.filter((state) => state.result.tier === "red").length,
      unavailable: providerStates.filter((state) => state.result.tier === "gray").length,
      nextResetLabel:
        nextReset === undefined
          ? "none scheduled"
          : formatQuotaResetCountdown(nowTs + nextReset, null, nowTs).label,
    },
  }
}

export function QuotaTrackerPanel({
  providers,
  nowTs,
  className,
}: QuotaTrackerPanelProps) {
  const [tickTs, setTickTs] = useState(() => nowTs ?? Date.now() / 1000)
  const displayNow = nowTs ?? tickTs
  const state = useMemo(
    () => computeQuotaTrackerPanelState(providers, displayNow),
    [providers, displayNow],
  )
  const hasActiveCountdown = state.providers.some(
    (provider) => provider.resetSeconds !== null && provider.resetSeconds > 0,
  )

  useEffect(() => {
    if (nowTs !== undefined || !hasActiveCountdown) return
    const timer = window.setInterval(() => {
      setTickTs(Date.now() / 1000)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [hasActiveCountdown, nowTs])

  return (
    <section
      className={cn(
        "holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]",
        className,
      )}
      data-testid="mp-quota-tracker-panel"
      aria-label="Provider quota tracker"
    >
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <RefreshCw
            className={cn(
              "h-4 w-4 text-[var(--neural-cyan,#67e8f9)]",
              hasActiveCountdown && "animate-spin",
            )}
            aria-hidden
          />
          <h2 className="truncate font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            QUOTA TRACKER
          </h2>
        </div>
        <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
          <Clock3 className="h-3.5 w-3.5" aria-hidden />
          <span data-testid="mp-quota-tracker-next-reset">
            Next reset {state.summary.nextResetLabel}
          </span>
        </div>
      </header>

      <div className="grid gap-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-3 sm:grid-cols-4">
        <SummaryCell label="Healthy" value={state.summary.healthy} tone="green" />
        <SummaryCell label="Watch" value={state.summary.watch} tone="yellow" />
        <SummaryCell label="Critical" value={state.summary.critical} tone="red" />
        <SummaryCell label="Unavailable" value={state.summary.unavailable} tone="gray" />
      </div>

      <div className="divide-y divide-[var(--neural-border,rgba(148,163,184,0.22))]">
        {state.providers.length === 0 ? (
          <div
            className="px-3 py-6 text-center font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]"
            data-testid="mp-quota-tracker-empty"
          >
            No subscribed providers
          </div>
        ) : (
          state.providers.map((providerState) => (
            <ProviderQuotaRow
              key={providerState.provider.id}
              state={providerState}
            />
          ))
        )}
      </div>
    </section>
  )
}

function SummaryCell({
  label,
  value,
  tone,
}: {
  label: string
  value: number
  tone: ProviderStatusTier
}) {
  return (
    <div
      className={cn("rounded-sm border px-3 py-2", TIER_CLASS[tone])}
      data-testid={`mp-quota-tracker-summary-${tone}`}
    >
      <div className="font-mono text-[10px] uppercase tracking-[0.16em] opacity-80">
        {label}
      </div>
      <div className="mt-1 font-mono text-xl leading-none">{value}</div>
    </div>
  )
}

function ProviderQuotaRow({
  state,
}: {
  state: QuotaTrackerPanelProviderState
}) {
  const { provider, result } = state
  const Icon = TIER_ICON[result.tier]
  const balancePct = formatPercent(result.balancePct)
  const ratePct = formatPercent(result.rateLimitRemainingPct)

  return (
    <article
      className="grid gap-3 px-3 py-3 sm:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(8rem,0.8fr)] sm:items-center"
      data-testid={`mp-quota-tracker-provider-${provider.id}`}
      data-provider={provider.id}
      data-tier={result.tier}
      aria-label={state.statusLabel}
      title={state.statusLabel}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-sm border",
            TIER_CLASS[result.tier],
          )}
          aria-hidden
        >
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <div className="truncate font-mono text-sm font-semibold text-[var(--foreground,#e2e8f0)]">
            {provider.name}
          </div>
          <div className="mt-1 flex items-center gap-2">
            <ProviderStatusBadge
              provider={provider.name}
              status={provider.status}
              reason={provider.reason}
              balanceRemaining={provider.balanceRemaining}
              grantedTotal={provider.grantedTotal}
              currency={provider.currency}
              rateLimitRemainingRequests={provider.rateLimitRemainingRequests}
              rateLimitRemainingTokens={provider.rateLimitRemainingTokens}
              rateLimitLimitRequests={provider.rateLimitLimitRequests}
              rateLimitLimitTokens={provider.rateLimitLimitTokens}
              resetAtTs={provider.resetAtTs}
              loading={provider.loading}
            />
            <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
              {TIER_LABEL[result.tier]}
            </span>
          </div>
        </div>
      </div>

      <QuotaMetric
        label="Balance"
        value={state.balanceLabel}
        detail={balancePct}
      />
      <QuotaMetric
        label="Rate-limit"
        value={`${state.requestLabel} req / ${state.tokenLabel} tok`}
        detail={ratePct}
      />
      <QuotaMetric
        label="Reset"
        value={state.resetLabel}
        detail={
          state.resetSeconds === null
            ? "no deadline"
            : state.resetSeconds === 0
              ? "due now"
              : "countdown"
        }
        alignRight
      />
    </article>
  )
}

function QuotaMetric({
  label,
  value,
  detail,
  alignRight = false,
}: {
  label: string
  value: string
  detail: string
  alignRight?: boolean
}) {
  return (
    <div className={cn("min-w-0", alignRight && "sm:text-right")}>
      <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
        {label}
      </div>
      <div className="mt-1 truncate font-mono text-[12px] text-[var(--foreground,#e2e8f0)]">
        {value}
      </div>
      <div className="mt-0.5 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
        {detail}
      </div>
    </div>
  )
}

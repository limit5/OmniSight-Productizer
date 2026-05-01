"use client"

/**
 * AB.6.6 — Cost dashboard.
 *
 * Three sections:
 *   1. Spend snapshots per scope — current daily / monthly vs caps
 *   2. Recent alerts — warn_80 / cap_100 / over_120 stream
 *   3. Budget configuration — list of operator-configured caps
 *
 * Pure presentation. Caller wires the data via /api/v1/cost/usage,
 * /api/v1/cost/alerts, /api/v1/cost/budgets endpoints (60s poll).
 *
 * Backend contract: CostSnapshot / BudgetCap / BudgetAlert defined in
 * `backend/agents/cost_guard.py`, mirrored in `./types.ts`.
 */

import {
  AlertCircle,
  AlertTriangle,
  Check,
  XCircle,
} from "lucide-react"
import {
  type AlertLevel,
  type BudgetAlert,
  type BudgetCap,
  type CostSnapshot,
  formatDateRelative,
  formatPercent,
  formatUsd,
} from "./types"

const ALERT_ICON: Record<AlertLevel, JSX.Element> = {
  warn_80: <AlertTriangle size={14} className="text-yellow-600" aria-hidden />,
  cap_100: <AlertCircle size={14} className="text-orange-600" aria-hidden />,
  over_120: <XCircle size={14} className="text-red-600" aria-hidden />,
}

const ALERT_LABEL: Record<AlertLevel, string> = {
  warn_80: "80% reached",
  cap_100: "Cap hit",
  over_120: "Over 120%",
}

const ALERT_BG: Record<AlertLevel, string> = {
  warn_80: "bg-yellow-50 border-yellow-200",
  cap_100: "bg-orange-50 border-orange-200",
  over_120: "bg-red-50 border-red-200",
}

export interface CostDashboardPanelProps {
  snapshots: CostSnapshot[]
  alerts: BudgetAlert[]
  budgets: BudgetCap[]
  onConfigureBudget?: (scopeKindKey: string) => void
}

export function CostDashboardPanel(
  props: CostDashboardPanelProps,
): JSX.Element {
  const { snapshots, alerts, budgets, onConfigureBudget } = props
  return (
    <section data-testid="cost-dashboard-panel" className="space-y-4">
      <h2 className="text-lg font-semibold">Cost Observability</h2>

      <SnapshotsTable snapshots={snapshots} />

      <AlertsList alerts={alerts} />

      <BudgetsTable
        budgets={budgets}
        onConfigure={onConfigureBudget}
      />
    </section>
  )
}

// ─── Snapshots ───────────────────────────────────────────────────

function SnapshotsTable({
  snapshots,
}: {
  snapshots: CostSnapshot[]
}): JSX.Element {
  if (snapshots.length === 0) {
    return (
      <p data-testid="cost-snapshots-empty" className="text-sm text-gray-500">
        No spend recorded yet.
      </p>
    )
  }
  return (
    <div data-testid="cost-snapshots" className="overflow-x-auto">
      <h3 className="mb-2 text-sm font-medium text-gray-700">
        Current spend
      </h3>
      <table className="w-full text-xs">
        <thead className="border-b border-gray-200 text-gray-500">
          <tr>
            <th className="text-left">Scope</th>
            <th className="text-right">Daily</th>
            <th className="text-right">Daily cap</th>
            <th className="text-right">Monthly</th>
            <th className="text-right">Monthly cap</th>
          </tr>
        </thead>
        <tbody>
          {snapshots.map((snap) => (
            <SnapshotRow
              key={`${snap.scope.kind}::${snap.scope.key}`}
              snap={snap}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function SnapshotRow({ snap }: { snap: CostSnapshot }): JSX.Element {
  const cap = snap.budget
  const dailyUsage = cap?.daily_limit_usd
    ? snap.spend_today_usd / cap.daily_limit_usd
    : 0
  const dailyClass = capUtilClass(dailyUsage)
  const monthlyUsage = cap?.monthly_limit_usd
    ? snap.spend_month_usd / cap.monthly_limit_usd
    : 0
  const monthlyClass = capUtilClass(monthlyUsage)
  return (
    <tr
      data-testid={`cost-snapshot-${snap.scope.kind}-${snap.scope.key}`}
      className="border-b border-gray-100"
    >
      <td className="py-1">
        <span className="text-gray-400">{snap.scope.kind}=</span>
        <span className="font-medium">{snap.scope.key}</span>
      </td>
      <td className={`py-1 text-right ${dailyClass}`}>
        {formatUsd(snap.spend_today_usd)}
      </td>
      <td className="py-1 text-right text-gray-500">
        {cap?.daily_limit_usd != null
          ? `${formatUsd(cap.daily_limit_usd)} (${formatPercent(snap.spend_today_usd, cap.daily_limit_usd)})`
          : "—"}
      </td>
      <td className={`py-1 text-right ${monthlyClass}`}>
        {formatUsd(snap.spend_month_usd)}
      </td>
      <td className="py-1 text-right text-gray-500">
        {cap?.monthly_limit_usd != null
          ? `${formatUsd(cap.monthly_limit_usd)} (${formatPercent(snap.spend_month_usd, cap.monthly_limit_usd)})`
          : "—"}
      </td>
    </tr>
  )
}

function capUtilClass(ratio: number): string {
  if (ratio >= 1.2) return "font-semibold text-red-600"
  if (ratio >= 1.0) return "font-semibold text-orange-600"
  if (ratio >= 0.8) return "font-medium text-yellow-700"
  return ""
}

// ─── Alerts ──────────────────────────────────────────────────────

function AlertsList({ alerts }: { alerts: BudgetAlert[] }): JSX.Element {
  if (alerts.length === 0) {
    return (
      <p data-testid="cost-alerts-empty" className="text-sm text-gray-500">
        No active budget alerts.
      </p>
    )
  }
  return (
    <div data-testid="cost-alerts">
      <h3 className="mb-2 text-sm font-medium text-gray-700">Recent alerts</h3>
      <ul className="space-y-1">
        {alerts.map((a) => (
          <li
            key={a.alert_id}
            data-testid={`cost-alert-${a.level}`}
            className={`flex items-start gap-2 rounded border px-2 py-1 text-xs ${ALERT_BG[a.level]}`}
          >
            {ALERT_ICON[a.level]}
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{ALERT_LABEL[a.level]}</span>
                <span className="text-gray-500">
                  {a.scope.kind}={a.scope.key} · {a.period}
                </span>
                <span className="ml-auto text-gray-400">
                  {formatDateRelative(a.fired_at)}
                </span>
              </div>
              <div className="text-gray-700">
                {formatUsd(a.observed_usd)} / {formatUsd(a.threshold_usd)} —
                action <span className="font-medium">{a.action}</span>
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ─── Budgets ─────────────────────────────────────────────────────

function BudgetsTable({
  budgets,
  onConfigure,
}: {
  budgets: BudgetCap[]
  onConfigure?: (scopeKindKey: string) => void
}): JSX.Element {
  return (
    <div data-testid="cost-budgets">
      <h3 className="mb-2 text-sm font-medium text-gray-700">
        Configured budgets ({budgets.filter((b) => b.enabled).length} enabled)
      </h3>
      {budgets.length === 0 ? (
        <p data-testid="cost-budgets-empty" className="text-xs text-gray-500">
          No budgets configured. All spend is tracked but not capped.
        </p>
      ) : (
        <table className="w-full text-xs">
          <thead className="border-b border-gray-200 text-gray-500">
            <tr>
              <th className="text-left">Scope</th>
              <th className="text-right">Per-batch</th>
              <th className="text-right">Daily</th>
              <th className="text-right">Monthly</th>
              <th className="text-center">Status</th>
            </tr>
          </thead>
          <tbody>
            {budgets.map((b) => (
              <tr
                key={`${b.scope.kind}::${b.scope.key}`}
                data-testid={`cost-budget-${b.scope.kind}-${b.scope.key}`}
                className={
                  b.enabled
                    ? "border-b border-gray-100"
                    : "border-b border-gray-100 text-gray-400"
                }
              >
                <td className="py-1">
                  <button
                    type="button"
                    onClick={() =>
                      onConfigure?.(`${b.scope.kind}::${b.scope.key}`)
                    }
                    className="hover:underline"
                  >
                    <span className="text-gray-400">{b.scope.kind}=</span>
                    <span className="font-medium">{b.scope.key}</span>
                  </button>
                </td>
                <td className="py-1 text-right">
                  {b.per_batch_limit_usd != null
                    ? formatUsd(b.per_batch_limit_usd)
                    : "—"}
                </td>
                <td className="py-1 text-right">
                  {b.daily_limit_usd != null
                    ? formatUsd(b.daily_limit_usd)
                    : "—"}
                </td>
                <td className="py-1 text-right">
                  {b.monthly_limit_usd != null
                    ? formatUsd(b.monthly_limit_usd)
                    : "—"}
                </td>
                <td className="py-1 text-center">
                  {b.enabled ? (
                    <Check
                      size={14}
                      className="inline text-green-600"
                      aria-label="enabled"
                    />
                  ) : (
                    <span aria-label="disabled">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

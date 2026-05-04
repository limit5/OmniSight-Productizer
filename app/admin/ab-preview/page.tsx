"use client"

/**
 * AB.* preview — render the four deferred panels with realistic mock
 * data so operator can see them before backend FastAPI endpoints wire up.
 *
 * Not a production route — mounted only for visual / interaction
 * verification. Data is hand-curated to exercise each panel's
 * empty / partial / busy states on one page.
 */

import { type JSX, useState } from "react"

import { BatchProgressPanel } from "@/components/omnisight/ab/batch-progress-panel"
import { CostDashboardPanel } from "@/components/omnisight/ab/cost-dashboard-panel"
import {
  BatchEligibilityBadge,
  BatchEligibilityPanel,
} from "@/components/omnisight/ab/batch-eligibility-panel"
import { ProviderModeWizard } from "@/components/omnisight/ab/provider-mode-wizard"
import type {
  BatchRun,
  BudgetAlert,
  BudgetCap,
  CostSnapshot,
  DispatcherStats,
  EligibilityRule,
  RoutingDecision,
  WizardState,
  WizardStep,
} from "@/components/omnisight/ab/types"


// ─── Mock data ────────────────────────────────────────────────────


const NOW = Date.now()
const MIN = 60_000
const HOUR = 60 * MIN

const MOCK_RUNS: BatchRun[] = [
  {
    batch_run_id: "br_a1b2c3d4e5f6",
    status: "submitted",
    request_count: 100,
    success_count: 73,
    error_count: 2,
    canceled_count: 0,
    expired_count: 0,
    anthropic_batch_id: "msgbatch_01HFG7KWQM",
    submitted_at: new Date(NOW - 12 * MIN).toISOString(),
    expires_at: new Date(NOW + 23 * HOUR + 48 * MIN).toISOString(),
    metadata: { phase: "HD.1", task_kind: "hd_parse_kicad" },
    created_by: "agent-software-beta",
    created_at: new Date(NOW - 13 * MIN).toISOString(),
  },
  {
    batch_run_id: "br_9876543210ab",
    status: "ended",
    request_count: 50,
    success_count: 48,
    error_count: 1,
    canceled_count: 0,
    expired_count: 1,
    anthropic_batch_id: "msgbatch_01HFG6PXNZ",
    submitted_at: new Date(NOW - 4 * HOUR).toISOString(),
    ended_at: new Date(NOW - 3 * HOUR + 22 * MIN).toISOString(),
    metadata: { phase: "L4.3", task_kind: "l4_adversarial_ci" },
    created_by: "nightly-cron",
    created_at: new Date(NOW - 4 * HOUR - MIN).toISOString(),
  },
  {
    batch_run_id: "br_failedbatchz",
    status: "failed",
    request_count: 25,
    success_count: 0,
    error_count: 0,
    canceled_count: 0,
    expired_count: 0,
    metadata: {
      phase: "HD.5.13",
      submit_error: "anthropic 503 service_unavailable",
    },
    created_by: "operator-dogfood",
    created_at: new Date(NOW - 18 * MIN).toISOString(),
  },
]

const MOCK_DISPATCHER_STATS: DispatcherStats = {
  queued: 12,
  active_batches: 1,
  batches_submitted: 47,
  results_processed: 1820,
  errors_encountered: 3,
  loop_iter: 423,
}

const MOCK_BUDGETS: BudgetCap[] = [
  {
    scope: { kind: "global", key: "*" },
    daily_limit_usd: 50.0,
    monthly_limit_usd: 800.0,
    per_batch_limit_usd: null,
    enabled: true,
  },
  {
    scope: { kind: "priority", key: "HD" },
    daily_limit_usd: 30.0,
    monthly_limit_usd: 500.0,
    per_batch_limit_usd: 5.0,
    enabled: true,
  },
  {
    scope: { kind: "priority", key: "L4" },
    daily_limit_usd: 15.0,
    monthly_limit_usd: 200.0,
    per_batch_limit_usd: null,
    enabled: true,
  },
  {
    scope: { kind: "workspace", key: "dev" },
    daily_limit_usd: 5.0,
    monthly_limit_usd: 50.0,
    per_batch_limit_usd: null,
    enabled: false,
  },
]

const MOCK_SNAPSHOTS: CostSnapshot[] = [
  {
    scope: { kind: "global", key: "*" },
    spend_today_usd: 41.32,
    spend_month_usd: 478.5,
    budget: MOCK_BUDGETS[0],
  },
  {
    scope: { kind: "priority", key: "HD" },
    spend_today_usd: 27.6,  // 92% of $30 cap → yellow
    spend_month_usd: 312.0,
    budget: MOCK_BUDGETS[1],
  },
  {
    scope: { kind: "priority", key: "L4" },
    spend_today_usd: 16.8,  // 112% of $15 cap → orange
    spend_month_usd: 92.5,
    budget: MOCK_BUDGETS[2],
  },
  {
    scope: { kind: "model", key: "claude-opus-4-7" },
    spend_today_usd: 7.2,
    spend_month_usd: 96.4,
    budget: null,
  },
]

const MOCK_ALERTS: BudgetAlert[] = [
  {
    alert_id: "alert_xy1z2",
    scope: { kind: "priority", key: "L4" },
    period: "daily",
    level: "over_120",
    threshold_usd: 15.0,
    observed_usd: 16.8,
    action: "block",
    fired_at: new Date(NOW - 8 * MIN).toISOString(),
  },
  {
    alert_id: "alert_abc3d",
    scope: { kind: "priority", key: "HD" },
    period: "daily",
    level: "warn_80",
    threshold_usd: 30.0,
    observed_usd: 27.6,
    action: "notify",
    fired_at: new Date(NOW - 27 * MIN).toISOString(),
  },
  {
    alert_id: "alert_pq4r5",
    scope: { kind: "global", key: "*" },
    period: "monthly",
    level: "cap_100",
    threshold_usd: 500.0,
    observed_usd: 478.5,
    action: "throttle",
    fired_at: new Date(NOW - 2 * HOUR).toISOString(),
  },
]

const MOCK_DEFAULT_RULES: EligibilityRule[] = [
  {
    task_kind: "hd_parse_kicad",
    batch_eligible: true,
    batch_priority: "P2",
    reason: "EDA parsing — long-running, no UI dependency",
    realtime_required: false,
    auto_batch_threshold: 10,
  },
  {
    task_kind: "hd_diff_reference",
    batch_eligible: true,
    batch_priority: "P2",
    reason: "Multi-component diff — long, no UI",
    realtime_required: false,
    auto_batch_threshold: 5,
  },
  {
    task_kind: "hd_sensor_kb_extract",
    batch_eligible: true,
    batch_priority: "P3",
    reason: "Datasheet vision LLM extraction — backlog priority",
    realtime_required: false,
    auto_batch_threshold: 20,
  },
  {
    task_kind: "todo_routine",
    batch_eligible: true,
    batch_priority: "P3",
    reason: "Bulk routine processing of TODO checkboxes",
    realtime_required: false,
    auto_batch_threshold: 10,
  },
  {
    task_kind: "chat_ui",
    batch_eligible: false,
    batch_priority: "P0",
    reason: "User-facing chat — real-time required",
    realtime_required: true,
    auto_batch_threshold: null,
  },
  {
    task_kind: "hd_bringup_live",
    batch_eligible: false,
    batch_priority: "P0",
    reason: "Live boot console parse — real-time required",
    realtime_required: true,
    auto_batch_threshold: null,
  },
  {
    task_kind: "generic_dev",
    batch_eligible: false,
    batch_priority: "P1",
    reason: "Default dev task — operator usually wants response now",
    realtime_required: false,
    auto_batch_threshold: null,
  },
]

const MOCK_OVERRIDES: EligibilityRule[] = [
  {
    task_kind: "hd_diff_reference",
    batch_eligible: false,
    batch_priority: "P0",
    reason:
      "operator: customer review session today, want diff results live",
    realtime_required: false,
    auto_batch_threshold: null,
  },
]

const MOCK_PREVIEW: RoutingDecision[] = [
  {
    lane: "batch",
    priority: "P2",
    rule: MOCK_DEFAULT_RULES[0],
    reason: "default batch-eligible: EDA parsing — long-running",
  },
  {
    lane: "realtime",
    priority: "P0",
    rule: MOCK_DEFAULT_RULES[4],
    reason: "default realtime: User-facing chat — real-time required",
  },
  {
    lane: "realtime",
    priority: "P0",
    rule: MOCK_DEFAULT_RULES[5],
    reason:
      "force_lane='batch' VETOED — task_kind 'hd_bringup_live' is realtime_required",
  },
]

const INITIAL_WIZARD: WizardState = {
  mode: "subscription",
  current_step: "not_started",
  target_workspace: "production",
  api_key_configured: false,
  api_key_fingerprint: "",
  spend_daily_usd: null,
  spend_monthly_usd: null,
  fallback_subscription_kept: true,
  smoke_test: null,
  started_at: null,
  completed_at: null,
  rollback_grace_until: null,
}


// ─── Page ─────────────────────────────────────────────────────────


export default function ABPreviewPage(): JSX.Element {
  // Wizard state — interactive, advances locally on each step click.
  const [wizardState, setWizardState] = useState<WizardState>(INITIAL_WIZARD)

  // Eligibility — interactive, supports add/clear override.
  const [overrides, setOverrides] =
    useState<EligibilityRule[]>(MOCK_OVERRIDES)

  return (
    <div className="mx-auto max-w-5xl space-y-8 p-6 text-sm text-gray-900">
      <header className="space-y-1 border-b border-gray-200 pb-3">
        <h1 className="text-2xl font-semibold">AB.* Frontend Preview</h1>
        <p className="text-sm text-gray-600">
          Visual preview of the four deferred panels (AB.4.5 / AB.6.6 /
          AB.8 / AB.9) with realistic mock data. Wires to backend
          endpoints land in a separate batch.
        </p>
      </header>

      <section className="space-y-2">
        <h2 className="text-base font-semibold text-gray-700">
          1 — AB.4.5 Batch Progress Panel
        </h2>
        <p className="text-xs text-gray-500">
          3 batches: one in-progress (cancellable on expand), one
          completed last hour, one failed-to-submit. Click rows to
          expand detail.
        </p>
        <div className="rounded border border-gray-200 bg-white p-4">
          <BatchProgressPanel
            runs={MOCK_RUNS}
            stats={MOCK_DISPATCHER_STATS}
            onCancel={async (id) => {
              alert(`Would cancel batch ${id}`)
            }}
          />
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="text-base font-semibold text-gray-700">
          2 — AB.6.6 Cost Dashboard Panel
        </h2>
        <p className="text-xs text-gray-500">
          Two scopes near caps: HD priority at 92% (yellow), L4
          priority at 112% (orange). Three alerts active across all
          three levels. Disabled dev workspace budget muted.
        </p>
        <div className="rounded border border-gray-200 bg-white p-4">
          <CostDashboardPanel
            snapshots={MOCK_SNAPSHOTS}
            alerts={MOCK_ALERTS}
            budgets={MOCK_BUDGETS}
            onConfigureBudget={(scopeKindKey) => {
              alert(`Would open budget config for ${scopeKindKey}`)
            }}
          />
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="text-base font-semibold text-gray-700">
          3 — AB.8 Provider Mode Wizard
        </h2>
        <p className="text-xs text-gray-500">
          Interactive — submit any sk-ant-prefixed string ≥ 27 chars
          to advance. Each step locally simulates the backend
          endpoint; operator sees the real flow without burning
          budget.
        </p>
        <div className="rounded border border-gray-200 bg-white p-4">
          <ProviderModeWizard
            state={wizardState}
            onSubmitApiKey={async (key, ws) => {
              const fp = key.length >= 8 ? `…${key.slice(-8)}` : "<short>"
              setWizardState((s) => ({
                ...s,
                target_workspace: ws,
                api_key_configured: true,
                api_key_fingerprint: fp,
                current_step: "key_obtained",
                started_at: new Date().toISOString(),
              }))
            }}
            onConfigureSpendLimits={async (daily, monthly) => {
              setWizardState((s) => ({
                ...s,
                spend_daily_usd: daily,
                spend_monthly_usd: monthly,
                current_step: "spend_limits_set",
              }))
            }}
            onSwitchMode={async () => {
              setWizardState((s) => ({
                ...s,
                mode: "api",
                current_step: "mode_switched",
              }))
            }}
            onRunSmokeTest={async () => {
              setWizardState((s) => ({
                ...s,
                smoke_test: {
                  call_id: "smoke_preview",
                  success: true,
                  latency_ms: 312,
                  cost_usd: 0.0014,
                  response_excerpt: "ok",
                },
                current_step: "smoke_test_passed",
              }))
            }}
            onConfirm={async () => {
              const now = new Date()
              const grace = new Date(now.getTime() + 30 * 86_400_000)
              setWizardState((s) => ({
                ...s,
                current_step: "confirmed",
                completed_at: now.toISOString(),
                rollback_grace_until: grace.toISOString(),
              }))
            }}
            onRollback={async () => {
              setWizardState(INITIAL_WIZARD)
            }}
          />
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="text-base font-semibold text-gray-700">
          4 — AB.9 Batch Eligibility Panel + Inline Badge
        </h2>
        <p className="text-xs text-gray-500">
          Try Override on any default rule. realtime_required tasks
          (chat_ui / hd_bringup_live with the lock icon) reject batch
          override at the radio level.
        </p>

        <div className="space-y-3 rounded border border-gray-200 bg-white p-4">
          <h3 className="text-sm font-medium">Inline badge variants</h3>
          <div className="flex flex-wrap gap-3">
            {MOCK_PREVIEW.map((d, i) => (
              <BatchEligibilityBadge key={i} decision={d} />
            ))}
          </div>
        </div>

        <div className="rounded border border-gray-200 bg-white p-4">
          <BatchEligibilityPanel
            defaults={MOCK_DEFAULT_RULES}
            overrides={overrides}
            previewDecisions={MOCK_PREVIEW}
            onSetOverride={async (rule) => {
              setOverrides((prev) => {
                const next = prev.filter(
                  (r) => r.task_kind !== rule.task_kind,
                )
                next.push(rule)
                return next
              })
            }}
            onClearOverride={async (taskKind) => {
              setOverrides((prev) =>
                prev.filter((r) => r.task_kind !== taskKind),
              )
            }}
          />
        </div>
      </section>

      <footer className="border-t border-gray-200 pt-3 text-xs text-gray-500">
        Mounted at <code>/admin/ab-preview</code>. Backend endpoints
        not wired — every action is a local state mutation +{" "}
        <code>alert()</code> stub. See{" "}
        <code>components/omnisight/ab/</code> for source.
      </footer>
    </div>
  )
}

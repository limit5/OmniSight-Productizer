/**
 * AB.* frontend type contracts — TypeScript mirrors of the backend
 * dataclasses shipped in `backend/agents/`. Single source for the
 * 4 deferred UI panels (AB.4.5 / AB.6.6 / AB.8 / AB.9).
 *
 * Keep aligned with:
 *   - backend/agents/batch_client.py        BatchRun / BatchResult
 *   - backend/agents/cost_guard.py          CostEstimate / BudgetCap / BudgetAlert
 *   - backend/agents/anthropic_mode_manager.py  WizardState / WizardStep / SmokeTestResult
 *   - backend/agents/batch_eligibility.py   RoutingDecision / EligibilityRule
 */

// ─── AB.4 batch ──────────────────────────────────────────────────

export type BatchRunStatus =
  | "pending"
  | "submitted"
  | "ended"
  | "canceled"
  | "expired"
  | "failed"

export interface BatchRun {
  batch_run_id: string
  status: BatchRunStatus
  request_count: number
  anthropic_batch_id?: string | null
  submitted_at?: string | null  // ISO 8601
  ended_at?: string | null
  expires_at?: string | null
  success_count: number
  error_count: number
  canceled_count: number
  expired_count: number
  metadata?: Record<string, unknown>
  created_by?: string | null
  created_at: string
}

export interface DispatcherStats {
  queued: number
  active_batches: number
  batches_submitted: number
  results_processed: number
  errors_encountered: number
  loop_iter: number
}

// ─── AB.6 cost ───────────────────────────────────────────────────

export type ScopeKind = "global" | "workspace" | "priority" | "task_type" | "model"
export type AlertLevel = "warn_80" | "cap_100" | "over_120"
export type AlertAction = "notify" | "throttle" | "block"
export type PeriodKind = "per_batch" | "daily" | "monthly"

export interface ScopeKey {
  kind: ScopeKind
  key: string
}

export interface BudgetCap {
  scope: ScopeKey
  daily_limit_usd: number | null
  monthly_limit_usd: number | null
  per_batch_limit_usd: number | null
  enabled: boolean
}

export interface BudgetAlert {
  alert_id: string
  scope: ScopeKey
  period: PeriodKind
  level: AlertLevel
  threshold_usd: number
  observed_usd: number
  action: AlertAction
  fired_at: string
}

export interface CostSnapshot {
  scope: ScopeKey
  spend_today_usd: number
  spend_month_usd: number
  budget?: BudgetCap | null
}

// ─── AB.8 wizard ────────────────────────────────────────────────

export type AnthropicMode = "subscription" | "api"
export type WizardStep =
  | "not_started"
  | "key_obtained"
  | "spend_limits_set"
  | "mode_switched"
  | "smoke_test_passed"
  | "confirmed"
export type WorkspaceKind = "dev" | "batch" | "production"

export interface SmokeTestResult {
  call_id: string
  success: boolean
  latency_ms: number
  cost_usd: number
  error_message?: string | null
  response_excerpt?: string
}

export interface WizardState {
  mode: AnthropicMode
  current_step: WizardStep
  target_workspace: WorkspaceKind
  api_key_configured: boolean
  api_key_fingerprint: string  // already redacted, e.g. "…XYZ12345"
  spend_daily_usd: number | null
  spend_monthly_usd: number | null
  fallback_subscription_kept: boolean
  smoke_test: SmokeTestResult | null
  started_at: string | null
  completed_at: string | null
  rollback_grace_until: string | null
}

// ─── AB.9 eligibility ───────────────────────────────────────────

export type LaneType = "realtime" | "batch"
export type PriorityLevel = "P0" | "P1" | "P2" | "P3"

export interface EligibilityRule {
  task_kind: string
  batch_eligible: boolean
  batch_priority: PriorityLevel
  reason: string
  realtime_required: boolean
  auto_batch_threshold: number | null
}

export interface RoutingDecision {
  lane: LaneType
  priority: PriorityLevel
  rule: EligibilityRule
  reason: string
}

// ─── Shared formatters ──────────────────────────────────────────

export function formatUsd(value: number, fractionDigits = 2): string {
  return `$${value.toFixed(fractionDigits)}`
}

export function formatPercent(numerator: number, denominator: number): string {
  if (denominator <= 0) return "—"
  return `${((numerator / denominator) * 100).toFixed(1)}%`
}

export function formatDateRelative(iso: string | null | undefined): string {
  if (!iso) return "—"
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const deltaSec = (Date.now() - date.getTime()) / 1000
  if (deltaSec < 60) return `${Math.floor(deltaSec)}s ago`
  if (deltaSec < 3600) return `${Math.floor(deltaSec / 60)}m ago`
  if (deltaSec < 86400) return `${Math.floor(deltaSec / 3600)}h ago`
  return `${Math.floor(deltaSec / 86400)}d ago`
}

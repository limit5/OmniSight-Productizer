"use client"

/**
 * AB.8 — Subscription ↔ API mode wizard UI.
 *
 * Drives the 5-step migration backed by `AnthropicModeManager`. The
 * full state machine + idempotence + rollback safety net are owned
 * by the backend; this component is the operator-facing form +
 * status surface.
 *
 * Five steps map 1:1 to backend API endpoints:
 *
 *   1. submit_api_key       → `/api/v1/anthropic-mode/wizard/submit-api-key`
 *   2. configure_spend       → `/api/v1/anthropic-mode/wizard/spend-limits`
 *   3. switch_mode           → `/api/v1/anthropic-mode/wizard/switch-mode`
 *   4. run_smoke_test        → `/api/v1/anthropic-mode/wizard/smoke-test`
 *   5. confirm               → `/api/v1/anthropic-mode/wizard/confirm`
 *   rollback                 → `/api/v1/anthropic-mode/rollback`
 *
 * UI never displays the full API key — only the backend-redacted
 * last-8 fingerprint surfaced in `state.api_key_fingerprint`.
 *
 * Backend contract: WizardState / WizardStep / SmokeTestResult /
 * AnthropicMode in `backend/agents/anthropic_mode_manager.py`,
 * mirrored in `./types.ts`.
 */

import { useState } from "react"
import {
  Check,
  Circle,
  Lock,
  RefreshCw,
  AlertTriangle,
  XCircle,
} from "lucide-react"
import {
  type SmokeTestResult,
  type WizardState,
  type WizardStep,
  type WorkspaceKind,
  formatDateRelative,
  formatUsd,
} from "./types"

const STEP_ORDER: WizardStep[] = [
  "not_started",
  "key_obtained",
  "spend_limits_set",
  "mode_switched",
  "smoke_test_passed",
  "confirmed",
]

const STEP_LABEL: Record<WizardStep, string> = {
  not_started: "Not started",
  key_obtained: "1 — API key set",
  spend_limits_set: "2 — Spend caps set",
  mode_switched: "3 — Mode switched",
  smoke_test_passed: "4 — Smoke test passed",
  confirmed: "5 — Confirmed",
}

function stepIndex(step: WizardStep): number {
  return STEP_ORDER.indexOf(step)
}

export interface ProviderModeWizardProps {
  state: WizardState
  onSubmitApiKey?: (apiKey: string, workspace: WorkspaceKind) => Promise<void>
  onConfigureSpendLimits?: (
    daily: number | null,
    monthly: number | null,
  ) => Promise<void>
  onSwitchMode?: () => Promise<void>
  onRunSmokeTest?: () => Promise<void>
  onConfirm?: () => Promise<void>
  onRollback?: () => Promise<void>
}

export function ProviderModeWizard(
  props: ProviderModeWizardProps,
): JSX.Element {
  const { state } = props
  const idx = stepIndex(state.current_step)
  return (
    <section data-testid="provider-mode-wizard" className="space-y-4">
      <header className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Anthropic Provider Mode</h2>
        <ModeBadge mode={state.mode} />
      </header>

      <Steps current={state.current_step} />

      <div className="space-y-3 rounded border border-gray-200 p-3">
        {idx < 1 && (
          <Step1ApiKey
            currentWorkspace={state.target_workspace}
            onSubmit={props.onSubmitApiKey}
          />
        )}
        {idx === 1 && (
          <Step2SpendLimits onConfigure={props.onConfigureSpendLimits} />
        )}
        {idx === 2 && <Step3SwitchMode onSwitch={props.onSwitchMode} />}
        {idx === 3 && (
          <Step4SmokeTest
            smokeTest={state.smoke_test}
            onRun={props.onRunSmokeTest}
          />
        )}
        {idx === 4 && <Step5Confirm onConfirm={props.onConfirm} />}
        {idx === 5 && (
          <ConfirmedSummary state={state} onRollback={props.onRollback} />
        )}
      </div>
    </section>
  )
}

// ─── Step bar ────────────────────────────────────────────────────

function Steps({ current }: { current: WizardStep }): JSX.Element {
  const currentIdx = stepIndex(current)
  return (
    <ol
      data-testid="wizard-steps"
      className="flex items-center gap-2 text-xs"
    >
      {STEP_ORDER.slice(1).map((step, i) => {
        const stepIdx = i + 1
        const done = stepIdx <= currentIdx
        const active = stepIdx === currentIdx
        return (
          <li
            key={step}
            data-testid={`wizard-step-${step}`}
            data-done={done}
            data-active={active}
            className="flex items-center gap-1"
          >
            {done ? (
              <Check size={14} className="text-green-600" aria-hidden />
            ) : active ? (
              <Circle size={14} className="text-blue-600" aria-hidden />
            ) : (
              <Circle size={14} className="text-gray-300" aria-hidden />
            )}
            <span
              className={
                done
                  ? "text-gray-700"
                  : active
                    ? "font-medium text-blue-700"
                    : "text-gray-400"
              }
            >
              {STEP_LABEL[step]}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

function ModeBadge({ mode }: { mode: "subscription" | "api" }): JSX.Element {
  return (
    <span
      data-testid={`mode-badge-${mode}`}
      className={
        mode === "api"
          ? "rounded bg-purple-100 px-2 py-0.5 text-xs font-medium text-purple-700"
          : "rounded bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700"
      }
    >
      {mode === "api" ? "API mode" : "Subscription mode"}
    </span>
  )
}

// ─── Step 1: API key ─────────────────────────────────────────────

function Step1ApiKey({
  currentWorkspace,
  onSubmit,
}: {
  currentWorkspace: WorkspaceKind
  onSubmit?: (key: string, ws: WorkspaceKind) => Promise<void>
}): JSX.Element {
  const [apiKey, setApiKey] = useState("")
  const [workspace, setWorkspace] = useState<WorkspaceKind>(currentWorkspace)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    if (!onSubmit) return
    setBusy(true)
    setErr(null)
    try {
      await onSubmit(apiKey.trim(), workspace)
      setApiKey("")
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Submission failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Step 1 — Anthropic API key</h3>
      <p className="text-xs text-gray-500">
        Generate at console.anthropic.com → API Keys. Set a monthly cap
        on the Anthropic side too — this is your second safety net.
      </p>
      <label className="block text-xs">
        Workspace
        <select
          data-testid="wizard-workspace"
          value={workspace}
          onChange={(e) => setWorkspace(e.target.value as WorkspaceKind)}
          className="ml-2 rounded border border-gray-300 px-1 text-sm"
        >
          <option value="dev">dev</option>
          <option value="batch">batch</option>
          <option value="production">production</option>
        </select>
      </label>
      <input
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        placeholder="sk-ant-..."
        data-testid="wizard-api-key"
        className="w-full rounded border border-gray-300 px-2 py-1 font-mono text-sm"
        autoComplete="off"
        spellCheck={false}
      />
      {err && (
        <p data-testid="wizard-error" className="text-xs text-red-600">
          {err}
        </p>
      )}
      <button
        type="button"
        onClick={submit}
        disabled={busy || !apiKey.trim()}
        data-testid="wizard-submit-api-key"
        className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        {busy ? "Submitting…" : "Submit API key"}
      </button>
    </div>
  )
}

// ─── Step 2: spend limits ────────────────────────────────────────

function Step2SpendLimits({
  onConfigure,
}: {
  onConfigure?: (daily: number | null, monthly: number | null) => Promise<void>
}): JSX.Element {
  const [daily, setDaily] = useState<string>("30")
  const [monthly, setMonthly] = useState<string>("500")
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    if (!onConfigure) return
    setBusy(true)
    try {
      await onConfigure(
        daily === "" ? null : Number(daily),
        monthly === "" ? null : Number(monthly),
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Step 2 — Spend caps</h3>
      <p className="text-xs text-gray-500">
        Tip: set OmniSight cap to 50-70% of your Anthropic console cap so
        80/100/120 alerts fire BEFORE Anthropic blocks.
      </p>
      <div className="flex gap-3">
        <label className="flex-1 text-xs">
          Daily ($)
          <input
            type="number"
            min={0}
            step="0.01"
            value={daily}
            onChange={(e) => setDaily(e.target.value)}
            data-testid="wizard-daily-cap"
            className="mt-0.5 block w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="flex-1 text-xs">
          Monthly ($)
          <input
            type="number"
            min={0}
            step="0.01"
            value={monthly}
            onChange={(e) => setMonthly(e.target.value)}
            data-testid="wizard-monthly-cap"
            className="mt-0.5 block w-full rounded border border-gray-300 px-2 py-1 text-sm"
          />
        </label>
      </div>
      <button
        type="button"
        onClick={submit}
        disabled={busy}
        data-testid="wizard-submit-spend"
        className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        {busy ? "Saving…" : "Save spend caps"}
      </button>
    </div>
  )
}

// ─── Step 3: switch mode ─────────────────────────────────────────

function Step3SwitchMode({
  onSwitch,
}: {
  onSwitch?: () => Promise<void>
}): JSX.Element {
  const [busy, setBusy] = useState(false)
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Step 3 — Switch to API mode</h3>
      <p className="text-xs text-gray-500">
        Subscription credentials remain as a 30-day rollback fallback.
        You can switch back any time via Rollback.
      </p>
      <button
        type="button"
        onClick={async () => {
          if (!onSwitch) return
          setBusy(true)
          try {
            await onSwitch()
          } finally {
            setBusy(false)
          }
        }}
        disabled={busy}
        data-testid="wizard-switch-mode"
        className="rounded bg-purple-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        {busy ? "Switching…" : "Switch to API mode"}
      </button>
    </div>
  )
}

// ─── Step 4: smoke test ──────────────────────────────────────────

function Step4SmokeTest({
  smokeTest,
  onRun,
}: {
  smokeTest: SmokeTestResult | null
  onRun?: () => Promise<void>
}): JSX.Element {
  const [busy, setBusy] = useState(false)
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Step 4 — Smoke test</h3>
      <p className="text-xs text-gray-500">
        Runs a small real Anthropic API call to verify auth + tools +
        cost tracker end-to-end. Failure does not auto-rollback —
        retry or rollback manually.
      </p>
      {smokeTest && (
        <div
          data-testid="wizard-smoke-result"
          className={
            smokeTest.success
              ? "rounded border border-green-200 bg-green-50 p-2 text-xs"
              : "rounded border border-red-200 bg-red-50 p-2 text-xs"
          }
        >
          {smokeTest.success ? (
            <>
              <Check size={14} className="inline text-green-600" aria-hidden />{" "}
              Smoke test passed — latency {smokeTest.latency_ms}ms, cost{" "}
              {formatUsd(smokeTest.cost_usd, 4)}
            </>
          ) : (
            <>
              <XCircle size={14} className="inline text-red-600" aria-hidden />{" "}
              Smoke test failed: {smokeTest.error_message ?? "(no message)"}
            </>
          )}
        </div>
      )}
      <button
        type="button"
        onClick={async () => {
          if (!onRun) return
          setBusy(true)
          try {
            await onRun()
          } finally {
            setBusy(false)
          }
        }}
        disabled={busy}
        data-testid="wizard-run-smoke"
        className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        {busy ? "Running…" : "Run smoke test"}
      </button>
    </div>
  )
}

// ─── Step 5: confirm ─────────────────────────────────────────────

function Step5Confirm({
  onConfirm,
}: {
  onConfirm?: () => Promise<void>
}): JSX.Element {
  const [busy, setBusy] = useState(false)
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Step 5 — Confirm migration</h3>
      <p className="text-xs text-gray-500">
        Starts the 30-day rollback grace period. Subscription fallback
        stays kept the entire window. After 30 days, run "Finalize" to
        permanently disable subscription mode.
      </p>
      <button
        type="button"
        onClick={async () => {
          if (!onConfirm) return
          setBusy(true)
          try {
            await onConfirm()
          } finally {
            setBusy(false)
          }
        }}
        disabled={busy}
        data-testid="wizard-confirm"
        className="rounded bg-green-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        {busy ? "Confirming…" : "Confirm migration"}
      </button>
    </div>
  )
}

// ─── Confirmed summary + rollback ────────────────────────────────

function ConfirmedSummary({
  state,
  onRollback,
}: {
  state: WizardState
  onRollback?: () => Promise<void>
}): JSX.Element {
  return (
    <div className="space-y-3">
      <div
        data-testid="wizard-confirmed-summary"
        className="rounded border border-green-200 bg-green-50 p-3 text-sm"
      >
        <div className="flex items-center gap-2">
          <Check size={18} className="text-green-700" aria-hidden />
          <span className="font-medium">API mode active</span>
        </div>
        <dl className="mt-2 space-y-0.5 text-xs">
          <SummaryRow
            label="Workspace"
            value={state.target_workspace}
          />
          <SummaryRow
            label="Key fingerprint"
            value={state.api_key_fingerprint || "—"}
            mono
          />
          <SummaryRow
            label="Daily cap"
            value={
              state.spend_daily_usd != null
                ? formatUsd(state.spend_daily_usd)
                : "—"
            }
          />
          <SummaryRow
            label="Monthly cap"
            value={
              state.spend_monthly_usd != null
                ? formatUsd(state.spend_monthly_usd)
                : "—"
            }
          />
          <SummaryRow
            label="Migrated"
            value={formatDateRelative(state.completed_at)}
          />
          <SummaryRow
            label="Rollback grace"
            value={formatDateRelative(state.rollback_grace_until)}
          />
        </dl>
      </div>
      {state.fallback_subscription_kept ? (
        <button
          type="button"
          onClick={async () => {
            if (!onRollback) return
            await onRollback()
          }}
          data-testid="wizard-rollback"
          className="rounded border border-yellow-300 px-3 py-1 text-sm text-yellow-800 hover:bg-yellow-50"
        >
          <AlertTriangle size={14} className="mr-1 inline" aria-hidden />
          Rollback to subscription mode
        </button>
      ) : (
        <p
          data-testid="wizard-rollback-locked"
          className="flex items-center gap-1 text-xs text-gray-500"
        >
          <Lock size={12} aria-hidden />
          Subscription fallback finalized — rollback unavailable.
        </p>
      )}
    </div>
  )
}

function SummaryRow({
  label,
  value,
  mono = false,
}: {
  label: string
  value: string
  mono?: boolean
}): JSX.Element {
  return (
    <div className="flex gap-2">
      <dt className="w-32 shrink-0 text-gray-500">{label}</dt>
      <dd className={mono ? "font-mono" : ""}>{value}</dd>
    </div>
  )
}

"use client"

/**
 * AB.9 — Per-task batch eligibility badge + operator override panel.
 *
 * Two surfaces in one component:
 *
 *   1. **Inline badge** for any task — shows the routing decision
 *      (lane / priority / reason) so operators understand why
 *      a task went realtime vs batch
 *
 *   2. **Override panel** — operator can force a different lane
 *      for a given task_kind, surfaced in EligibilityRegistry as
 *      override over the default
 *
 * Hard veto path: if the task's underlying rule has
 * `realtime_required=true`, force_lane="batch" is rejected by the
 * backend with an explicit "VETOED" reason. UI surfaces this
 * (disabled batch button + tooltip).
 *
 * Backend contract: RoutingDecision / EligibilityRule from
 * `backend/agents/batch_eligibility.py`, mirrored in `./types.ts`.
 */

import { type JSX, useState } from "react"
import { Lock, Zap, Database, Layers, Plus, Trash } from "lucide-react"
import {
  type EligibilityRule,
  type LaneType,
  type RoutingDecision,
} from "./types"

// ─── Inline badge (for task list rows) ───────────────────────────

export interface BatchEligibilityBadgeProps {
  decision: RoutingDecision
  className?: string
}

export function BatchEligibilityBadge({
  decision,
  className = "",
}: BatchEligibilityBadgeProps): JSX.Element {
  const isVetoed = decision.reason.includes("VETOED")
  const Icon = decision.lane === "batch" ? Database : Zap
  return (
    <span
      data-testid="batch-eligibility-badge"
      data-lane={decision.lane}
      data-vetoed={isVetoed}
      title={decision.reason}
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ${
        decision.lane === "batch"
          ? "bg-blue-100 text-blue-800"
          : "bg-orange-100 text-orange-800"
      } ${className}`}
    >
      <Icon size={12} aria-hidden />
      <span>{decision.lane === "batch" ? "Batch" : "Realtime"}</span>
      <span className="text-gray-500">·</span>
      <span>{decision.priority}</span>
      {decision.rule.realtime_required && (
        <Lock size={10} aria-label="realtime required" />
      )}
    </span>
  )
}

// ─── Override panel ──────────────────────────────────────────────

export interface BatchEligibilityPanelProps {
  /** Default rules visible to the operator (read-only metadata). */
  defaults: EligibilityRule[]
  /** Currently-active operator overrides. */
  overrides: EligibilityRule[]
  /** Optional preview of how a specific task_kind currently routes. */
  previewDecisions?: RoutingDecision[]

  onSetOverride?: (rule: EligibilityRule) => Promise<void>
  onClearOverride?: (taskKind: string) => Promise<void>
}

export function BatchEligibilityPanel(
  props: BatchEligibilityPanelProps,
): JSX.Element {
  const { defaults, overrides, previewDecisions, onSetOverride, onClearOverride } = props
  return (
    <section
      data-testid="batch-eligibility-panel"
      className="space-y-4"
    >
      <header>
        <h2 className="text-lg font-semibold">Task lane routing</h2>
        <p className="text-xs text-gray-500">
          Tasks marked{" "}
          <Lock size={10} className="inline" aria-hidden /> are{" "}
          <code>realtime_required</code> — they cannot be batched even
          with override (24h batch SLA would make them useless).
        </p>
      </header>

      <OverridesList
        overrides={overrides}
        onClear={onClearOverride}
      />

      <DefaultsTable defaults={defaults} onSetOverride={onSetOverride} />

      {previewDecisions && previewDecisions.length > 0 && (
        <DecisionsPreview decisions={previewDecisions} />
      )}
    </section>
  )
}

// ─── Overrides list ──────────────────────────────────────────────

function OverridesList({
  overrides,
  onClear,
}: {
  overrides: EligibilityRule[]
  onClear?: (taskKind: string) => Promise<void>
}): JSX.Element {
  return (
    <div data-testid="eligibility-overrides">
      <h3 className="mb-1 text-sm font-medium text-gray-700">
        Active overrides ({overrides.length})
      </h3>
      {overrides.length === 0 ? (
        <p
          data-testid="eligibility-overrides-empty"
          className="text-xs text-gray-500"
        >
          No operator overrides — using defaults.
        </p>
      ) : (
        <ul className="space-y-1">
          {overrides.map((rule) => (
            <li
              key={rule.task_kind}
              data-testid={`eligibility-override-${rule.task_kind}`}
              className="flex items-center justify-between rounded border border-yellow-200 bg-yellow-50 px-2 py-1 text-xs"
            >
              <div>
                <span className="font-mono">{rule.task_kind}</span>
                <span className="ml-2 text-gray-500">→</span>
                <span className="ml-1 font-medium">
                  {rule.batch_eligible ? "batch" : "realtime"} ({rule.batch_priority})
                </span>
                <span className="ml-2 text-gray-600">{rule.reason}</span>
              </div>
              {onClear && (
                <button
                  type="button"
                  onClick={() => onClear(rule.task_kind)}
                  data-testid={`eligibility-clear-${rule.task_kind}`}
                  aria-label={`Clear override for ${rule.task_kind}`}
                  className="text-red-600 hover:text-red-800"
                >
                  <Trash size={14} />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ─── Defaults table + per-row override action ───────────────────

function DefaultsTable({
  defaults,
  onSetOverride,
}: {
  defaults: EligibilityRule[]
  onSetOverride?: (rule: EligibilityRule) => Promise<void>
}): JSX.Element {
  const [openOverrideFor, setOpenOverrideFor] = useState<string | null>(null)
  return (
    <div data-testid="eligibility-defaults">
      <h3 className="mb-1 text-sm font-medium text-gray-700">Default routing</h3>
      <table className="w-full text-xs">
        <thead className="border-b border-gray-200 text-gray-500">
          <tr>
            <th className="text-left">Task kind</th>
            <th className="text-left">Default lane</th>
            <th className="text-left">Priority</th>
            <th className="text-left">Reason</th>
            <th className="text-right" />
          </tr>
        </thead>
        <tbody>
          {defaults.map((rule) => (
            <DefaultRow
              key={rule.task_kind}
              rule={rule}
              isOpen={openOverrideFor === rule.task_kind}
              onOpenOverride={() => setOpenOverrideFor(rule.task_kind)}
              onCloseOverride={() => setOpenOverrideFor(null)}
              onSetOverride={onSetOverride}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DefaultRow({
  rule,
  isOpen,
  onOpenOverride,
  onCloseOverride,
  onSetOverride,
}: {
  rule: EligibilityRule
  isOpen: boolean
  onOpenOverride: () => void
  onCloseOverride: () => void
  onSetOverride?: (rule: EligibilityRule) => Promise<void>
}): JSX.Element {
  return (
    <>
      <tr
        data-testid={`eligibility-default-${rule.task_kind}`}
        className="border-b border-gray-100"
      >
        <td className="py-1 font-mono">
          {rule.task_kind}
          {rule.realtime_required && (
            <Lock
              size={10}
              className="ml-1 inline text-gray-400"
              aria-label="realtime_required"
            />
          )}
        </td>
        <td className="py-1">{rule.batch_eligible ? "batch" : "realtime"}</td>
        <td className="py-1">{rule.batch_priority}</td>
        <td className="py-1 text-gray-600">{rule.reason}</td>
        <td className="py-1 text-right">
          {onSetOverride && (
            <button
              type="button"
              onClick={isOpen ? onCloseOverride : onOpenOverride}
              data-testid={`eligibility-override-toggle-${rule.task_kind}`}
              className="text-blue-600 hover:underline"
            >
              {isOpen ? "Cancel" : "Override"}
            </button>
          )}
        </td>
      </tr>
      {isOpen && onSetOverride && (
        <OverrideRow rule={rule} onSet={onSetOverride} />
      )}
    </>
  )
}

function OverrideRow({
  rule,
  onSet,
}: {
  rule: EligibilityRule
  onSet: (rule: EligibilityRule) => Promise<void>
}): JSX.Element {
  const realtimeOnly = rule.realtime_required
  // For an already-batch rule the meaningful override is "force realtime"
  // (and vice versa). Computed default flips the eligibility.
  const [batchEligible, setBatchEligible] = useState(!rule.batch_eligible && !realtimeOnly)
  const [reason, setReason] = useState("operator override")
  const [busy, setBusy] = useState(false)

  // realtime_required tasks: batch is hard-vetoed; force batchEligible=false
  // and disable the batch radio.
  const submit = async () => {
    setBusy(true)
    try {
      await onSet({
        ...rule,
        batch_eligible: realtimeOnly ? false : batchEligible,
        reason,
      })
    } finally {
      setBusy(false)
    }
  }
  return (
    <tr
      data-testid={`eligibility-override-form-${rule.task_kind}`}
      className="border-b border-gray-100 bg-gray-50"
    >
      <td colSpan={5} className="space-y-2 py-2 pl-4 pr-2 text-xs">
        <fieldset className="flex items-center gap-3">
          <legend className="sr-only">New lane</legend>
          <label
            className={`flex items-center gap-1 ${realtimeOnly ? "cursor-not-allowed text-gray-400" : ""}`}
          >
            <input
              type="radio"
              name={`lane-${rule.task_kind}`}
              checked={batchEligible}
              disabled={realtimeOnly}
              onChange={() => setBatchEligible(true)}
              data-testid={`eligibility-override-batch-${rule.task_kind}`}
            />
            <Database size={12} aria-hidden /> Batch
          </label>
          <label className="flex items-center gap-1">
            <input
              type="radio"
              name={`lane-${rule.task_kind}`}
              checked={!batchEligible || realtimeOnly}
              onChange={() => setBatchEligible(false)}
              data-testid={`eligibility-override-realtime-${rule.task_kind}`}
            />
            <Zap size={12} aria-hidden /> Realtime
          </label>
          {realtimeOnly && (
            <span className="text-yellow-700">
              <Lock size={10} className="inline" /> Batch unavailable
              (realtime_required)
            </span>
          )}
        </fieldset>
        <input
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Why are you overriding? (audit trail)"
          data-testid={`eligibility-override-reason-${rule.task_kind}`}
          className="w-full rounded border border-gray-300 px-2 py-0.5"
        />
        <button
          type="button"
          onClick={submit}
          disabled={busy}
          data-testid={`eligibility-override-save-${rule.task_kind}`}
          className="rounded bg-blue-600 px-3 py-0.5 text-white disabled:opacity-40"
        >
          {busy ? "Saving…" : (
            <>
              <Plus size={12} className="mr-1 inline" aria-hidden /> Apply override
            </>
          )}
        </button>
      </td>
    </tr>
  )
}

// ─── Optional preview of live decisions ──────────────────────────

function DecisionsPreview({
  decisions,
}: {
  decisions: RoutingDecision[]
}): JSX.Element {
  return (
    <div data-testid="eligibility-preview">
      <h3 className="mb-1 text-sm font-medium text-gray-700">
        Live routing preview
      </h3>
      <ul className="space-y-1">
        {decisions.map((d, idx) => (
          <li
            key={`${d.rule.task_kind}-${idx}`}
            className="flex items-center gap-2 text-xs"
          >
            <span className="font-mono">{d.rule.task_kind}</span>
            <Layers size={10} className="text-gray-400" aria-hidden />
            <BatchEligibilityBadge decision={d} />
            <span className="text-gray-500">{d.reason}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

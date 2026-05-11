/**
 * OP-889 D17 — single row in the deployment-history list.
 *
 * One row per audit entry from `/admin/deploy-history`. Surfaces
 *   - tag (semver / git sha)
 *   - kind (`deploy` / `rollback`)
 *   - outcome (`succeeded` / `failed` / etc.)
 *   - actor + relative timestamp + free-text summary
 *   - per-row rollback CTA — fires `onRollback(release_id)` so the parent
 *     can call the D9 orchestrator (one click → disabled + spinner via
 *     the `inFlight` flag from the parent's race-protection reducer).
 *
 * The row is intentionally dumb: no fetch, no SSE — every dynamic bit
 * arrives via props so the parent panel owns the state machine.
 */
"use client"

import * as React from "react"
import {
  AlertTriangle,
  CheckCircle2,
  CircleSlash,
  History,
  Loader2,
  Rocket,
  RotateCcw,
  XCircle,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

export type DeployRecordKind = "deploy" | "rollback"
export type DeployRecordOutcome =
  | "succeeded"
  | "failed"
  | "in_progress"
  | "cancelled"
  | "rolled_back"
  | "pending"
  | "unknown"

export interface DeployRecord {
  /** Audit-row id; also used as `release_id` for rollback. */
  id: number | string
  /** ISO-8601 timestamp from the audit row. */
  timestamp: string
  /** Semver / git sha. May be empty on synthetic rows. */
  tag: string | null
  kind: DeployRecordKind
  outcome: DeployRecordOutcome
  actor: string | null
  /** One-line operator-friendly summary built server-side. */
  summary: string
}

export function formatRelativeAge(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso || typeof iso !== "string") return "—"
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return iso
  const delta = Math.max(0, now - t)
  if (delta < 60_000) return "just now"
  const minutes = Math.floor(delta / 60_000)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  const weeks = Math.floor(days / 7)
  return `${weeks}w ago`
}

export function deployOutcomeColor(outcome: DeployRecordOutcome): string {
  switch (outcome) {
    case "succeeded":
      return "var(--validation-emerald)"
    case "failed":
    case "rolled_back":
      return "var(--critical-red)"
    case "in_progress":
    case "pending":
      return "var(--neural-blue)"
    case "cancelled":
      return "var(--muted-foreground)"
    default:
      return "var(--muted-foreground)"
  }
}

export function deployOutcomeLabel(outcome: DeployRecordOutcome): string {
  switch (outcome) {
    case "succeeded":
      return "Succeeded"
    case "failed":
      return "Failed"
    case "in_progress":
      return "In progress"
    case "cancelled":
      return "Cancelled"
    case "rolled_back":
      return "Rolled back"
    case "pending":
      return "Pending"
    default:
      return "Unknown"
  }
}

function KindIcon({ kind }: { kind: DeployRecordKind }) {
  if (kind === "rollback") {
    return <RotateCcw className="size-3.5" aria-hidden="true" />
  }
  return <Rocket className="size-3.5" aria-hidden="true" />
}

function OutcomeIcon({ outcome }: { outcome: DeployRecordOutcome }) {
  switch (outcome) {
    case "succeeded":
      return <CheckCircle2 className="size-3.5" aria-hidden="true" />
    case "failed":
      return <XCircle className="size-3.5" aria-hidden="true" />
    case "in_progress":
    case "pending":
      return <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
    case "cancelled":
      return <CircleSlash className="size-3.5" aria-hidden="true" />
    case "rolled_back":
      return <RotateCcw className="size-3.5" aria-hidden="true" />
    default:
      return <History className="size-3.5" aria-hidden="true" />
  }
}

export interface DeployHistoryRowProps {
  record: DeployRecord
  /**
   * Whether the rollback for *this* row is currently in flight. The
   * parent flips this true on the first click and clears it once D9
   * resolves — covers `RollbackButtonRaceCondition` from the ticket
   * error catalog.
   */
  inFlight?: boolean
  /**
   * Whether a rollback elsewhere is in flight — disables this row's
   * button so the operator cannot stack rollbacks.
   */
  globalRollbackInFlight?: boolean
  /** Fires when the operator clicks "Rollback to this". */
  onRollback?: (record: DeployRecord) => void
  /** Pin "now" for relative-time formatting (test seam). */
  now?: number
  /** Whether this row represents the currently-live prod build. */
  isCurrent?: boolean
  /** `data-testid` root from the parent panel. */
  testId?: string
}

export function DeployHistoryRow({
  record,
  inFlight = false,
  globalRollbackInFlight = false,
  onRollback,
  now = Date.now(),
  isCurrent = false,
  testId = "deploy-history-row",
}: DeployHistoryRowProps) {
  const outcomeColour = deployOutcomeColor(record.outcome)
  const outcomeLabel = deployOutcomeLabel(record.outcome)
  // A rollback CTA only makes sense for *successful prior* deploys.
  // Failed / in-progress / already-rolled-back rows hide the button so
  // operators cannot send the orchestrator into a stuck state.
  const canRollback =
    record.kind === "deploy" &&
    record.outcome === "succeeded" &&
    !!record.tag &&
    !isCurrent
  const disabled = inFlight || globalRollbackInFlight || !canRollback

  const handleClick = React.useCallback(() => {
    if (disabled) return
    onRollback?.(record)
  }, [disabled, onRollback, record])

  return (
    <li
      data-testid={`${testId}-${record.id}`}
      data-kind={record.kind}
      data-outcome={record.outcome}
      data-is-current={isCurrent ? "true" : "false"}
      data-in-flight={inFlight ? "true" : "false"}
      className={cn(
        "flex flex-col gap-1 rounded-md border px-3 py-2 text-xs",
        record.outcome === "failed" && "border-rose-500/40 bg-rose-500/5",
        record.outcome === "succeeded" && "border-emerald-500/40 bg-emerald-500/5",
        record.outcome === "in_progress" && "border-sky-500/40 bg-sky-500/5",
        isCurrent && "ring-1 ring-emerald-400/60",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          data-testid={`${testId}-${record.id}-kind`}
          variant="outline"
          className="h-5 gap-1 px-1.5 text-[11px]"
        >
          <KindIcon kind={record.kind} />
          {record.kind === "rollback" ? "Rollback" : "Deploy"}
        </Badge>
        <Badge
          data-testid={`${testId}-${record.id}-outcome`}
          variant="outline"
          className="h-5 gap-1 px-1.5 text-[11px]"
          style={{ color: outcomeColour }}
        >
          <OutcomeIcon outcome={record.outcome} />
          {outcomeLabel}
        </Badge>
        <code
          data-testid={`${testId}-${record.id}-tag`}
          className="rounded-sm bg-muted/50 px-1.5 py-0.5 font-mono text-[11px]"
        >
          {record.tag || "—"}
        </code>
        {isCurrent && (
          <Badge
            data-testid={`${testId}-${record.id}-current`}
            variant="secondary"
            className="h-5 px-1.5 text-[11px]"
          >
            Current
          </Badge>
        )}
        <span
          data-testid={`${testId}-${record.id}-time`}
          className="ml-auto font-mono text-[10px] text-muted-foreground"
          title={record.timestamp}
        >
          {formatRelativeAge(record.timestamp, now)}
        </span>
      </div>

      <p
        data-testid={`${testId}-${record.id}-summary`}
        className="text-[11px] text-muted-foreground"
      >
        {record.summary}
      </p>

      <div className="flex items-center justify-between gap-2">
        <span
          data-testid={`${testId}-${record.id}-actor`}
          className="font-mono text-[10px] text-muted-foreground"
        >
          {record.actor ?? "system"}
        </span>
        {canRollback && onRollback && (
          <Button
            type="button"
            size="sm"
            variant={inFlight ? "ghost" : "secondary"}
            data-testid={`${testId}-${record.id}-rollback`}
            onClick={handleClick}
            disabled={disabled}
            className="h-7 gap-1 px-2 text-xs"
          >
            {inFlight ? (
              <>
                <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                Rolling back…
              </>
            ) : (
              <>
                <RotateCcw className="size-3" aria-hidden="true" />
                Rollback to this
              </>
            )}
          </Button>
        )}
        {record.outcome === "failed" && (
          <span
            data-testid={`${testId}-${record.id}-failure-marker`}
            className="inline-flex items-center gap-1 text-[10px] text-rose-400"
          >
            <AlertTriangle className="size-3" aria-hidden="true" />
            review audit log
          </span>
        )}
      </div>
    </li>
  )
}

export default DeployHistoryRow

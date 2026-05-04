"use client"

/**
 * AB.4.5 — Batch progress panel.
 *
 * Reads the batch_runs table (alembic 0181) + dispatcher.stats() and
 * surfaces:
 *
 *   - dispatcher headline counters (queued / active / submitted / errors)
 *   - per-batch row with status badge + progress + counts
 *   - per-batch detail expansion with metadata + timing
 *
 * Pure presentation — caller wires the data via props (typically a
 * useEffect-driven 60s poll of `/api/v1/batch/runs` + `/api/v1/batch/stats`).
 *
 * Backend contract: BatchRun + DispatcherStats from
 * `backend/agents/batch_client.py` and `backend/agents/batch_dispatcher.py`,
 * mirrored in `./types.ts`.
 */

import { type JSX, useState } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import {
  type BatchRun,
  type BatchRunStatus,
  type DispatcherStats,
  formatDateRelative,
} from "./types"

const STATUS_COLOR: Record<BatchRunStatus, string> = {
  pending: "bg-gray-200 text-gray-700",
  submitted: "bg-blue-100 text-blue-800",
  ended: "bg-green-100 text-green-800",
  canceled: "bg-yellow-100 text-yellow-800",
  expired: "bg-orange-100 text-orange-800",
  failed: "bg-red-100 text-red-800",
}

const STATUS_LABEL: Record<BatchRunStatus, string> = {
  pending: "Pending",
  submitted: "Processing",
  ended: "Completed",
  canceled: "Canceled",
  expired: "Expired (24h)",
  failed: "Failed to submit",
}

export interface BatchProgressPanelProps {
  runs: BatchRun[]
  stats?: DispatcherStats | null
  onCancel?: (batchRunId: string) => Promise<void> | void
}

export function BatchProgressPanel(props: BatchProgressPanelProps): JSX.Element {
  const { runs, stats, onCancel } = props
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  return (
    <section data-testid="batch-progress-panel" className="space-y-3">
      <header className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Batch Dispatcher</h2>
        {stats && <DispatcherStatsRow stats={stats} />}
      </header>

      {runs.length === 0 ? (
        <p
          data-testid="batch-progress-empty"
          className="text-sm text-gray-500"
        >
          No batches submitted yet.
        </p>
      ) : (
        <ul className="divide-y divide-gray-200 rounded border border-gray-200">
          {runs.map((run) => {
            const open = !!expanded[run.batch_run_id]
            return (
              <li
                key={run.batch_run_id}
                data-testid={`batch-run-row-${run.batch_run_id}`}
                className="px-3 py-2"
              >
                <button
                  type="button"
                  onClick={() =>
                    setExpanded((s) => ({ ...s, [run.batch_run_id]: !s[run.batch_run_id] }))
                  }
                  aria-expanded={open}
                  className="flex w-full items-center justify-between gap-2"
                >
                  <span className="flex items-center gap-2">
                    {open ? (
                      <ChevronDown size={16} aria-hidden />
                    ) : (
                      <ChevronRight size={16} aria-hidden />
                    )}
                    <span className="font-mono text-sm">{run.batch_run_id}</span>
                    <StatusBadge status={run.status} />
                  </span>
                  <span className="text-xs text-gray-500">
                    {run.request_count} req · {formatDateRelative(run.submitted_at)}
                  </span>
                </button>
                {open && (
                  <BatchRunDetail run={run} onCancel={onCancel} />
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function DispatcherStatsRow({ stats }: { stats: DispatcherStats }): JSX.Element {
  return (
    <ul
      data-testid="batch-progress-stats"
      className="flex gap-3 text-xs text-gray-600"
    >
      <Stat label="Queued" value={stats.queued} />
      <Stat label="Active" value={stats.active_batches} />
      <Stat label="Submitted" value={stats.batches_submitted} />
      <Stat label="Results" value={stats.results_processed} />
      <Stat
        label="Errors"
        value={stats.errors_encountered}
        emphasis={stats.errors_encountered > 0}
      />
    </ul>
  )
}

function Stat({
  label,
  value,
  emphasis = false,
}: {
  label: string
  value: number
  emphasis?: boolean
}): JSX.Element {
  return (
    <li>
      <span className="text-gray-400">{label}</span>{" "}
      <span className={emphasis ? "font-semibold text-red-600" : "font-medium"}>
        {value}
      </span>
    </li>
  )
}

function StatusBadge({ status }: { status: BatchRunStatus }): JSX.Element {
  return (
    <span
      data-testid={`batch-status-${status}`}
      className={`rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_COLOR[status]}`}
    >
      {STATUS_LABEL[status]}
    </span>
  )
}

function BatchRunDetail({
  run,
  onCancel,
}: {
  run: BatchRun
  onCancel?: (id: string) => void | Promise<void>
}): JSX.Element {
  const counts = [
    { label: "Succeeded", value: run.success_count, emphasis: false },
    { label: "Errored", value: run.error_count, emphasis: run.error_count > 0 },
    { label: "Canceled", value: run.canceled_count, emphasis: false },
    { label: "Expired", value: run.expired_count, emphasis: run.expired_count > 0 },
  ]
  const inFlight = run.status === "pending" || run.status === "submitted"
  return (
    <div className="mt-2 space-y-2 pl-6 text-xs text-gray-700">
      <dl className="grid grid-cols-4 gap-2">
        {counts.map((c) => (
          <div key={c.label}>
            <dt className="text-gray-400">{c.label}</dt>
            <dd className={c.emphasis ? "font-semibold text-red-600" : ""}>
              {c.value}
            </dd>
          </div>
        ))}
      </dl>
      <dl className="space-y-0.5">
        {run.anthropic_batch_id && (
          <Row label="Anthropic batch" value={run.anthropic_batch_id} mono />
        )}
        {run.created_by && <Row label="Submitted by" value={run.created_by} />}
        <Row label="Submitted" value={formatDateRelative(run.submitted_at)} />
        {run.ended_at && (
          <Row label="Ended" value={formatDateRelative(run.ended_at)} />
        )}
        {run.expires_at && (
          <Row label="Expires" value={formatDateRelative(run.expires_at)} />
        )}
        {run.metadata && Object.keys(run.metadata).length > 0 && (
          <Row
            label="Metadata"
            value={JSON.stringify(run.metadata)}
            mono
          />
        )}
      </dl>
      {inFlight && onCancel && (
        <button
          type="button"
          data-testid={`batch-cancel-${run.batch_run_id}`}
          onClick={() => onCancel(run.batch_run_id)}
          className="rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-50"
        >
          Cancel batch
        </button>
      )}
    </div>
  )
}

function Row({
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
      <dt className="w-32 shrink-0 text-gray-400">{label}</dt>
      <dd className={mono ? "font-mono" : ""}>{value}</dd>
    </div>
  )
}

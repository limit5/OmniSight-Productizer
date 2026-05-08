"use client"

/**
 * MP.W4.3 - Provider Constellation project core centerpiece.
 *
 * Standalone circular tile for the center of the provider constellation.
 * The surrounding shell owns grid placement; this component is pure
 * presentation over the aggregate task summary.
 */

import { Network } from "lucide-react"

import type { ProviderConstellationTaskSummary } from "./ProviderConstellation"

interface ProjectCoreProps {
  taskSummary: ProviderConstellationTaskSummary
}

type CountKey =
  | "activeCount"
  | "activeTaskCount"
  | "activeTasks"
  | "queuedCount"
  | "queuedTaskCount"
  | "queuedTasks"

const ACTIVE_COUNT_KEYS: ReadonlyArray<CountKey> = [
  "activeCount",
  "activeTaskCount",
  "activeTasks",
]

const QUEUED_COUNT_KEYS: ReadonlyArray<CountKey> = [
  "queuedCount",
  "queuedTaskCount",
  "queuedTasks",
]

function readCount(
  taskSummary: ProviderConstellationTaskSummary,
  keys: ReadonlyArray<CountKey>,
  fallback: number,
): number {
  const values = taskSummary as ProviderConstellationTaskSummary &
    Partial<Record<CountKey, unknown>>

  for (const key of keys) {
    const value = values[key]
    if (typeof value === "number" && Number.isFinite(value)) {
      return Math.max(0, Math.trunc(value))
    }
  }

  return Math.max(0, Math.trunc(fallback))
}

function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(
    value,
  )
}

export default function ProjectCore({ taskSummary }: ProjectCoreProps) {
  const activeCount = readCount(
    taskSummary,
    ACTIVE_COUNT_KEYS,
    taskSummary.taskCount,
  )
  const queuedCount = readCount(taskSummary, QUEUED_COUNT_KEYS, 0)

  return (
    <section
      aria-label={`${taskSummary.title} task summary`}
      className="flex h-40 w-40 flex-col items-center justify-center rounded-full border border-[var(--neural-cyan,#67e8f9)]/65 bg-[var(--background,#020617)]/80 p-4 text-center shadow-[0_0_48px_rgba(103,232,249,0.22)] sm:h-48 sm:w-48"
      data-testid="project-core"
    >
      <div
        className="flex size-10 items-center justify-center rounded-full border border-[var(--neural-cyan,#67e8f9)]/35 bg-[var(--neural-cyan,#67e8f9)]/10"
        data-testid="project-core-brand-icon"
      >
        <Network
          aria-hidden
          className="size-5 text-[var(--neural-cyan,#67e8f9)]"
        />
      </div>

      <h3 className="mt-3 max-w-full truncate text-sm font-semibold text-[var(--foreground,#e2e8f0)]">
        {taskSummary.title}
      </h3>

      <dl className="mt-3 grid w-full grid-cols-2 gap-2 font-mono text-[10px] uppercase text-[var(--muted-foreground,#94a3b8)]">
        <div className="min-w-0 rounded-sm border border-white/10 bg-white/[0.03] px-2 py-1">
          <dt>Active</dt>
          <dd
            className="text-sm font-semibold text-[var(--neural-cyan,#67e8f9)]"
            data-testid="project-core-active-count"
          >
            {formatCount(activeCount)}
          </dd>
        </div>
        <div className="min-w-0 rounded-sm border border-white/10 bg-white/[0.03] px-2 py-1">
          <dt>Queued</dt>
          <dd
            className="text-sm font-semibold text-[var(--foreground,#e2e8f0)]"
            data-testid="project-core-queued-count"
          >
            {formatCount(queuedCount)}
          </dd>
        </div>
      </dl>
    </section>
  )
}

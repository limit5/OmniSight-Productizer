"use client"

/**
 * MP.W6.4 — Tasks Backlog Panel.
 *
 * War Room leaf panel for selecting backlog tasks before routing them
 * across the multi-provider orchestrator. The component is deliberately
 * presentation-only: callers own the task source and may control the
 * selected task ids, while this panel owns only the fallback uncontrolled
 * selection state.
 *
 * Module-global state audit:
 *   - No mutable module-level state.
 *   - Selection is per-component-instance React state when uncontrolled.
 *   - All derived counts are recomputed from props on render.
 */

import { useCallback, useMemo, useState } from "react"
import { CheckSquare, CircleDollarSign, ListChecks, Square } from "lucide-react"

export type TasksBacklogPriority = "critical" | "high" | "medium" | "low"

export type TasksBacklogStatus =
  | "backlog"
  | "analyzing"
  | "assigned"
  | "in_progress"
  | "in_review"
  | "completed"
  | "blocked"

export interface TasksBacklogItem {
  id: string
  title: string
  priority: TasksBacklogPriority
  status: TasksBacklogStatus
  estimatedTokens?: number
  estimatedCostUsd?: number
  providerHint?: string
}

export interface TasksBacklogPanelProps {
  tasks: ReadonlyArray<TasksBacklogItem>
  selectedTaskIds?: ReadonlyArray<string>
  defaultSelectedTaskIds?: ReadonlyArray<string>
  onSelectionChange?: (selectedTaskIds: string[]) => void
  className?: string
}

const PRIORITY_CLASS: Record<TasksBacklogPriority, string> = {
  critical: "bg-rose-400",
  high: "bg-amber-400",
  medium: "bg-cyan-400",
  low: "bg-slate-400",
}

const STATUS_LABEL: Record<TasksBacklogStatus, string> = {
  backlog: "Backlog",
  analyzing: "Analyzing",
  assigned: "Assigned",
  in_progress: "In progress",
  in_review: "In review",
  completed: "Completed",
  blocked: "Blocked",
}

function formatTokens(tokens: number | undefined): string {
  if (tokens === undefined || !Number.isFinite(tokens)) return "Estimate pending"
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(2)}M tokens`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K tokens`
  return `${Math.max(0, Math.trunc(tokens)).toLocaleString()} tokens`
}

function formatCost(cost: number | undefined): string {
  if (cost === undefined || !Number.isFinite(cost)) return "$--"
  return `$${cost.toFixed(cost >= 1 ? 2 : 3)}`
}

function cx(...classes: Array<string | false | undefined>): string {
  return classes.filter(Boolean).join(" ")
}

export function TasksBacklogPanel({
  tasks,
  selectedTaskIds,
  defaultSelectedTaskIds = [],
  onSelectionChange,
  className,
}: TasksBacklogPanelProps) {
  const [internalSelected, setInternalSelected] = useState<Set<string>>(
    () => new Set(defaultSelectedTaskIds),
  )

  const selectableIds = useMemo(
    () => tasks.filter((task) => task.status === "backlog").map((task) => task.id),
    [tasks],
  )
  const selected = useMemo(
    () => new Set(selectedTaskIds ?? Array.from(internalSelected)),
    [internalSelected, selectedTaskIds],
  )
  const selectedVisibleCount = selectableIds.filter((id) => selected.has(id)).length
  const allSelected =
    selectableIds.length > 0 &&
    selectableIds.every((id) => selected.has(id))

  const commitSelection = useCallback(
    (next: Set<string>) => {
      const ids = Array.from(next)
      if (selectedTaskIds === undefined) {
        setInternalSelected(next)
      }
      onSelectionChange?.(ids)
    },
    [onSelectionChange, selectedTaskIds],
  )

  const toggleTask = useCallback(
    (taskId: string) => {
      const next = new Set(selected)
      if (next.has(taskId)) {
        next.delete(taskId)
      } else {
        next.add(taskId)
      }
      commitSelection(next)
    },
    [commitSelection, selected],
  )

  const toggleAll = useCallback(() => {
    const next = new Set(selected)
    if (allSelected) {
      selectableIds.forEach((id) => next.delete(id))
    } else {
      selectableIds.forEach((id) => next.add(id))
    }
    commitSelection(next)
  }, [allSelected, commitSelection, selectableIds, selected])

  return (
    <section
      data-testid="mp-tasks-backlog-panel"
      className={cx(
        "holo-glass-simple corner-brackets-full flex min-h-[360px] flex-col overflow-hidden rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]",
        className,
      )}
      aria-labelledby="mp-tasks-backlog-panel-title"
      data-selected-count={selectedVisibleCount}
    >
      <header className="flex items-center justify-between gap-3 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <ListChecks
            className="h-4 w-4 shrink-0 text-[var(--neural-cyan,#67e8f9)]"
            aria-hidden
          />
          <h2
            id="mp-tasks-backlog-panel-title"
            className="truncate font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]"
          >
            TASKS BACKLOG
          </h2>
        </div>
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
          {selectedVisibleCount} / {selectableIds.length} selected
        </span>
      </header>

      <div className="border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
        <button
          type="button"
          onClick={toggleAll}
          disabled={selectableIds.length === 0}
          className="flex min-h-8 w-full items-center justify-between gap-3 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-white/[0.03] px-3 py-1.5 text-left font-mono text-xs text-[var(--foreground,#e2e8f0)] transition hover:border-[var(--neural-cyan,#67e8f9)]/50 hover:bg-[var(--neural-cyan,#67e8f9)]/10 disabled:cursor-not-allowed disabled:opacity-50"
          aria-pressed={allSelected}
          data-testid="mp-tasks-backlog-panel-select-all"
        >
          <span className="flex min-w-0 items-center gap-2">
            {allSelected ? (
              <CheckSquare className="h-4 w-4 shrink-0 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
            ) : (
              <Square className="h-4 w-4 shrink-0 text-[var(--muted-foreground,#94a3b8)]" aria-hidden />
            )}
            <span className="truncate">Select all backlog tasks</span>
          </span>
          <span className="shrink-0 text-[10px] uppercase tracking-[0.14em] text-[var(--muted-foreground,#94a3b8)]">
            Backlog only
          </span>
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {tasks.length === 0 ? (
          <div className="flex h-full min-h-48 flex-col items-center justify-center text-center">
            <ListChecks className="mb-2 h-7 w-7 text-[var(--muted-foreground,#94a3b8)] opacity-40" aria-hidden />
            <p className="font-mono text-xs uppercase tracking-[0.18em] text-[var(--muted-foreground,#94a3b8)]">
              No tasks
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {tasks.map((task) => {
              const selectable = task.status === "backlog"
              const checked = selected.has(task.id)

              return (
                <label
                  key={task.id}
                  className={cx(
                    "group flex gap-3 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-white/[0.03] p-3 transition",
                    selectable && "cursor-pointer hover:border-[var(--neural-cyan,#67e8f9)]/50 hover:bg-[var(--neural-cyan,#67e8f9)]/10",
                    checked && "border-[var(--neural-cyan,#67e8f9)]/70 bg-[var(--neural-cyan,#67e8f9)]/10",
                    !selectable && "opacity-60",
                  )}
                  data-testid={`mp-tasks-backlog-panel-task-${task.id}`}
                  data-selected={checked ? "true" : "false"}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    disabled={!selectable}
                    onChange={() => toggleTask(task.id)}
                    className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--neural-cyan,#67e8f9)]"
                    aria-label={`Select ${task.title}`}
                    data-testid={`mp-tasks-backlog-panel-checkbox-${task.id}`}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-start justify-between gap-3">
                      <span className="min-w-0">
                        <span className="block truncate font-mono text-xs font-semibold text-[var(--foreground,#e2e8f0)]">
                          {task.title}
                        </span>
                        <span className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--muted-foreground,#94a3b8)]">
                          <span className="flex items-center gap-1">
                            <span
                              className={cx("h-1.5 w-1.5 rounded-full", PRIORITY_CLASS[task.priority])}
                              aria-hidden
                            />
                            {task.priority}
                          </span>
                          <span>{STATUS_LABEL[task.status]}</span>
                          {task.providerHint && <span>{task.providerHint}</span>}
                        </span>
                      </span>
                      <span className="flex shrink-0 items-center gap-1 font-mono text-xs text-[var(--neural-cyan,#67e8f9)]">
                        <CircleDollarSign className="h-3.5 w-3.5" aria-hidden />
                        {formatCost(task.estimatedCostUsd)}
                      </span>
                    </span>
                    <span className="mt-2 block font-mono text-[11px] text-[var(--muted-foreground,#94a3b8)]">
                      {formatTokens(task.estimatedTokens)}
                    </span>
                  </span>
                </label>
              )
            })}
          </div>
        )}
      </div>
    </section>
  )
}

export default TasksBacklogPanel

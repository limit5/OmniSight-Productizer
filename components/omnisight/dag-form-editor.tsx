"use client"

/**
 * Phase 56-DAG-F — Form-based DAG authoring.
 *
 * A row-per-task editor for operators who don't want to hand-write JSON.
 * Controlled component: the parent owns the DAG object and passes it in
 * via `value`; every mutation goes through `onChange` so the sibling
 * JSON view (DAG-E) and this form stay in lockstep.
 *
 * Deliberate omissions (keep the first pass small):
 *   - Free-text `toolchain` (no backend enum exposed yet).
 *   - `inputs` and `output_overlap_ack` edited in JSON tab only —
 *     they're rare-path knobs; promoting them to the form would add
 *     noise to the 95% case.
 *   - Validation errors render in the parent (DagAuthoringPanel) so
 *     the JSON tab shares the same panel.
 */

import { useCallback, useMemo } from "react"
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react"

// Mirrors `backend/dag_schema.py`. Kept local rather than hoisting into
// lib/api.ts because only the form editor needs this shape concrete
// (the API surface exchanges plain objects).
export interface FormTask {
  task_id: string
  description: string
  required_tier: "t1" | "networked" | "t3"
  toolchain: string
  expected_output: string
  depends_on: string[]
  inputs?: string[]
  output_overlap_ack?: boolean
}

export interface FormDAG {
  schema_version: number
  dag_id: string
  tasks: FormTask[]
  total_tasks?: number
}

interface Props {
  value: FormDAG
  onChange: (next: FormDAG) => void
}

const TIERS: FormTask["required_tier"][] = ["t1", "networked", "t3"]

function blankTask(index: number, allIds: string[]): FormTask {
  let id = `task_${index + 1}`
  let suffix = index + 1
  while (allIds.includes(id)) {
    suffix += 1
    id = `task_${suffix}`
  }
  return {
    task_id: id,
    description: "",
    required_tier: "t1",
    toolchain: "",
    expected_output: "",
    depends_on: [],
  }
}

export function DagFormEditor({ value, onChange }: Props) {
  // ─── mutation helpers ──────────────────────────────────────────

  const patchDag = useCallback(
    (patch: Partial<FormDAG>) => onChange({ ...value, ...patch }),
    [value, onChange],
  )

  const patchTask = useCallback(
    (idx: number, patch: Partial<FormTask>) => {
      const tasks = value.tasks.map((t, i) => (i === idx ? { ...t, ...patch } : t))
      onChange({ ...value, tasks })
    },
    [value, onChange],
  )

  const addTask = () => {
    const allIds = value.tasks.map((t) => t.task_id)
    onChange({ ...value, tasks: [...value.tasks, blankTask(value.tasks.length, allIds)] })
  }

  const removeTask = (idx: number) => {
    const removed = value.tasks[idx]?.task_id
    const tasks = value.tasks
      .filter((_, i) => i !== idx)
      // Dropping a task means every downstream depends_on that points at
      // it becomes an `unknown_dep`. Scrub to keep form validity high.
      .map((t) => ({ ...t, depends_on: t.depends_on.filter((d) => d !== removed) }))
    onChange({ ...value, tasks })
  }

  const moveTask = (idx: number, delta: -1 | 1) => {
    const dst = idx + delta
    if (dst < 0 || dst >= value.tasks.length) return
    const tasks = [...value.tasks]
    ;[tasks[idx], tasks[dst]] = [tasks[dst], tasks[idx]]
    onChange({ ...value, tasks })
  }

  const toggleDep = (idx: number, depId: string) => {
    const t = value.tasks[idx]
    const has = t.depends_on.includes(depId)
    const depends_on = has
      ? t.depends_on.filter((d) => d !== depId)
      : [...t.depends_on, depId]
    patchTask(idx, { depends_on })
  }

  // ─── derived ───────────────────────────────────────────────────

  // A task can only depend on tasks that appear before it in the list.
  // This isn't a backend rule (the validator only cares about cycles),
  // but it's a useful UX guard because listing order == topological
  // intention. We still show all other ids so the operator can reorder.
  const allIds = useMemo(() => value.tasks.map((t) => t.task_id), [value.tasks])

  // ─── render ────────────────────────────────────────────────────

  return (
    <div className="flex flex-col gap-3">
      {/* DAG-level fields */}
      <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 items-center">
        <label className="text-xs font-mono text-[var(--muted-foreground)]">dag_id</label>
        <input
          type="text"
          value={value.dag_id}
          onChange={(e) => patchDag({ dag_id: e.target.value })}
          className="text-xs font-mono px-2 py-1 rounded bg-[var(--background)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
          placeholder="REQ-my-dag"
          aria-label="dag_id"
        />
      </div>

      {/* Task rows */}
      <div className="flex flex-col gap-2">
        {value.tasks.map((t, idx) => (
          <div
            key={idx}
            className="rounded border border-[var(--border)] bg-[var(--background)] p-2 flex flex-col gap-1"
          >
            {/* Row 1: id + tier + reorder + delete */}
            <div className="flex items-center gap-1">
              <input
                type="text"
                value={t.task_id}
                onChange={(e) => patchTask(idx, { task_id: e.target.value })}
                placeholder="task_id"
                aria-label={`task ${idx + 1} id`}
                className="flex-1 text-xs font-mono px-2 py-0.5 rounded bg-[var(--card)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
              />
              <select
                value={t.required_tier}
                onChange={(e) =>
                  patchTask(idx, { required_tier: e.target.value as FormTask["required_tier"] })
                }
                aria-label={`task ${idx + 1} tier`}
                className="text-xs font-mono px-1 py-0.5 rounded bg-[var(--card)] border border-[var(--border)] text-[var(--foreground)]"
              >
                {TIERS.map((tier) => (
                  <option key={tier} value={tier}>{tier}</option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => moveTask(idx, -1)}
                disabled={idx === 0}
                aria-label={`move task ${idx + 1} up`}
                className="p-1 rounded border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--muted)] disabled:opacity-30"
              >
                <ArrowUp size={10} />
              </button>
              <button
                type="button"
                onClick={() => moveTask(idx, 1)}
                disabled={idx === value.tasks.length - 1}
                aria-label={`move task ${idx + 1} down`}
                className="p-1 rounded border border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--muted)] disabled:opacity-30"
              >
                <ArrowDown size={10} />
              </button>
              <button
                type="button"
                onClick={() => removeTask(idx)}
                aria-label={`remove task ${idx + 1}`}
                className="p-1 rounded border border-[var(--destructive)]/40 text-[var(--destructive)] hover:bg-[var(--destructive)]/10"
              >
                <Trash2 size={10} />
              </button>
            </div>

            {/* Row 2: toolchain + expected_output */}
            <div className="grid grid-cols-2 gap-1">
              <input
                type="text"
                value={t.toolchain}
                onChange={(e) => patchTask(idx, { toolchain: e.target.value })}
                placeholder="toolchain (e.g. cmake)"
                aria-label={`task ${idx + 1} toolchain`}
                className="text-xs font-mono px-2 py-0.5 rounded bg-[var(--card)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
              />
              <input
                type="text"
                value={t.expected_output}
                onChange={(e) => patchTask(idx, { expected_output: e.target.value })}
                placeholder="expected_output (path | git:SHA | issue:ID)"
                aria-label={`task ${idx + 1} expected_output`}
                className="text-xs font-mono px-2 py-0.5 rounded bg-[var(--card)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
              />
            </div>

            {/* Row 3: description */}
            <input
              type="text"
              value={t.description}
              onChange={(e) => patchTask(idx, { description: e.target.value })}
              placeholder="description — what this task does"
              aria-label={`task ${idx + 1} description`}
              className="text-xs font-mono px-2 py-0.5 rounded bg-[var(--card)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
            />

            {/* Row 4: depends_on chips */}
            {allIds.length > 1 && (
              <div className="flex flex-wrap gap-1 items-center">
                <span className="text-[10px] font-mono text-[var(--muted-foreground)] mr-1">depends_on:</span>
                {allIds.filter((id) => id !== t.task_id).map((id) => {
                  const on = t.depends_on.includes(id)
                  return (
                    <button
                      key={id}
                      type="button"
                      onClick={() => toggleDep(idx, id)}
                      aria-pressed={on}
                      className={
                        "text-[10px] font-mono px-1.5 py-0.5 rounded border transition-colors " +
                        (on
                          ? "bg-[var(--artifact-purple)] text-white border-[var(--artifact-purple)]"
                          : "border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--muted)]")
                      }
                    >
                      {id}
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Add */}
      <button
        type="button"
        onClick={addTask}
        className="self-start text-xs font-mono px-2 py-1 rounded border border-[var(--artifact-purple)] text-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)] hover:text-white flex items-center gap-1"
      >
        <Plus size={10} /> Add task
      </button>
    </div>
  )
}

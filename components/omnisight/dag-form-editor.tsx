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

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ArrowDown, ArrowUp, Plus, Trash2, X } from "lucide-react"

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
  /** Phase 56-DAG-G follow-up: when bumped (new `n`), scroll the row
   * for `taskId` into view and flash a highlight ring. DagEditor sets
   * this in response to the `omnisight:dag-focus-task` event emitted
   * by DagCanvas clicks. A counter rather than a plain string so the
   * same id twice in a row still re-fires. */
  focusRequest?: { taskId: string; n: number } | null
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

export function DagFormEditor({ value, onChange, focusRequest }: Props) {
  // Per-row draft for the "add input" text field. Stored outside the
  // DAG itself so an empty draft doesn't serialise back into the JSON
  // text tab. Keyed by task index — cleared on commit or row delete.
  const [inputDraft, setInputDraft] = useState<Record<number, string>>({})

  // Refs to every task row DOM node, for scroll-to-row on focus request.
  const rowRefs = useRef<Record<string, HTMLDivElement | null>>({})
  // Task id currently flashing the highlight ring (null otherwise).
  const [flashed, setFlashed] = useState<string | null>(null)

  useEffect(() => {
    if (!focusRequest) return
    const node = rowRefs.current[focusRequest.taskId]
    if (!node) return
    node.scrollIntoView({ behavior: "smooth", block: "center" })
    setFlashed(focusRequest.taskId)
    const t = setTimeout(() => setFlashed(null), 1500)
    return () => clearTimeout(t)
  }, [focusRequest])

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
    // Clear any draft typeahead tied to the removed row index so it
    // doesn't get misapplied to whatever shifts into that slot.
    setInputDraft((d) => {
      const next = { ...d }
      delete next[idx]
      return next
    })
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

  // ─── inputs[] helpers ──────────────────────────────────────────
  // Chip-with-typeahead: the draft text commits on Enter / blur, dups
  // are dropped silently so the operator can't trip unknown_input in
  // the validator. Empty string is a no-op.

  const addInput = (idx: number) => {
    const draft = (inputDraft[idx] || "").trim()
    if (!draft) return
    const t = value.tasks[idx]
    const inputs = t.inputs ?? []
    if (inputs.includes(draft)) {
      setInputDraft((d) => ({ ...d, [idx]: "" }))
      return
    }
    patchTask(idx, { inputs: [...inputs, draft] })
    setInputDraft((d) => ({ ...d, [idx]: "" }))
  }

  const removeInput = (idx: number, val: string) => {
    const t = value.tasks[idx]
    const inputs = (t.inputs ?? []).filter((x) => x !== val)
    patchTask(idx, { inputs })
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
            ref={(el) => {
              rowRefs.current[t.task_id] = el
            }}
            data-task-row-id={t.task_id}
            className={
              "rounded border p-2 flex flex-col gap-1 transition-shadow " +
              (flashed === t.task_id
                ? "border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 shadow-[0_0_0_2px_var(--artifact-purple)]"
                : "border-[var(--border)] bg-[var(--background)]")
            }
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

            {/* Row 4: inputs chips + typeahead */}
            <div className="flex flex-wrap gap-1 items-center">
              <span className="text-[10px] font-mono text-[var(--muted-foreground)] mr-1">
                inputs:
              </span>
              {(t.inputs ?? []).map((inp) => (
                <span
                  key={inp}
                  className="inline-flex items-center gap-0.5 text-[10px] font-mono px-1.5 py-0.5 rounded border border-[var(--border)] bg-[var(--muted)]/30 text-[var(--foreground)]"
                >
                  {inp}
                  <button
                    type="button"
                    onClick={() => removeInput(idx, inp)}
                    aria-label={`remove input ${inp} from task ${idx + 1}`}
                    className="ml-0.5 opacity-60 hover:opacity-100"
                  >
                    <X size={9} />
                  </button>
                </span>
              ))}
              <input
                type="text"
                value={inputDraft[idx] || ""}
                onChange={(e) =>
                  setInputDraft((d) => ({ ...d, [idx]: e.target.value }))
                }
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault()
                    addInput(idx)
                  }
                }}
                onBlur={() => addInput(idx)}
                placeholder="add input path (press Enter)"
                aria-label={`task ${idx + 1} new input`}
                className="flex-1 min-w-[120px] text-[10px] font-mono px-1.5 py-0.5 rounded bg-[var(--card)] border border-[var(--border)] text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--artifact-purple)]"
              />
            </div>

            {/* Row 5: output_overlap_ack — MECE escape hatch.
                Rarely used, so visually small and titled with the why. */}
            <label
              className="flex items-center gap-1 text-[10px] font-mono text-[var(--muted-foreground)] select-none"
              title="Allow this task's expected_output path to overlap with another task's. The DAG validator's MECE rule will refuse overlapping outputs unless BOTH sides set this flag (e.g. parallel benchmarks writing the same merged report)."
            >
              <input
                type="checkbox"
                checked={!!t.output_overlap_ack}
                onChange={(e) =>
                  patchTask(idx, { output_overlap_ack: e.target.checked })
                }
                aria-label={`task ${idx + 1} output overlap ack`}
                className="accent-[var(--artifact-purple)]"
              />
              <span>allow output overlap (MECE escape)</span>
            </label>

            {/* Row 6: depends_on chips */}
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

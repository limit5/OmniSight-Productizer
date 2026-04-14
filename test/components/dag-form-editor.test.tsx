/**
 * Phase 56-DAG-F — DagFormEditor (controlled) + tab round-trip tests.
 *
 * Covers the invariants that matter:
 *   1. Rendering a FormDAG paints one row per task.
 *   2. Editing a field fires onChange with the patched task.
 *   3. "Add task" appends a row with a unique task_id.
 *   4. Deleting a task scrubs every downstream depends_on pointing at it.
 *   5. depends_on chips toggle membership.
 *   6. Tab switch in DagEditor: JSON → Form → edit → back to JSON shows
 *      the patched DAG without losing work.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { DagFormEditor, type FormDAG } from "@/components/omnisight/dag-form-editor"

const sample: FormDAG = {
  schema_version: 1,
  dag_id: "REQ-test",
  tasks: [
    {
      task_id: "a",
      description: "A desc",
      required_tier: "t1",
      toolchain: "cmake",
      expected_output: "build/a.bin",
      depends_on: [],
    },
    {
      task_id: "b",
      description: "B desc",
      required_tier: "t3",
      toolchain: "flash_board",
      expected_output: "logs/b.log",
      depends_on: ["a"],
    },
  ],
}

describe("DagFormEditor", () => {
  it("renders one row per task with aria-labelled fields", () => {
    const onChange = vi.fn()
    render(<DagFormEditor value={sample} onChange={onChange} />)
    expect(screen.getByLabelText("task 1 id")).toHaveValue("a")
    expect(screen.getByLabelText("task 2 id")).toHaveValue("b")
    expect(screen.getByLabelText("task 2 tier")).toHaveValue("t3")
  })

  it("editing task_id fires onChange with the patched task", () => {
    const onChange = vi.fn()
    render(<DagFormEditor value={sample} onChange={onChange} />)
    fireEvent.change(screen.getByLabelText("task 1 id"), { target: { value: "compile" } })
    expect(onChange).toHaveBeenCalledTimes(1)
    const next = onChange.mock.calls[0][0] as FormDAG
    expect(next.tasks[0].task_id).toBe("compile")
    expect(next.tasks[1].task_id).toBe("b") // sibling untouched
  })

  it("Add task appends a row with a unique id", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<DagFormEditor value={sample} onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: /add task/i }))
    const next = onChange.mock.calls[0][0] as FormDAG
    expect(next.tasks).toHaveLength(3)
    expect(next.tasks[2].task_id).toBe("task_3")
    // The new id must not collide with existing ones.
    const ids = next.tasks.map((t) => t.task_id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it("deleting a task scrubs its id from downstream depends_on", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<DagFormEditor value={sample} onChange={onChange} />)
    // Delete task 1 (id=a); task 2 should lose "a" from its depends_on.
    await user.click(screen.getByRole("button", { name: /remove task 1/i }))
    const next = onChange.mock.calls[0][0] as FormDAG
    expect(next.tasks).toHaveLength(1)
    expect(next.tasks[0].task_id).toBe("b")
    expect(next.tasks[0].depends_on).toEqual([])
  })

  it("depends_on chip toggles membership", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<DagFormEditor value={sample} onChange={onChange} />)
    // Task 2 currently depends on a. Clicking the "a" chip should remove it.
    // There are two "a" elements (task 1's id input + task 2's chip) — pick
    // the pressed chip.
    const chip = screen.getByRole("button", { pressed: true, name: "a" })
    await user.click(chip)
    const next = onChange.mock.calls[0][0] as FormDAG
    expect(next.tasks[1].depends_on).toEqual([])
  })
})

// ─── Tab round-trip (DagEditor integration) ───────────────────────

vi.mock("@/lib/api", () => ({
  validateDag: vi.fn().mockResolvedValue({ ok: true, stage: "semantic", errors: [] }),
  submitDag: vi.fn(),
}))

import { DagEditor } from "@/components/omnisight/dag-editor"

describe("DagEditor tab round-trip", () => {
  beforeEach(() => vi.clearAllMocks())

  it("edits made in Form tab survive a flip back to JSON", async () => {
    const user = userEvent.setup()
    render(<DagEditor />)
    // Switch to Form
    await user.click(screen.getByRole("tab", { name: /form/i }))
    // Rename the only task
    await waitFor(() => expect(screen.getByLabelText("task 1 id")).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText("task 1 id"), { target: { value: "renamed_task" } })
    // Flip back to JSON — the textarea should contain the new id.
    await user.click(screen.getByRole("tab", { name: /json/i }))
    const ta = screen.getByRole("textbox", { name: /dag json editor/i }) as HTMLTextAreaElement
    expect(ta.value).toContain("renamed_task")
  })

  it("Form tab shows a fix-JSON nudge when the JSON is unparseable", async () => {
    const user = userEvent.setup()
    render(<DagEditor />)
    const ta = screen.getByRole("textbox", { name: /dag json editor/i }) as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: "{ not-json" } })
    await user.click(screen.getByRole("tab", { name: /form/i }))
    expect(screen.getByText(/not parseable/i)).toBeInTheDocument()
  })
})

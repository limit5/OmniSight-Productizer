/**
 * Phase 56-DAG-E — DagEditor component tests.
 *
 * Invariants that matter in production:
 *   1. First render paints the default template and kicks off validate.
 *   2. A malformed JSON edit surfaces a parse error without hitting the
 *      network (the textarea can't produce a valid DAG).
 *   3. When validation returns errors, each rule row is shown and the
 *      Submit button stays disabled.
 *   4. When validation returns ok, Submit is enabled and clicking it
 *      POSTs the parsed DAG.
 *   5. Loading a template replaces the editor contents.
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  validateDag: vi.fn(),
  submitDag: vi.fn(),
}))

import { DagEditor } from "@/components/omnisight/dag-editor"
import * as api from "@/lib/api"

const mockValidate = api.validateDag as ReturnType<typeof vi.fn>
const mockSubmit = api.submitDag as ReturnType<typeof vi.fn>

describe("DagEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockValidate.mockResolvedValue({ ok: true, stage: "semantic", errors: [], task_count: 1 })
    mockSubmit.mockResolvedValue({
      run_id: "wf-xyz", plan_id: 42, status: "executing", validation_errors: [],
    })
  })

  it("paints the default template and validates on mount", async () => {
    render(<DagEditor />)
    // Default template body shows up in the textarea.
    const ta = screen.getByRole("textbox", { name: /dag json editor/i }) as HTMLTextAreaElement
    expect(ta.value).toContain("SAMPLE-minimal")
    await waitFor(() => expect(mockValidate).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText(/valid/i)).toBeInTheDocument())
  })

  it("surfaces a JSON parse error without calling validateDag", async () => {
    const user = userEvent.setup()
    render(<DagEditor />)
    const ta = screen.getByRole("textbox", { name: /dag json editor/i }) as HTMLTextAreaElement
    await waitFor(() => expect(mockValidate).toHaveBeenCalled())
    mockValidate.mockClear()

    fireEvent.change(ta, { target: { value: "{ not-json" } })
    await waitFor(() => expect(screen.getByText(/1 error/i)).toBeInTheDocument())
    // Parse error rule should be surfaced
    expect(screen.getByText(/json_parse/i)).toBeInTheDocument()
    // The validator must NOT have been hit for unparseable content.
    expect(mockValidate).not.toHaveBeenCalled()
    void user  // referenced to keep the import used in other tests
  })

  it("shows each semantic rule error and disables Submit", async () => {
    mockValidate.mockResolvedValue({
      ok: false,
      stage: "semantic",
      errors: [
        { rule: "tier_violation", task_id: "X", message: "flash_board denied on t1" },
        { rule: "cycle", task_id: null, message: "cycle: A → B → A" },
      ],
    })
    render(<DagEditor />)
    await waitFor(() => expect(screen.getByText(/2 errors/i)).toBeInTheDocument())
    expect(screen.getByText(/tier_violation/i)).toBeInTheDocument()
    expect(screen.getAllByText(/cycle/i).length).toBeGreaterThan(0)
    const submit = screen.getByRole("button", { name: /submit/i })
    expect(submit).toBeDisabled()
  })

  it("enables Submit on valid DAG and POSTs on click", async () => {
    const user = userEvent.setup()
    render(<DagEditor />)
    await waitFor(() => expect(screen.getByText(/valid/i)).toBeInTheDocument())
    const submit = screen.getByRole("button", { name: /submit/i })
    await waitFor(() => expect(submit).not.toBeDisabled())
    await user.click(submit)
    await waitFor(() => expect(mockSubmit).toHaveBeenCalled())
    // The payload is the parsed minimal-template DAG.
    const [parsed, opts] = mockSubmit.mock.calls[0]
    expect((parsed as { dag_id: string }).dag_id).toBe("SAMPLE-minimal")
    expect(opts).toEqual({ mutate: false })
    await waitFor(() => expect(screen.getByText(/wf-xyz/)).toBeInTheDocument())
  })

  it("after successful submit, View in Timeline dispatches navigate event", async () => {
    const user = userEvent.setup()
    render(<DagEditor />)
    await waitFor(() => expect(screen.getByText(/valid/i)).toBeInTheDocument())
    const submit = screen.getByRole("button", { name: /submit/i })
    await waitFor(() => expect(submit).not.toBeDisabled())

    const navListener = vi.fn()
    window.addEventListener("omnisight:navigate", navListener as EventListener)
    try {
      await user.click(submit)
      await waitFor(() => expect(screen.getByRole("button", { name: /view in timeline/i })).toBeInTheDocument())
      await user.click(screen.getByRole("button", { name: /view in timeline/i }))
      expect(navListener).toHaveBeenCalledTimes(1)
      const ev = navListener.mock.calls[0][0] as CustomEvent<{ panel: string }>
      expect(ev.detail.panel).toBe("timeline")
    } finally {
      window.removeEventListener("omnisight:navigate", navListener as EventListener)
    }
  })

  it("dag-seed-from-spec event seeds JSON tab from SpecTemplateEditor handoff", async () => {
    render(<DagEditor />)
    // Wait for initial template to load.
    await waitFor(() => expect(screen.getByText(/valid/i)).toBeInTheDocument())

    // Fire the seed event with an embedded_firmware spec → expect
    // the cross-compile template to be selected.
    window.dispatchEvent(new CustomEvent("omnisight:dag-seed-from-spec", {
      detail: {
        spec: {
          project_type: { value: "embedded_firmware", confidence: 0.9 },
          runtime_model: { value: "unknown", confidence: 0 },
          framework: { value: "embedded", confidence: 0.7 },
        },
      },
    }))

    const ta = await screen.findByRole("textbox", { name: /dag json editor/i }) as HTMLTextAreaElement
    await waitFor(() => expect(ta.value).toContain("SAMPLE-cross-compile"))
    // Seed message surfaces so the operator knows what happened.
    expect(screen.getByText(/Seeded from spec/i)).toBeInTheDocument()
  })

  it("dag-focus-task event flips to Form tab and scrolls/highlights the row", async () => {
    const scrollSpy = vi.fn()
    // jsdom doesn't implement scrollIntoView; stub before render so
    // the effect doesn't throw.
    Element.prototype.scrollIntoView = scrollSpy as unknown as Element["scrollIntoView"]

    const user = userEvent.setup()
    render(<DagEditor />)
    // Wait for the default template to parse / validate.
    await waitFor(() => expect(screen.getByText(/valid/i)).toBeInTheDocument())

    // Fire the focus event the Canvas would emit on click.
    window.dispatchEvent(
      new CustomEvent("omnisight:dag-focus-task", { detail: { taskId: "compile" } }),
    )

    // Tab should flip to Form and the row should be in the document.
    await waitFor(() => {
      expect(screen.getByLabelText("task 1 id")).toBeInTheDocument()
    })
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled())
  })

  it("loads a template when the chip is clicked", async () => {
    const user = userEvent.setup()
    render(<DagEditor />)
    const ta = screen.getByRole("textbox", { name: /dag json editor/i }) as HTMLTextAreaElement
    await waitFor(() => expect(mockValidate).toHaveBeenCalled())
    await user.click(screen.getByRole("button", { name: /fan-out/i }))
    expect(ta.value).toContain("SAMPLE-fanout")
    expect(ta.value).toContain("sim_npu")
  })
})

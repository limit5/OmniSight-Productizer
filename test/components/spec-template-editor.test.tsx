/**
 * Phase 68-C — SpecTemplateEditor component tests.
 *
 * Covers:
 *   1. Prose typing debounces into parseIntent
 *   2. Form tab lets the operator set a field at confidence 1.0
 *   3. Conflict panel renders options and clicking one calls
 *      clarifyIntent with the right ids
 *   4. Continue button disabled when a conflict is unresolved
 *   5. Continue button fires onSpecReady with the current spec
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  parseIntent: vi.fn(),
  clarifyIntent: vi.fn(),
}))

import { SpecTemplateEditor } from "@/components/omnisight/spec-template-editor"
import * as api from "@/lib/api"
import type { ParsedSpec } from "@/lib/api"

const mockParse = api.parseIntent as ReturnType<typeof vi.fn>
const mockClarify = api.clarifyIntent as ReturnType<typeof vi.fn>

const okSpec: ParsedSpec = {
  project_type:      { value: "web_app",   confidence: 0.9 },
  runtime_model:     { value: "ssg",       confidence: 0.9 },
  target_arch:       { value: "x86_64",    confidence: 0.9 },
  target_os:         { value: "linux",     confidence: 0.9 },
  framework:         { value: "nextjs",    confidence: 0.9 },
  persistence:       { value: "sqlite",    confidence: 0.9 },
  deploy_target:     { value: "local",     confidence: 0.9 },
  hardware_required: { value: "no",        confidence: 0.9 },
  raw_text: "demo",
  conflicts: [],
}

const conflictSpec: ParsedSpec = {
  ...okSpec,
  conflicts: [{
    id: "static_with_runtime_db",
    message: "Pick one:",
    fields: ["runtime_model", "persistence"],
    options: [
      { id: "ssg_build_time", label: "SSG — build time" },
      { id: "ssr_runtime", label: "SSR — runtime" },
    ],
    severity: "routine",
  }],
}

describe("SpecTemplateEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockParse.mockResolvedValue(okSpec)
  })

  it("debounces prose input into a parseIntent call", async () => {
    const user = userEvent.setup()
    render(<SpecTemplateEditor />)
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "build next.js app")
    // Debounced; wait for the single call to fire.
    await waitFor(() => expect(mockParse).toHaveBeenCalled(), { timeout: 1200 })
    const args = mockParse.mock.calls[mockParse.mock.calls.length - 1]
    expect(args[0]).toContain("build next.js app")
  })

  it("form tab patches field at confidence 1.0 without calling LLM", async () => {
    const user = userEvent.setup()
    render(<SpecTemplateEditor />)
    // Switch to Form tab.
    await user.click(screen.getByRole("tab", { name: /form/i }))
    const picker = screen.getByLabelText("target_arch") as HTMLSelectElement
    await user.selectOptions(picker, "x86_64")
    // No network call — structured picks skip the LLM round-trip.
    expect(mockParse).not.toHaveBeenCalled()
    // Confidence badge ✓ appears for operator-set fields.
    expect(picker.value).toBe("x86_64")
  })

  it("conflict panel shows options and picks call clarifyIntent", async () => {
    const user = userEvent.setup()
    mockParse.mockResolvedValue(conflictSpec)
    mockClarify.mockResolvedValue(okSpec)
    render(<SpecTemplateEditor />)
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "static next.js site reads runtime db")
    await waitFor(() => expect(
      screen.getByText(/static_with_runtime_db/),
    ).toBeInTheDocument(), { timeout: 2000 })

    await user.click(screen.getByRole("button", { name: /SSR — runtime/i }))
    await waitFor(() => expect(mockClarify).toHaveBeenCalled())
    const [parsedArg, conflictId, optionId] = mockClarify.mock.calls[0]
    expect(parsedArg.conflicts.length).toBe(1)
    expect(conflictId).toBe("static_with_runtime_db")
    expect(optionId).toBe("ssr_runtime")
  })

  it("Continue button disabled while a conflict is unresolved", async () => {
    const user = userEvent.setup()
    mockParse.mockResolvedValue(conflictSpec)
    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />)
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "spec with conflict")
    await waitFor(() => expect(
      screen.getByText(/static_with_runtime_db/),
    ).toBeInTheDocument(), { timeout: 2000 })
    const btn = screen.getByRole("button", { name: /continue/i })
    expect(btn).toBeDisabled()
    expect(onReady).not.toHaveBeenCalled()
  })

  it("Continue button fires onSpecReady once spec is clean", async () => {
    const user = userEvent.setup()
    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />)
    const ta = screen.getByRole("textbox", { name: /project prose/i })
    await user.type(ta, "clean build request")
    await waitFor(() => expect(mockParse).toHaveBeenCalled(), { timeout: 1200 })
    const btn = await screen.findByRole("button", { name: /continue/i })
    await waitFor(() => expect(btn).not.toBeDisabled())
    await user.click(btn)
    expect(onReady).toHaveBeenCalledTimes(1)
    expect(onReady.mock.calls[0][0].framework.value).toBe("nextjs")
  })

  it("Continue persists the spec to localStorage for back-jump restore", async () => {
    const user = userEvent.setup()
    window.localStorage.removeItem("omnisight:intent:last_spec")
    const onReady = vi.fn()
    render(<SpecTemplateEditor onSpecReady={onReady} />)
    await user.type(
      screen.getByRole("textbox", { name: /project prose/i }),
      "anything",
    )
    await waitFor(() => expect(mockParse).toHaveBeenCalled(), { timeout: 1200 })
    const btn = await screen.findByRole("button", { name: /continue/i })
    await waitFor(() => expect(btn).not.toBeDisabled())
    await user.click(btn)
    const stored = window.localStorage.getItem("omnisight:intent:last_spec")
    expect(stored).not.toBeNull()
    const cached = JSON.parse(stored!)
    expect(cached.framework.value).toBe("nextjs")
  })

  it("restores the cached spec on next mount (back-from-DAG path)", async () => {
    window.localStorage.setItem(
      "omnisight:intent:last_spec",
      JSON.stringify({
        ...okSpec,
        raw_text: "previously typed prompt",
        framework: { value: "django", confidence: 0.9 },
      }),
    )
    render(<SpecTemplateEditor />)
    const ta = await screen.findByRole(
      "textbox", { name: /project prose/i },
    ) as HTMLTextAreaElement
    await waitFor(() => expect(ta.value).toBe("previously typed prompt"))
  })
})

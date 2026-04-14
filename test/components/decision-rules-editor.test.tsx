/**
 * Phase 50B — DecisionRulesEditor tests.
 *
 * 1. Initial load paints existing rules in priority order
 * 2. ADD row + SAVE PUTs the new rule
 * 3. Dry-run button POSTs kinds to /test and renders hits
 * 4. Error banner surfaces a validation failure from the backend
 */

import { describe, expect, it, vi, beforeEach } from "vitest"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

vi.mock("@/lib/api", () => ({
  getDecisionRules: vi.fn(),
  putDecisionRules: vi.fn(),
  testDecisionRules: vi.fn(),
}))

import { DecisionRulesEditor } from "@/components/omnisight/decision-rules-editor"
import * as api from "@/lib/api"
import type { DecisionRule } from "@/lib/api"

function mkRule(overrides: Partial<DecisionRule> = {}): DecisionRule {
  return {
    id: "rule-1", kind_pattern: "stuck/*", severity: "risky",
    auto_in_modes: [], default_option_id: null,
    priority: 10, enabled: true, note: "",
    ...overrides,
  }
}

const VOCAB = {
  severities: ["info", "routine", "risky", "destructive"] as const,
  modes: ["manual", "supervised", "full_auto", "turbo"] as const,
}

describe("DecisionRulesEditor", () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("renders existing rules in priority order on mount", async () => {
    ;(api.getDecisionRules as ReturnType<typeof vi.fn>).mockResolvedValue({
      rules: [
        mkRule({ id: "rule-late", kind_pattern: "budget/*", priority: 50 }),
        mkRule({ id: "rule-early", kind_pattern: "stuck/*", priority: 5 }),
      ],
      severities: [...VOCAB.severities],
      modes: [...VOCAB.modes],
    })
    render(<DecisionRulesEditor />)
    await waitFor(() => {
      const rows = screen.getAllByTestId(/^rule-row-/)
      expect(rows).toHaveLength(2)
      // Priority 5 (early) first.
      expect(rows[0].getAttribute("data-testid")).toBe("rule-row-rule-early")
    })
  })

  it("ADD + SAVE PUTs the new rule with a kind_pattern", async () => {
    const user = userEvent.setup()
    ;(api.getDecisionRules as ReturnType<typeof vi.fn>).mockResolvedValue({
      rules: [], severities: [...VOCAB.severities], modes: [...VOCAB.modes],
    })
    ;(api.putDecisionRules as ReturnType<typeof vi.fn>).mockResolvedValue({
      rules: [mkRule({ id: "rule-saved", kind_pattern: "ambiguity/*" })],
    })
    render(<DecisionRulesEditor />)
    await screen.findByText(/no rules/i)
    await user.click(screen.getByRole("button", { name: /add rule/i }))
    const kindInput = screen.getByPlaceholderText("e.g. stuck/*")
    await user.type(kindInput, "ambiguity/*")
    await user.click(screen.getByRole("button", { name: /save/i }))
    expect(api.putDecisionRules).toHaveBeenCalledTimes(1)
    const put = (api.putDecisionRules as ReturnType<typeof vi.fn>).mock.calls[0][0]
    expect(Array.isArray(put)).toBe(true)
    expect(put[0].kind_pattern).toBe("ambiguity/*")
    // Local tmp- ids are stripped before PUT so the backend assigns stable ones.
    expect(put[0].id).toBeUndefined()
  })

  it("dry-run button posts kinds and renders each hit", async () => {
    const user = userEvent.setup()
    ;(api.getDecisionRules as ReturnType<typeof vi.fn>).mockResolvedValue({
      rules: [mkRule({ id: "rx", kind_pattern: "stuck/*", severity: "risky" })],
      severities: [...VOCAB.severities], modes: [...VOCAB.modes],
    })
    ;(api.testDecisionRules as ReturnType<typeof vi.fn>).mockResolvedValue({
      mode: "supervised",
      hits: [
        { kind: "stuck/loop", rule_id: "rx", severity: "risky", auto: false },
        { kind: "other", rule_id: null, severity: null, auto: false },
      ],
    })
    render(<DecisionRulesEditor />)
    await screen.findByTestId("rule-row-rx")
    await user.type(
      screen.getByPlaceholderText(/stuck\/loop/),
      "stuck/loop, other",
    )
    await user.click(screen.getByRole("button", { name: /^RUN$/ }))
    await waitFor(() => screen.getByLabelText("test results"))
    // Hit + miss both rendered.
    expect(screen.getByText(/→ rx/)).toBeInTheDocument()
    expect(screen.getByText(/no match/)).toBeInTheDocument()
  })

  it("surfaces backend validation errors on save", async () => {
    const user = userEvent.setup()
    ;(api.getDecisionRules as ReturnType<typeof vi.fn>).mockResolvedValue({
      rules: [], severities: [...VOCAB.severities], modes: [...VOCAB.modes],
    })
    ;(api.putDecisionRules as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("kind_pattern is required"),
    )
    render(<DecisionRulesEditor />)
    await screen.findByText(/no rules/i)
    await user.click(screen.getByRole("button", { name: /add rule/i }))
    // SAVE without filling kind_pattern — backend rejects.
    await user.click(screen.getByRole("button", { name: /save/i }))
    await screen.findByText(/kind_pattern is required/)
  })
})

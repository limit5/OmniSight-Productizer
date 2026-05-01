/**
 * AB.9 — BatchEligibilityPanel + Badge tests.
 */

import { describe, expect, it, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import {
  BatchEligibilityBadge,
  BatchEligibilityPanel,
} from "@/components/omnisight/ab/batch-eligibility-panel"
import type {
  EligibilityRule,
  RoutingDecision,
} from "@/components/omnisight/ab/types"

function _rule(over: Partial<EligibilityRule> = {}): EligibilityRule {
  return {
    task_kind: "hd_parse_kicad",
    batch_eligible: true,
    batch_priority: "P2",
    reason: "EDA parsing — long-running, no UI dependency",
    realtime_required: false,
    auto_batch_threshold: 10,
    ...over,
  }
}

function _decision(over: Partial<RoutingDecision> = {}): RoutingDecision {
  return {
    lane: "batch",
    priority: "P2",
    rule: _rule(),
    reason: "default batch-eligible: EDA parsing",
    ...over,
  }
}

describe("BatchEligibilityBadge", () => {
  it("renders batch lane with batch styling", () => {
    render(<BatchEligibilityBadge decision={_decision()} />)
    const badge = screen.getByTestId("batch-eligibility-badge")
    expect(badge).toHaveAttribute("data-lane", "batch")
    expect(badge).toHaveTextContent("Batch")
    expect(badge).toHaveTextContent("P2")
    expect(badge.className).toContain("bg-blue-100")
  })

  it("renders realtime lane with realtime styling", () => {
    render(
      <BatchEligibilityBadge
        decision={_decision({
          lane: "realtime",
          priority: "P0",
          rule: _rule({
            task_kind: "chat_ui",
            batch_eligible: false,
            realtime_required: true,
          }),
          reason: "default realtime: chat",
        })}
      />,
    )
    const badge = screen.getByTestId("batch-eligibility-badge")
    expect(badge).toHaveAttribute("data-lane", "realtime")
    expect(badge).toHaveTextContent("Realtime")
    expect(badge).toHaveTextContent("P0")
    expect(badge.className).toContain("bg-orange-100")
  })

  it("surfaces VETOED state via data attribute and tooltip", () => {
    render(
      <BatchEligibilityBadge
        decision={_decision({
          lane: "realtime",
          priority: "P0",
          rule: _rule({
            task_kind: "chat_ui",
            realtime_required: true,
          }),
          reason: "force_lane='batch' VETOED — realtime_required",
        })}
      />,
    )
    const badge = screen.getByTestId("batch-eligibility-badge")
    expect(badge).toHaveAttribute("data-vetoed", "true")
    expect(badge.title).toContain("VETOED")
  })

  it("shows lock icon when realtime_required", () => {
    render(
      <BatchEligibilityBadge
        decision={_decision({
          lane: "realtime",
          rule: _rule({ realtime_required: true }),
        })}
      />,
    )
    expect(
      screen.getByLabelText("realtime required"),
    ).toBeInTheDocument()
  })
})

describe("BatchEligibilityPanel", () => {
  it("renders empty overrides state", () => {
    render(
      <BatchEligibilityPanel
        defaults={[_rule()]}
        overrides={[]}
      />,
    )
    expect(
      screen.getByTestId("eligibility-overrides-empty"),
    ).toBeInTheDocument()
  })

  it("lists overrides with task kind + lane + reason", () => {
    render(
      <BatchEligibilityPanel
        defaults={[]}
        overrides={[
          _rule({
            task_kind: "hd_parse_kicad",
            batch_eligible: false,
            reason: "emergency real-time only",
          }),
        ]}
      />,
    )
    const row = screen.getByTestId("eligibility-override-hd_parse_kicad")
    expect(row).toHaveTextContent("hd_parse_kicad")
    expect(row).toHaveTextContent("realtime")
    expect(row).toHaveTextContent("emergency real-time only")
  })

  it("invokes onClearOverride when trash button clicked", async () => {
    const onClearOverride = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <BatchEligibilityPanel
        defaults={[]}
        overrides={[_rule({ task_kind: "hd_parse_kicad" })]}
        onClearOverride={onClearOverride}
      />,
    )
    await user.click(screen.getByTestId("eligibility-clear-hd_parse_kicad"))
    expect(onClearOverride).toHaveBeenCalledWith("hd_parse_kicad")
  })

  it("renders defaults table with one row per rule", () => {
    render(
      <BatchEligibilityPanel
        defaults={[
          _rule({ task_kind: "hd_parse_kicad" }),
          _rule({
            task_kind: "chat_ui",
            batch_eligible: false,
            realtime_required: true,
          }),
        ]}
        overrides={[]}
      />,
    )
    expect(
      screen.getByTestId("eligibility-default-hd_parse_kicad"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("eligibility-default-chat_ui"),
    ).toBeInTheDocument()
    // realtime_required gets a lock icon
    expect(
      screen.getByLabelText("realtime_required"),
    ).toBeInTheDocument()
  })

  it("toggles override form open/closed", async () => {
    const onSetOverride = vi.fn()
    const user = userEvent.setup()
    render(
      <BatchEligibilityPanel
        defaults={[_rule()]}
        overrides={[]}
        onSetOverride={onSetOverride}
      />,
    )
    expect(
      screen.queryByTestId("eligibility-override-form-hd_parse_kicad"),
    ).not.toBeInTheDocument()
    await user.click(
      screen.getByTestId("eligibility-override-toggle-hd_parse_kicad"),
    )
    expect(
      screen.getByTestId("eligibility-override-form-hd_parse_kicad"),
    ).toBeInTheDocument()
    await user.click(
      screen.getByTestId("eligibility-override-toggle-hd_parse_kicad"),
    )
    expect(
      screen.queryByTestId("eligibility-override-form-hd_parse_kicad"),
    ).not.toBeInTheDocument()
  })

  it("override form invokes onSetOverride with new lane + reason", async () => {
    const onSetOverride = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <BatchEligibilityPanel
        defaults={[_rule()]}
        overrides={[]}
        onSetOverride={onSetOverride}
      />,
    )
    // Open the form
    await user.click(
      screen.getByTestId("eligibility-override-toggle-hd_parse_kicad"),
    )
    // Default for batch-eligible rule = flip to realtime
    // (radio already pre-selected). Type a custom reason:
    const reasonInput = screen.getByTestId(
      "eligibility-override-reason-hd_parse_kicad",
    )
    await user.clear(reasonInput)
    await user.type(reasonInput, "emergency: only realtime this week")
    await user.click(
      screen.getByTestId("eligibility-override-save-hd_parse_kicad"),
    )
    expect(onSetOverride).toHaveBeenCalled()
    const call = onSetOverride.mock.calls[0][0] as EligibilityRule
    expect(call.task_kind).toBe("hd_parse_kicad")
    expect(call.batch_eligible).toBe(false)
    expect(call.reason).toBe("emergency: only realtime this week")
  })

  it("override form for realtime_required rule disables batch radio", async () => {
    const onSetOverride = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(
      <BatchEligibilityPanel
        defaults={[
          _rule({
            task_kind: "chat_ui",
            batch_eligible: false,
            realtime_required: true,
          }),
        ]}
        overrides={[]}
        onSetOverride={onSetOverride}
      />,
    )
    await user.click(screen.getByTestId("eligibility-override-toggle-chat_ui"))
    const batchRadio = screen.getByTestId(
      "eligibility-override-batch-chat_ui",
    )
    expect(batchRadio).toBeDisabled()
    // Save should still flip to realtime (or stay) — never batch
    await user.click(
      screen.getByTestId("eligibility-override-save-chat_ui"),
    )
    const call = onSetOverride.mock.calls[0][0] as EligibilityRule
    expect(call.batch_eligible).toBe(false)
  })

  it("renders preview decisions section when provided", () => {
    render(
      <BatchEligibilityPanel
        defaults={[_rule()]}
        overrides={[]}
        previewDecisions={[
          _decision(),
          _decision({
            lane: "realtime",
            priority: "P0",
            rule: _rule({ task_kind: "chat_ui", batch_eligible: false }),
            reason: "default realtime",
          }),
        ]}
      />,
    )
    const preview = screen.getByTestId("eligibility-preview")
    expect(preview).toBeInTheDocument()
    // Two badges rendered inside preview
    const badges = preview.querySelectorAll(
      '[data-testid="batch-eligibility-badge"]',
    )
    expect(badges.length).toBe(2)
  })
})

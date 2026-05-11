/**
 * RPG.W13 — <CharacterCard> Tools tab contract tests.
 *
 * Locks the Tools section added in OP-218 on top of the W12 Skills
 * surface: per-tool Lv + invocation count + success-rate bar plus the
 * gate-blocked banner the operator sees when an agent's level is
 * below a tool's required gate.
 */

import { render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import {
  CharacterCard,
  type CharacterCardProps,
  type CharacterTool,
} from "@/components/omnisight/agents/CharacterCard"

const BASE_CARD: CharacterCardProps = {
  agentId: "agent-codex-alpha",
  displayName: "Codex Alpha",
  guild: "backend",
  level: 12,
  xp: 375,
  nextLevelXp: 500,
  specialization: "RPG.W13 tools tab",
  instanceSuffix: "alpha",
}

function renderCard(overrides: Partial<CharacterCardProps> = {}) {
  return render(<CharacterCard {...BASE_CARD} {...overrides} />)
}

describe("<CharacterCard> Tools tab (W13)", () => {
  it("omits the Tools section when no tools are provided", () => {
    renderCard()
    expect(screen.queryByTestId("character-card-tools")).toBeNull()
  })

  it("renders each tool row with level, invocation count, and success-rate bar", () => {
    const tools: CharacterTool[] = [
      {
        toolId: "Read",
        displayName: "Read",
        level: 2,
        invocationCount: 20,
        successCount: 18,
        requiredLevel: 1,
      },
    ]
    renderCard({ tools })

    const section = screen.getByTestId("character-card-tools")
    expect(section).toBeInTheDocument()
    const row = screen.getByTestId("character-card-tool")
    expect(row).toHaveAttribute("data-tool-id", "Read")
    expect(row).toHaveAttribute("data-tool-level", "2")
    expect(within(row).getByText(/Lv 2 · 20 invocations · 90% success/)).toBeInTheDocument()
    const progressbar = within(row).getByRole("progressbar")
    expect(progressbar).toHaveAttribute("aria-valuenow", "90")
    expect(screen.queryByTestId("character-card-tool-gate-blocked")).toBeNull()
  })

  it("renders the gate-blocked banner when level < requiredLevel", () => {
    const tools: CharacterTool[] = [
      {
        toolId: "mcp__filesystem__write_multiple_files",
        displayName: "Batch Write",
        level: 1,
        invocationCount: 0,
        successCount: 0,
        requiredLevel: 3,
      },
    ]
    renderCard({ tools })
    const row = screen.getByTestId("character-card-tool")
    expect(row).toHaveAttribute("data-tool-blocked", "true")
    const banner = screen.getByTestId("character-card-tool-gate-blocked")
    expect(within(banner).getByText(/requires Lv 3/)).toBeInTheDocument()
  })

  it("hides the gate banner when the agent meets the required level", () => {
    const tools: CharacterTool[] = [
      {
        toolId: "Read",
        level: 2,
        invocationCount: 12,
        successCount: 10,
        requiredLevel: 2,
      },
    ]
    renderCard({ tools })
    const row = screen.getByTestId("character-card-tool")
    expect(row).toHaveAttribute("data-tool-blocked", "false")
    expect(screen.queryByTestId("character-card-tool-gate-blocked")).toBeNull()
  })

  it("falls back to the toolId when no displayName is supplied", () => {
    const tools: CharacterTool[] = [
      {
        toolId: "mcp__bespoke",
        level: 1,
        invocationCount: 0,
        successCount: 0,
      },
    ]
    renderCard({ tools })
    expect(screen.getByText("mcp__bespoke")).toBeInTheDocument()
  })

  it("skips rows with no toolId so malformed inputs don't crash the tab", () => {
    const malformed = [
      { toolId: "", level: 1, invocationCount: 0, successCount: 0 } as CharacterTool,
      { toolId: "Read", level: 1, invocationCount: 0, successCount: 0 } as CharacterTool,
    ]
    renderCard({ tools: malformed })
    expect(screen.getAllByTestId("character-card-tool")).toHaveLength(1)
  })

  it("renders 0% success when there are no invocations", () => {
    const tools: CharacterTool[] = [
      {
        toolId: "Read",
        level: 1,
        invocationCount: 0,
        successCount: 0,
      },
    ]
    renderCard({ tools })
    const row = screen.getByTestId("character-card-tool")
    expect(within(row).getByText(/0% success/)).toBeInTheDocument()
    expect(within(row).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0")
  })
})

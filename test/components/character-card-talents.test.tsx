/**
 * RPG.W14 — <CharacterCard> Talent tree contract tests.
 *
 * Locks the Talents section added in OP-219 on top of the W8.1 card:
 *   - per-milestone option row with 3 picks
 *   - "Choice locked" indicator after lock
 *   - greyed-out alternates after lock
 *   - Lv-80 capstone row gated by `pickable` + `locked`
 *
 * The runtime data shape is what `GET /agents/{id}/talents` returns;
 * the operator UI normalises that into <CharacterCardProps.talents>.
 */

import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import {
  CharacterCard,
  type CharacterCardProps,
  type CharacterTalentMilestone,
} from "@/components/omnisight/agents/CharacterCard"

const BASE_CARD: CharacterCardProps = {
  agentId: "agent-codex-alpha",
  displayName: "Codex Alpha",
  guild: "backend",
  level: 12,
  xp: 375,
  nextLevelXp: 500,
  specialization: "RPG.W14 talent tree",
  instanceSuffix: "alpha",
}

const LV10_OPTIONS = [
  { talentId: "schema-first", displayName: "Schema-First" },
  { talentId: "performance-first", displayName: "Performance-First" },
  { talentId: "security-first", displayName: "Security-First" },
]

function renderCard(overrides: Partial<CharacterCardProps> = {}) {
  return render(<CharacterCard {...BASE_CARD} {...overrides} />)
}

describe("<CharacterCard> Talent tree (W14)", () => {
  it("omits the Talents section when no talents and no capstone are provided", () => {
    renderCard()
    expect(screen.queryByTestId("character-card-talents")).toBeNull()
  })

  it("renders one milestone row per talents entry, sorted by milestoneLevel", () => {
    const talents: CharacterTalentMilestone[] = [
      { milestoneLevel: 30, options: LV10_OPTIONS, chosenTalentId: null, choiceRequired: false },
      { milestoneLevel: 10, options: LV10_OPTIONS, chosenTalentId: null, choiceRequired: true },
    ]
    renderCard({ talents })
    const rows = screen.getAllByTestId("character-card-talent-milestone")
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveAttribute("data-milestone-level", "10")
    expect(rows[1]).toHaveAttribute("data-milestone-level", "30")
  })

  it("shows 'Choice locked' indicator and greys alternates after lock", () => {
    const talents: CharacterTalentMilestone[] = [
      {
        milestoneLevel: 10,
        options: LV10_OPTIONS,
        chosenTalentId: "schema-first",
        choiceRequired: false,
      },
    ]
    renderCard({ talents })

    expect(
      screen.getByTestId("character-card-talent-locked"),
    ).toHaveTextContent("Choice locked")

    const row = screen.getByTestId("character-card-talent-milestone")
    expect(row).toHaveAttribute("data-talent-locked", "true")
    expect(row).toHaveAttribute("data-talent-id", "schema-first")

    const options = within(row).getAllByTestId("character-card-talent-option")
    const chosen = options.find(
      (option) => option.getAttribute("data-talent-option-id") === "schema-first",
    )
    const alternates = options.filter(
      (option) => option.getAttribute("data-talent-option-id") !== "schema-first",
    )
    expect(chosen).toHaveAttribute("data-talent-option-chosen", "true")
    for (const alt of alternates) {
      expect(alt).toHaveAttribute("data-talent-option-alternate", "true")
    }
  })

  it("fires onLockTalent with (milestoneLevel, talentId) when an option is clicked", () => {
    const onLockTalent = vi.fn()
    const talents: CharacterTalentMilestone[] = [
      {
        milestoneLevel: 10,
        options: LV10_OPTIONS,
        chosenTalentId: null,
        choiceRequired: true,
      },
    ]
    renderCard({ talents, onLockTalent })

    const security = screen.getByText("Security-First")
    fireEvent.click(security)
    expect(onLockTalent).toHaveBeenCalledWith(10, "security-first")
  })

  it("disables the option buttons after a milestone is locked", () => {
    const onLockTalent = vi.fn()
    const talents: CharacterTalentMilestone[] = [
      {
        milestoneLevel: 10,
        options: LV10_OPTIONS,
        chosenTalentId: "schema-first",
        choiceRequired: false,
      },
    ]
    renderCard({ talents, onLockTalent })

    const performance = screen.getByText("Performance-First")
    expect(performance).toBeDisabled()
    fireEvent.click(performance)
    expect(onLockTalent).not.toHaveBeenCalled()
  })

  it("renders the capstone block with pickable=true and fires onLockCapstone", () => {
    const onLockCapstone = vi.fn()
    renderCard({
      level: 80,
      capstone: {
        abilityId: "code_archaeologist",
        displayName: "Code Archaeologist",
        summary: "1M context legacy-code read + surgical refactor.",
        pickable: true,
        locked: false,
      },
      onLockCapstone,
    })

    const capstone = screen.getByTestId("character-card-capstone")
    expect(capstone).toHaveAttribute("data-capstone-pickable", "true")
    expect(capstone).toHaveAttribute("data-capstone-locked", "false")

    const button = within(capstone).getByTestId("character-card-capstone-lock")
    fireEvent.click(button)
    expect(onLockCapstone).toHaveBeenCalledTimes(1)
  })

  it("shows the capstone as locked when locked=true", () => {
    renderCard({
      level: 80,
      capstone: {
        abilityId: "code_archaeologist",
        displayName: "Code Archaeologist",
        summary: "1M context legacy-code read + surgical refactor.",
        pickable: false,
        locked: true,
      },
    })

    const capstone = screen.getByTestId("character-card-capstone")
    expect(capstone).toHaveAttribute("data-capstone-locked", "true")
    expect(within(capstone).getByText("Locked")).toBeInTheDocument()
    expect(
      screen.queryByTestId("character-card-capstone-lock"),
    ).toBeNull()
  })

  it("hides the capstone lock button until the Lv-80 milestone is picked", () => {
    renderCard({
      level: 80,
      capstone: {
        abilityId: "code_archaeologist",
        displayName: "Code Archaeologist",
        summary: "1M context legacy-code read + surgical refactor.",
        pickable: false,
        locked: false,
      },
    })

    const capstone = screen.getByTestId("character-card-capstone")
    expect(capstone).toHaveAttribute("data-capstone-pickable", "false")
    expect(
      within(capstone).getByText("Requires Lv 80 + final pick"),
    ).toBeInTheDocument()
  })
})

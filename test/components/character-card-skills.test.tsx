/**
 * RPG.W12 — <CharacterCard> Skills tab contract tests.
 *
 * Locks the Skills section added in OP-217 on top of the W8.1 card:
 * per-skill Lv + XP bar, branch-locked banner, and the Lv-3
 * branch picker that fires `onLockBranch(skillId, branchId)`.
 */

import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import {
  CharacterCard,
  type CharacterCardProps,
  type CharacterSkill,
} from "@/components/omnisight/agents/CharacterCard"

const BASE_CARD: CharacterCardProps = {
  agentId: "agent-codex-alpha",
  displayName: "Codex Alpha",
  guild: "backend",
  level: 12,
  xp: 375,
  nextLevelXp: 500,
  specialization: "RPG.W12 skills tab",
  instanceSuffix: "alpha",
}

function renderCard(overrides: Partial<CharacterCardProps> = {}) {
  return render(<CharacterCard {...BASE_CARD} {...overrides} />)
}

describe("<CharacterCard> Skills tab (W12)", () => {
  it("omits the Skills section when no skills are provided", () => {
    renderCard()
    expect(screen.queryByTestId("character-card-skills")).toBeNull()
  })

  it("renders each skill row with its level, XP bar, and display name", () => {
    const skills: CharacterSkill[] = [
      {
        skillId: "enterprise_web",
        displayName: "Enterprise Web",
        level: 2,
        xp: 60,
        nextLevelXp: 100,
        branchChoice: null,
        branchChoiceRequired: false,
      },
    ]
    renderCard({ skills })

    const section = screen.getByTestId("character-card-skills")
    expect(section).toBeInTheDocument()
    const row = screen.getByTestId("character-card-skill")
    expect(row).toHaveAttribute("data-skill-id", "enterprise_web")
    expect(row).toHaveAttribute("data-skill-level", "2")
    expect(within(row).getByText("Enterprise Web")).toBeInTheDocument()
    expect(within(row).getByText("Lv 2 · 60 / 100 XP")).toBeInTheDocument()
    const progressbar = within(row).getByRole("progressbar")
    expect(progressbar).toHaveAttribute("aria-valuenow", "60")
  })

  it("renders the locked branch banner when a branch_choice is present", () => {
    const skills: CharacterSkill[] = [
      {
        skillId: "enterprise_web",
        displayName: "Enterprise Web",
        level: 3,
        xp: 120,
        nextLevelXp: 250,
        branchChoice: "perf_tuning",
        branchChoiceRequired: false,
      },
    ]
    renderCard({ skills })
    const banner = screen.getByTestId("character-card-skill-branch-locked")
    expect(within(banner).getByText("perf_tuning")).toBeInTheDocument()
    expect(screen.queryByTestId("character-card-skill-branch-picker")).toBeNull()
  })

  it("renders the branch picker buttons when branch_choice_required is true", () => {
    const onLockBranch = vi.fn()
    const skills: CharacterSkill[] = [
      {
        skillId: "enterprise_web",
        displayName: "Enterprise Web",
        level: 3,
        xp: 120,
        nextLevelXp: 250,
        branchChoice: null,
        branchChoiceRequired: true,
        branchOptions: [
          { branchId: "perf_tuning", displayName: "Performance Tuning" },
          { branchId: "type_correctness", displayName: "Type Correctness" },
        ],
      },
    ]
    renderCard({ skills, onLockBranch })

    const picker = screen.getByTestId("character-card-skill-branch-picker")
    expect(picker).toBeInTheDocument()
    const options = within(picker).getAllByTestId("character-card-skill-branch-option")
    expect(options).toHaveLength(2)
    expect(options[0]).toHaveAttribute("data-branch-id", "perf_tuning")
    expect(options[1]).toHaveAttribute("data-branch-id", "type_correctness")
  })

  it("fires onLockBranch with (skillId, branchId) when the operator clicks a branch", () => {
    const onLockBranch = vi.fn()
    const skills: CharacterSkill[] = [
      {
        skillId: "enterprise_web",
        displayName: "Enterprise Web",
        level: 3,
        xp: 120,
        nextLevelXp: 250,
        branchChoice: null,
        branchChoiceRequired: true,
        branchOptions: [
          { branchId: "perf_tuning", displayName: "Performance Tuning" },
          { branchId: "type_correctness", displayName: "Type Correctness" },
        ],
      },
    ]
    renderCard({ skills, onLockBranch })

    const performance = screen.getByText("Performance Tuning")
    fireEvent.click(performance)
    expect(onLockBranch).toHaveBeenCalledWith("enterprise_web", "perf_tuning")
  })

  it("falls back to the skillId when no displayName is supplied", () => {
    const skills: CharacterSkill[] = [
      {
        skillId: "uvc",
        level: 1,
        xp: 0,
        nextLevelXp: 25,
      },
    ]
    renderCard({ skills })
    expect(screen.getByText("uvc")).toBeInTheDocument()
  })

  it("skips rows with no skillId so malformed inputs don't crash the tab", () => {
    const malformed = [
      { skillId: "", level: 1, xp: 0, nextLevelXp: 25 } as CharacterSkill,
      { skillId: "uvc", level: 1, xp: 0, nextLevelXp: 25 } as CharacterSkill,
    ]
    renderCard({ skills: malformed })
    expect(screen.getAllByTestId("character-card-skill")).toHaveLength(1)
  })
})

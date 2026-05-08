/**
 * RPG.W8.5 - <CharacterCard> contract tests.
 *
 * Locks the compact RPG identity panel from W8.1: agent identity, Guild
 * crest, level progress, specialization, style fingerprint, buffs, and
 * badge wall. Data loading and instance switching belong to separate W8
 * follow-ups, so this file stays focused on the presentational card.
 */

import { render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import {
  CharacterCard,
  getLevelProgressPercent,
  type CharacterCardProps,
} from "@/components/omnisight/agents/CharacterCard"

const BASE_CARD: CharacterCardProps = {
  agentId: "agent-codex-alpha",
  displayName: "Codex Alpha",
  guild: "frontend",
  level: 12,
  xp: 375,
  nextLevelXp: 500,
  specialization: "React contract tests",
  instanceSuffix: "alpha",
}

function renderCard(overrides: Partial<CharacterCardProps> = {}) {
  return render(<CharacterCard {...BASE_CARD} {...overrides} />)
}

describe("getLevelProgressPercent()", () => {
  it("returns proportional progress for normal XP input", () => {
    expect(getLevelProgressPercent(375, 500)).toBe(75)
  })

  it("clamps progress outside the 0-100 range", () => {
    expect(getLevelProgressPercent(650, 500)).toBe(100)
    expect(getLevelProgressPercent(-25, 500)).toBe(0)
  })

  it("returns 0 when nextLevelXp is zero or invalid", () => {
    expect(getLevelProgressPercent(10, 0)).toBe(0)
    expect(getLevelProgressPercent(10, Number.NaN)).toBe(0)
  })
})

describe("<CharacterCard>", () => {
  it("renders the agent identity, instance suffix, guild, crest, and specialization", () => {
    const { container } = renderCard()

    const card = container.querySelector("article")
    expect(card).toHaveAttribute("data-agent-id", "agent-codex-alpha")
    expect(card).toHaveAttribute("data-agent-guild", "frontend")

    expect(screen.getByRole("heading", { name: "Codex Alpha" })).toBeInTheDocument()
    expect(screen.getByText("agent-codex-alpha")).toBeInTheDocument()
    expect(screen.getByText("alpha")).toBeInTheDocument()
    expect(screen.getByText("Frontend Guild")).toBeInTheDocument()
    expect(screen.getByText("React contract tests")).toBeInTheDocument()

    const crest = screen.getByLabelText("Frontend Guild")
    expect(crest).toHaveAttribute("title", "Frontend Guild")
    expect(within(crest).getByText("FE")).toBeInTheDocument()
  })

  it("renders the level row and progressbar with rounded percentage", () => {
    renderCard({ level: 8.8, xp: 1249.9, nextLevelXp: 2000 })

    expect(screen.getByText("Level 8")).toBeInTheDocument()
    expect(screen.getByText("1,249 / 2,000 XP")).toBeInTheDocument()

    const progress = screen.getByRole("progressbar", {
      name: "Level progress 62 percent",
    })
    expect(progress).toHaveAttribute("aria-valuenow", "62")
    expect(progress).toHaveAttribute("aria-valuemin", "0")
    expect(progress).toHaveAttribute("aria-valuemax", "100")
  })

  it("clamps displayed level and XP values before rendering", () => {
    renderCard({ level: -3, xp: -10, nextLevelXp: -20 })

    expect(screen.getByText("Level 1")).toBeInTheDocument()
    expect(screen.getByText("0 / 0 XP")).toBeInTheDocument()
    expect(
      screen.getByRole("progressbar", { name: "Level progress 0 percent" }),
    ).toHaveAttribute("aria-valuenow", "0")
  })

  it("uses initials as the portrait fallback when no portrait URL is present", () => {
    renderCard({ displayName: "Ada Lovelace", portraitUrl: null })

    expect(screen.getByText("AL")).toBeInTheDocument()
    expect(screen.queryByAltText("Ada Lovelace portrait")).toBeNull()
  })

  it("omits optional panels when no style fingerprint, buffs, or badges are provided", () => {
    renderCard()

    expect(screen.queryByText(/Style fingerprint:/)).toBeNull()
    expect(screen.queryByTestId("character-card-buffs")).toBeNull()
    expect(screen.queryByTestId("character-card-badge-wall")).toBeNull()
  })

  it("renders the style fingerprint when provided", () => {
    renderCard({ styleFingerprint: "test-heavy/refactor-light" })

    expect(screen.getByText(/Style fingerprint:/)).toBeInTheDocument()
    expect(screen.getByText("test-heavy/refactor-light")).toBeInTheDocument()
  })

  it("renders active buffs with label, title metadata, stacks, and polarity", () => {
    renderCard({
      buffs: [
        {
          id: "fresh",
          kind: "fresh_tokens",
          description: "Rolling reset bonus",
          expiresIn: "25m",
          stacks: 3,
          polarity: "buff",
        },
        {
          id: "burnout",
          kind: "burnout",
          label: "Needs recovery",
          polarity: "debuff",
        },
      ],
    })

    const buffs = screen.getAllByTestId("character-card-buff")
    expect(buffs).toHaveLength(2)

    expect(buffs[0]).toHaveAttribute("aria-label", "Fresh Tokens")
    expect(buffs[0]).toHaveAttribute("data-buff-kind", "fresh_tokens")
    expect(buffs[0]).toHaveAttribute("data-buff-polarity", "buff")
    expect(buffs[0]).toHaveAttribute(
      "title",
      "Fresh Tokens · Rolling reset bonus · Expires 25m · 3 stacks",
    )
    expect(within(buffs[0]).getByText("3")).toBeInTheDocument()

    expect(buffs[1]).toHaveAttribute("aria-label", "Needs recovery")
    expect(buffs[1]).toHaveAttribute("data-buff-kind", "burnout")
    expect(buffs[1]).toHaveAttribute("data-buff-polarity", "debuff")
  })

  it("filters unsupported buff records before rendering", () => {
    renderCard({
      buffs: [
        { kind: "streak" },
        { kind: "unsupported" } as NonNullable<CharacterCardProps["buffs"]>[number],
      ],
    })

    const buffs = screen.getAllByTestId("character-card-buff")
    expect(buffs).toHaveLength(1)
    expect(buffs[0]).toHaveAttribute("data-buff-kind", "streak")
  })

  it("renders valid badge wall entries with rarity, lock state, progress, and title metadata", () => {
    renderCard({
      badges: [
        {
          id: "prs",
          kind: "pr_merged_100",
          earnedAt: "2026-05-08",
          rarity: "gold",
        },
        {
          id: "campaign",
          kind: "campaign",
          label: "Campaign Finisher",
          description: "Completed a narrative campaign",
          progressLabel: "3 / 4 chapters",
          rarity: "legendary",
          locked: true,
        },
        { kind: "unsupported" } as NonNullable<CharacterCardProps["badges"]>[number],
      ],
    })

    const badgeWall = screen.getByTestId("character-card-badge-wall")
    expect(badgeWall).toHaveAccessibleName("Achievement badge wall")

    const badges = screen.getAllByTestId("character-card-badge")
    expect(badges).toHaveLength(2)

    expect(badges[0]).toHaveAttribute("aria-label", "100 PRs Merged")
    expect(badges[0]).toHaveAttribute("data-badge-kind", "pr_merged_100")
    expect(badges[0]).toHaveAttribute("data-badge-rarity", "gold")
    expect(badges[0]).toHaveAttribute("data-badge-locked", "false")
    expect(badges[0]).toHaveAttribute("title", "100 PRs Merged · Earned 2026-05-08")

    expect(badges[1]).toHaveAttribute("aria-label", "Campaign Finisher")
    expect(badges[1]).toHaveAttribute("data-badge-kind", "campaign")
    expect(badges[1]).toHaveAttribute("data-badge-rarity", "legendary")
    expect(badges[1]).toHaveAttribute("data-badge-locked", "true")
    expect(badges[1]).toHaveAttribute(
      "title",
      "Campaign Finisher · Completed a narrative campaign · 3 / 4 chapters",
    )
    expect(within(badges[1]).getByText("3 / 4 chapters")).toBeInTheDocument()
  })
})

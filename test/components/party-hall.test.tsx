/**
 * RPG.W17 — <PartyHall> contract tests (OP-220 AC #5 / #10).
 *
 * Locks the Party Hall surface:
 *   - empty-state when no active parties
 *   - one card per party with member portraits + count badge
 *   - active task line shows task title or fallback "Idle …"
 *   - synergy badge renders from synergy data (or "No synergy" tile)
 */

import { render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import {
  PartyHall,
  type Party,
} from "@/components/omnisight/agents/PartyHall"

const SYNERGY_FULLSTACK = {
  label: "fullstack",
  displayName: "Fullstack",
  guilds: ["backend", "frontend"] as const,
  xpBonus: 0.15,
  skillBonusTarget: null,
  skillBonus: null,
  summary: "Backend + Frontend coverage end-to-end.",
}

function makeParty(overrides: Partial<Party> = {}): Party {
  return {
    partyId: "party-001",
    name: "Atlas",
    members: [
      { agentId: "agent-be", displayName: "Backend Alpha", guild: "backend" },
      { agentId: "agent-fe", displayName: "Frontend Beta", guild: "frontend" },
    ],
    activeTaskId: "OP-220",
    activeTaskTitle: "RPG.W17 — Synergy / Party system",
    synergy: { ...SYNERGY_FULLSTACK, guilds: ["backend", "frontend"] },
    ...overrides,
  }
}

describe("<PartyHall>", () => {
  it("renders the empty state when there are no active parties", () => {
    render(<PartyHall parties={[]} />)
    expect(screen.getByTestId("party-hall-empty")).toBeInTheDocument()
    expect(
      screen.getByText(/no active parties/i),
    ).toBeInTheDocument()
  })

  it("renders one card per active party", () => {
    const parties = [makeParty(), makeParty({ partyId: "party-002", name: "Borealis" })]
    render(<PartyHall parties={parties} />)
    const cards = screen.getAllByTestId("party-hall-card")
    expect(cards).toHaveLength(2)
    expect(cards[0]).toHaveAttribute("data-party-id", "party-001")
    expect(cards[1]).toHaveAttribute("data-party-id", "party-002")
  })

  it("shows member portrait initials and a member-count badge per card", () => {
    render(<PartyHall parties={[makeParty()]} />)
    const card = screen.getByTestId("party-hall-card")
    const members = within(card).getAllByTestId("party-hall-member")
    expect(members).toHaveLength(2)
    expect(members[0]).toHaveAttribute("data-agent-id", "agent-be")
    expect(members[1]).toHaveAttribute("data-agent-id", "agent-fe")
  })

  it("shows the active task title on the card, falling back to the id", () => {
    render(<PartyHall parties={[makeParty({ activeTaskTitle: null })]} />)
    const card = screen.getByTestId("party-hall-card")
    const taskLine = within(card).getByTestId("party-hall-card-task")
    expect(taskLine).toHaveAttribute("data-active-task-id", "OP-220")
    expect(taskLine).toHaveTextContent("OP-220")
  })

  it("renders an Idle line when the party has no active task", () => {
    render(
      <PartyHall
        parties={[makeParty({ activeTaskId: null, activeTaskTitle: null })]}
      />,
    )
    const card = screen.getByTestId("party-hall-card")
    const taskLine = within(card).getByTestId("party-hall-card-task")
    expect(taskLine).toHaveAttribute("data-active-task-id", "")
    expect(taskLine).toHaveTextContent(/idle/i)
  })

  it("renders the synergy badge with the matrix label + Guild pair", () => {
    render(<PartyHall parties={[makeParty()]} />)
    const badge = screen.getByTestId("party-badge")
    expect(badge).toHaveAttribute("data-synergy-label", "fullstack")
    const guilds = within(badge).getAllByTestId("party-badge-guild")
    expect(guilds.map((node) => node.getAttribute("data-guild"))).toEqual([
      "backend",
      "frontend",
    ])
    expect(
      within(badge).getByTestId("party-badge-bonus"),
    ).toHaveTextContent(/\+15% party XP/i)
  })

  it("renders a 'No synergy' tile when the party has no matching matrix entry", () => {
    render(<PartyHall parties={[makeParty({ synergy: null })]} />)
    expect(screen.getByTestId("party-badge-no-synergy")).toHaveTextContent(
      /no synergy/i,
    )
  })
})

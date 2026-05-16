/**
 * RPG.W12.7 — <CharacterCard> branching-tree visualization tests.
 *
 * Locks the W12.7 tree surface on top of the W12.4 picker contract:
 *
 *   - Tree renders only when ≥ 2 branch options are declared (otherwise
 *     skill rows without a fork collapse cleanly).
 *   - Root node carries the parent skill_id + Lv label.
 *   - Fork node sits between root and leaves so the tree is unambiguous.
 *   - Each leaf has its own branch_id, marked chosen / alternate so the
 *     immutability of W12.4 lock-in is visible at a glance.
 *   - Clicking a leaf when unlocked fires onLockBranch(skill_id, branch_id).
 *   - Clicking a leaf when locked is a no-op (immutable per
 *     SkillBranchAlreadyLocked).
 *
 * These contracts are independent of the older flat-picker test file
 * (`character-card-skills.test.tsx`) — that file still covers the
 * `character-card-skill-branch-picker` and `character-card-skill-branch-option`
 * test IDs that W12.7 reuses for backward compatibility.
 */

import { fireEvent, render, screen, within } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import {
  CharacterCard,
  type CharacterCardProps,
  type CharacterSkill,
} from "@/components/omnisight/agents/CharacterCard"
import { SkillBranchTree } from "@/components/omnisight/agents/SkillBranchTree"

const BASE_CARD: CharacterCardProps = {
  agentId: "agent-codex-alpha",
  displayName: "Codex Alpha",
  guild: "backend",
  level: 12,
  xp: 375,
  nextLevelXp: 500,
  specialization: "RPG.W12.7 branching tree",
  instanceSuffix: "alpha",
}

const TWO_BRANCH_OPTIONS = [
  {
    branchId: "perf_tuning",
    displayName: "Performance Tuning",
    summary: "Bias toward query plans, caching, and tail-latency reduction.",
  },
  {
    branchId: "type_correctness",
    displayName: "Type Correctness",
    summary: "Bias toward schema rigour, contract tests, and type safety.",
  },
]

function renderCard(overrides: Partial<CharacterCardProps> = {}) {
  return render(<CharacterCard {...BASE_CARD} {...overrides} />)
}

describe("<SkillBranchTree> rendering invariants (W12.7)", () => {
  it("returns nothing when fewer than two branch options are provided", () => {
    const skill: CharacterSkill = {
      skillId: "enterprise_web",
      displayName: "Enterprise Web",
      level: 2,
      xp: 60,
      nextLevelXp: 100,
      branchOptions: [TWO_BRANCH_OPTIONS[0]],
    }
    const { container } = render(<SkillBranchTree skill={skill} />)
    expect(container).toBeEmptyDOMElement()
  })

  it("renders a tree with a root node, fork node, and two leaves", () => {
    const skill: CharacterSkill = {
      skillId: "enterprise_web",
      displayName: "Enterprise Web",
      level: 3,
      xp: 120,
      nextLevelXp: 250,
      branchChoice: null,
      branchChoiceRequired: true,
      branchOptions: TWO_BRANCH_OPTIONS,
    }
    render(<SkillBranchTree skill={skill} />)

    const tree = screen.getByRole("tree")
    expect(tree).toHaveAccessibleName("Enterprise Web branching tree")
    expect(tree).toHaveAttribute("data-branch-tree-skill", "enterprise_web")
    expect(tree).toHaveAttribute("data-branch-tree-locked", "false")
    expect(tree).toHaveAttribute("data-branch-tree-pickable", "true")

    const root = screen.getByTestId("character-card-skill-branch-tree-root")
    expect(root).toHaveAttribute("data-skill-id", "enterprise_web")
    expect(within(root).getByText("Enterprise Web")).toBeInTheDocument()
    expect(within(root).getByText(/Lv 3$/)).toBeInTheDocument()

    expect(
      screen.getByTestId("character-card-skill-branch-tree-fork"),
    ).toBeInTheDocument()

    const leaves = screen.getAllByTestId("character-card-skill-branch-option")
    expect(leaves).toHaveLength(2)
    expect(leaves[0]).toHaveAttribute("data-branch-id", "perf_tuning")
    expect(leaves[1]).toHaveAttribute("data-branch-id", "type_correctness")
  })

  it("falls back to skillId when no displayName is supplied on the root", () => {
    const skill: CharacterSkill = {
      skillId: "uvc",
      level: 3,
      xp: 0,
      nextLevelXp: 250,
      branchOptions: TWO_BRANCH_OPTIONS,
      branchChoiceRequired: true,
    }
    render(<SkillBranchTree skill={skill} />)
    const root = screen.getByTestId("character-card-skill-branch-tree-root")
    expect(within(root).getByText("uvc")).toBeInTheDocument()
  })
})

describe("<SkillBranchTree> leaf state encoding (W12.7)", () => {
  const baseSkill: CharacterSkill = {
    skillId: "enterprise_web",
    displayName: "Enterprise Web",
    level: 3,
    xp: 120,
    nextLevelXp: 250,
    branchOptions: TWO_BRANCH_OPTIONS,
  }

  it("marks the chosen leaf and dims the alternate when a branch is locked", () => {
    const skill: CharacterSkill = {
      ...baseSkill,
      branchChoice: "perf_tuning",
      branchChoiceRequired: false,
    }
    render(<SkillBranchTree skill={skill} />)

    const tree = screen.getByRole("tree")
    expect(tree).toHaveAttribute("data-branch-tree-locked", "true")
    expect(tree).toHaveAttribute("data-branch-tree-pickable", "false")

    const leaves = screen.getAllByTestId("character-card-skill-branch-option")
    const chosen = leaves.find(
      (leaf) => leaf.getAttribute("data-branch-id") === "perf_tuning",
    )
    const alternate = leaves.find(
      (leaf) => leaf.getAttribute("data-branch-id") === "type_correctness",
    )
    expect(chosen).toHaveAttribute("data-branch-leaf-chosen", "true")
    expect(chosen).toHaveAttribute("aria-selected", "true")
    expect(alternate).toHaveAttribute("data-branch-leaf-alternate", "true")
    expect(alternate).toHaveAttribute("aria-selected", "false")
  })

  it("renders no 'Pick a branch' prompt when the choice is already locked", () => {
    const skill: CharacterSkill = {
      ...baseSkill,
      branchChoice: "perf_tuning",
      branchChoiceRequired: false,
    }
    render(<SkillBranchTree skill={skill} />)
    expect(
      screen.queryByTestId("character-card-skill-branch-tree-prompt"),
    ).toBeNull()
  })

  it("renders the immutability prompt when a pick is required", () => {
    const skill: CharacterSkill = {
      ...baseSkill,
      branchChoice: null,
      branchChoiceRequired: true,
    }
    render(<SkillBranchTree skill={skill} />)
    const prompt = screen.getByTestId("character-card-skill-branch-tree-prompt")
    expect(prompt).toHaveTextContent(/immutable/i)
  })
})

describe("<SkillBranchTree> interaction (W12.7)", () => {
  const skill: CharacterSkill = {
    skillId: "enterprise_web",
    displayName: "Enterprise Web",
    level: 3,
    xp: 120,
    nextLevelXp: 250,
    branchChoice: null,
    branchChoiceRequired: true,
    branchOptions: TWO_BRANCH_OPTIONS,
  }

  it("fires onLockBranch with (skillId, branchId) when an unlocked leaf is clicked", () => {
    const onLockBranch = vi.fn()
    render(<SkillBranchTree skill={skill} onLockBranch={onLockBranch} />)
    fireEvent.click(screen.getByText("Type Correctness"))
    expect(onLockBranch).toHaveBeenCalledWith("enterprise_web", "type_correctness")
  })

  it("disables both leaves and ignores clicks after a branch is locked", () => {
    const onLockBranch = vi.fn()
    const lockedSkill: CharacterSkill = {
      ...skill,
      branchChoice: "perf_tuning",
      branchChoiceRequired: false,
    }
    render(<SkillBranchTree skill={lockedSkill} onLockBranch={onLockBranch} />)

    const leaves = screen.getAllByTestId("character-card-skill-branch-option")
    for (const leaf of leaves) {
      expect(leaf).toBeDisabled()
    }
    fireEvent.click(screen.getByText("Type Correctness"))
    expect(onLockBranch).not.toHaveBeenCalled()
  })

  it("disables leaves when a pick is not required (skill below Lv 3)", () => {
    const onLockBranch = vi.fn()
    const preLockSkill: CharacterSkill = {
      ...skill,
      level: 2,
      branchChoice: null,
      branchChoiceRequired: false,
    }
    render(<SkillBranchTree skill={preLockSkill} onLockBranch={onLockBranch} />)
    const leaves = screen.getAllByTestId("character-card-skill-branch-option")
    for (const leaf of leaves) {
      expect(leaf).toBeDisabled()
    }
  })
})

describe("<CharacterCard> tree integration on the Skills tab (W12.7)", () => {
  it("nests the W12.7 tree visualization inside the W8 Skills section", () => {
    const skills: CharacterSkill[] = [
      {
        skillId: "enterprise_web",
        displayName: "Enterprise Web",
        level: 3,
        xp: 120,
        nextLevelXp: 250,
        branchChoice: null,
        branchChoiceRequired: true,
        branchOptions: TWO_BRANCH_OPTIONS,
      },
    ]
    renderCard({ skills })
    const skillsSection = screen.getByTestId("character-card-skills")
    expect(
      within(skillsSection).getByTestId("character-card-skill-branch-picker"),
    ).toBeInTheDocument()
    expect(
      within(skillsSection).getByTestId("character-card-skill-branch-tree"),
    ).toBeInTheDocument()
  })

  it("omits the tree on a Lv-2 skill row (no fork yet)", () => {
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
    expect(screen.queryByTestId("character-card-skill-branch-tree")).toBeNull()
    expect(screen.queryByTestId("character-card-skill-branch-tree-root")).toBeNull()
  })

  it("forwards the operator's leaf click through to onLockBranch on CharacterCard", () => {
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
        branchOptions: TWO_BRANCH_OPTIONS,
      },
    ]
    renderCard({ skills, onLockBranch })
    fireEvent.click(screen.getByText("Performance Tuning"))
    expect(onLockBranch).toHaveBeenCalledWith("enterprise_web", "perf_tuning")
  })
})

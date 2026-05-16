"use client"

/**
 * RPG.W12.7 — Branching-tree visualization for the Character Card
 * "Skills" tab (second tab on the card, per ADR-0008).
 *
 * Renders the W12.4 fork structure as an explicit tree: the parent skill
 * sits at the root, a fork node anchors the Lv-3 split point, and the two
 * canonical branches from `skill_matrix.yaml` are leaf nodes the operator
 * locks immutably via `onLockBranch(skill_id, branch_id)`.
 *
 * The component coexists with the existing flat picker contract from
 * W12.4 (OP-217) — it reuses the `character-card-skill-branch-picker` and
 * `character-card-skill-branch-option` test IDs on the tree container and
 * leaves so prior contract tests stay green, and adds tree-specific test
 * IDs for the root and fork nodes that W12.7 locks.
 *
 * Tree state encoding for a skill with two declared branches:
 *
 *   - `branchChoice` is null and `branchChoiceRequired` is true
 *       → both leaves are clickable (pick required)
 *   - `branchChoice` is set
 *       → chosen leaf is highlighted, alternate leaf is dimmed,
 *         both leaves are disabled (immutable per SkillBranchAlreadyLocked)
 *   - `branchChoice` is null and `branchChoiceRequired` is false
 *       → leaves render but are disabled (skill has not crossed Lv 3 yet)
 *
 * The component does NOT render anything when fewer than two branch options
 * are supplied, so a level-2 skill row (no fork yet) collapses cleanly.
 */

import type { ReactElement } from "react"

import { cn } from "@/lib/utils"

import type {
  CharacterSkill,
  CharacterSkillBranchOption,
} from "./CharacterCard"

export interface SkillBranchTreeProps {
  skill: CharacterSkill
  onLockBranch?: (skillId: string, branchId: string) => void
  className?: string
}

interface BranchLeafStateProps {
  option: CharacterSkillBranchOption
  isChosen: boolean
  isAlternate: boolean
  locked: boolean
  pickable: boolean
  onLockBranch?: (skillId: string, branchId: string) => void
  skillId: string
}

function BranchLeaf({
  option,
  isChosen,
  isAlternate,
  locked,
  pickable,
  onLockBranch,
  skillId,
}: BranchLeafStateProps): ReactElement {
  const disabled = locked || !pickable || !onLockBranch
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onLockBranch?.(skillId, option.branchId)}
      role="treeitem"
      aria-level={2}
      aria-selected={isChosen}
      className={cn(
        "flex min-w-0 flex-col items-start rounded-md border bg-background px-2 py-1.5 text-left transition-colors",
        isChosen
          ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : pickable && !locked
            ? "hover:bg-muted hover:border-muted-foreground/40"
            : "",
        isAlternate && "opacity-45",
        disabled && "cursor-not-allowed",
      )}
      data-testid="character-card-skill-branch-option"
      data-branch-id={option.branchId}
      data-branch-leaf-chosen={isChosen ? "true" : "false"}
      data-branch-leaf-alternate={isAlternate ? "true" : "false"}
      title={option.summary ?? option.displayName}
    >
      <span className="truncate font-mono text-[11px] font-semibold">
        {option.displayName}
      </span>
      <span className="mt-0.5 truncate font-mono text-[9px] text-muted-foreground">
        {option.branchId}
      </span>
    </button>
  )
}

export function SkillBranchTree({
  skill,
  onLockBranch,
  className,
}: SkillBranchTreeProps): ReactElement | null {
  const branchOptions = skill.branchOptions ?? []
  if (branchOptions.length < 2) return null

  const locked = Boolean(skill.branchChoice)
  const pickable = Boolean(skill.branchChoiceRequired) && !locked
  const skillLabel = skill.displayName?.trim() || skill.skillId

  return (
    <div
      className={cn(
        "mt-2 rounded-md border border-dashed bg-muted/20 px-3 py-2",
        className,
      )}
      role="tree"
      aria-label={`${skillLabel} branching tree`}
      data-testid="character-card-skill-branch-picker"
      data-branch-tree-skill={skill.skillId}
      data-branch-tree-locked={locked ? "true" : "false"}
      data-branch-tree-pickable={pickable ? "true" : "false"}
    >
      <div
        className="flex flex-col items-stretch gap-0"
        data-testid="character-card-skill-branch-tree"
      >
        <div className="flex justify-center">
          <div
            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2 py-1 font-mono text-[10px] font-semibold"
            role="treeitem"
            aria-level={1}
            aria-selected={false}
            data-testid="character-card-skill-branch-tree-root"
            data-skill-id={skill.skillId}
          >
            <span className="truncate">{skillLabel}</span>
            <span className="text-muted-foreground">
              · Lv {Math.max(1, Math.trunc(skill.level))}
            </span>
          </div>
        </div>

        <div className="flex justify-center" aria-hidden="true">
          <div className="h-3 w-px bg-border" />
        </div>

        <div className="flex justify-center" aria-hidden="true">
          <div
            className="size-2 rounded-full border bg-background"
            data-testid="character-card-skill-branch-tree-fork"
          />
        </div>

        <div
          aria-hidden="true"
          className="relative mx-auto h-3 w-full max-w-[16rem]"
        >
          <div className="absolute left-1/2 right-1/4 top-0 h-3 -translate-x-px border-l border-t border-border" />
          <div className="absolute left-1/4 right-1/2 top-0 h-3 translate-x-px border-r border-t border-border" />
        </div>

        <div
          className="grid grid-cols-2 gap-2"
          role="group"
          aria-label="Branch leaves"
          data-testid="character-card-skill-branch-tree-leaves"
        >
          {branchOptions.map((option) => {
            const isChosen = locked && option.branchId === skill.branchChoice
            const isAlternate = locked && !isChosen
            return (
              <BranchLeaf
                key={option.branchId}
                option={option}
                isChosen={isChosen}
                isAlternate={isAlternate}
                locked={locked}
                pickable={pickable}
                onLockBranch={onLockBranch}
                skillId={skill.skillId}
              />
            )
          })}
        </div>

        {pickable ? (
          <p
            className="mt-1.5 text-center text-[10px] font-medium text-amber-700 dark:text-amber-300"
            data-testid="character-card-skill-branch-tree-prompt"
          >
            Pick a branch — choice is immutable
          </p>
        ) : null}
      </div>
    </div>
  )
}

export default SkillBranchTree

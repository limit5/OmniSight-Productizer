"use client"

/**
 * RPG.W14.5 — frontend talent fork modal that blocks task assignment
 * until the operator resolves every pending Lv 10/30/50/80 milestone.
 *
 * Pure presentation: callers resolve the agent identity, the list of
 * pending milestones, and the lock/cancel wiring. The modal itself
 * owns only the operator-review surface — the "Assign anyway" button
 * stays disabled until every pending milestone has a chosen talent.
 *
 * Data shape mirrors <CharacterCard> talents prop so callers can pass
 * the same `CharacterTalentMilestone[]` they already render in the
 * Skills tab without reshaping.
 */

import { AlertTriangle, Crown, Lock } from "lucide-react"
import type { ReactElement } from "react"
import { useCallback } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

import type { CharacterTalentMilestone } from "./CharacterCard"

export interface TalentForkModalProps {
  /** Controls modal visibility — mount/unmount is owned by the caller. */
  open: boolean
  /** Agent identifier — shown in the header and emitted as a data attr. */
  agentId: string
  /** Operator-facing display name for the agent being assigned. */
  agentDisplayName: string
  /**
   * Task identifier the operator was attempting to assign when the
   * modal interrupted them. Surfaced as a data attribute so e2e /
   * unit tests can assert the modal blocked the correct task.
   */
  pendingTaskId?: string | null
  /**
   * Milestones the agent has reached but not yet locked. The modal
   * filters out milestones that already have a `chosenTalentId` so
   * a caller may pass the full talent list without pre-filtering.
   */
  milestones: readonly CharacterTalentMilestone[]
  /** Called when the operator picks a talent button inside the modal. */
  onLockTalent: (milestoneLevel: number, talentId: string) => void
  /**
   * Called when the operator confirms assignment after locking every
   * pending milestone. The button stays disabled — and this callback
   * is never fired — while any pending milestone remains unresolved.
   */
  onConfirm: () => void
  /**
   * Called when the operator cancels out (Esc, overlay click, or the
   * explicit Cancel button). The task assignment should be aborted.
   */
  onCancel: () => void
  className?: string
}

function pendingMilestonesOf(
  milestones: readonly CharacterTalentMilestone[],
): CharacterTalentMilestone[] {
  return milestones
    .filter((milestone) => {
      if (!milestone || !Number.isFinite(milestone.milestoneLevel)) return false
      if (milestone.chosenTalentId) return false
      return Boolean(milestone.choiceRequired)
    })
    .slice()
    .sort((a, b) => a.milestoneLevel - b.milestoneLevel)
}

export function TalentForkModal({
  open,
  agentId,
  agentDisplayName,
  pendingTaskId,
  milestones,
  onLockTalent,
  onConfirm,
  onCancel,
  className,
}: TalentForkModalProps): ReactElement | null {
  const pending = pendingMilestonesOf(milestones)
  const allResolved = pending.length === 0

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) onCancel()
    },
    [onCancel],
  )

  const handleConfirm = useCallback(() => {
    if (!allResolved) return
    onConfirm()
  }, [allResolved, onConfirm])

  if (!open) return null

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className={cn(
          "max-h-[calc(100vh-2rem)] max-w-2xl overflow-y-auto",
          className,
        )}
        data-testid="talent-fork-modal"
        data-agent-id={agentId}
        data-pending-task-id={pendingTaskId ?? ""}
        data-pending-count={pending.length}
        data-all-resolved={allResolved ? "true" : "false"}
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="talent-fork-modal-title"
          >
            <Crown className="size-4 text-fuchsia-500" aria-hidden="true" />
            Talent fork required
          </DialogTitle>
          <DialogDescription
            className="font-mono text-[11px] text-muted-foreground"
            data-testid="talent-fork-modal-description"
          >
            <span className="font-mono text-foreground">{agentDisplayName}</span>{" "}
            has reached a milestone with an unresolved talent fork. Lock a
            talent for each pending milestone before assigning a task — the
            choice is permanent per ADR-0008.
          </DialogDescription>
        </DialogHeader>

        <section
          aria-label="Pending talent forks"
          className="flex min-w-0 flex-col gap-2"
          data-testid="talent-fork-modal-milestones"
        >
          {pending.length === 0 ? (
            <div
              className="flex items-center gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 font-mono text-[11px] text-emerald-700 dark:text-emerald-300"
              data-testid="talent-fork-modal-empty"
            >
              <Lock className="size-3.5" aria-hidden="true" />
              All milestones locked — ready to assign.
            </div>
          ) : (
            pending.map((milestone) => (
              <article
                key={milestone.milestoneLevel}
                className="rounded-md border bg-background/40 p-3"
                data-milestone-level={milestone.milestoneLevel}
                data-testid="talent-fork-modal-milestone"
              >
                <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <span className="inline-flex items-center gap-1.5 font-mono text-xs font-semibold">
                    <AlertTriangle
                      className="size-3.5 text-amber-500"
                      aria-hidden="true"
                    />
                    Lv {milestone.milestoneLevel} Talent
                  </span>
                  <Badge
                    variant="outline"
                    className="h-5 border-amber-500/40 bg-amber-500/10 px-1.5 font-mono text-[10px] text-amber-700 dark:text-amber-300"
                  >
                    Pick required
                  </Badge>
                </header>

                <div
                  className="flex flex-col gap-1.5"
                  data-testid="talent-fork-modal-options"
                >
                  {milestone.options.map((option) => (
                    <button
                      key={option.talentId}
                      type="button"
                      onClick={() =>
                        onLockTalent(milestone.milestoneLevel, option.talentId)
                      }
                      className="flex flex-col gap-0.5 rounded border bg-background px-2 py-1.5 text-left hover:bg-muted"
                      data-testid="talent-fork-modal-option"
                      data-talent-option-id={option.talentId}
                      title={option.summary ?? option.displayName}
                    >
                      <span className="font-mono text-xs font-semibold">
                        {option.displayName}
                      </span>
                      {option.summary ? (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {option.summary}
                        </span>
                      ) : null}
                      {option.routingLabel ? (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          Routing:{" "}
                          <span className="font-mono text-foreground">
                            {option.routingLabel}
                          </span>
                        </span>
                      ) : null}
                    </button>
                  ))}
                </div>
              </article>
            ))
          )}
        </section>

        <DialogFooter className="gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="inline-flex items-center justify-center rounded border border-border bg-card px-3 py-1.5 font-mono text-xs text-muted-foreground hover:text-foreground"
            data-testid="talent-fork-modal-cancel"
          >
            Cancel assignment
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!allResolved}
            className="inline-flex items-center justify-center gap-1 rounded border border-emerald-500/55 bg-emerald-500/10 px-3 py-1.5 font-mono text-xs text-emerald-700 hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-50 dark:text-emerald-300"
            data-testid="talent-fork-modal-confirm"
          >
            <Lock className="size-3" aria-hidden="true" />
            Lock talents &amp; assign
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default TalentForkModal

"use client"

/**
 * RPG.W14.5 — frontend talent fork modal that blocks task assignment
 * until the operator resolves every pending Lv 10/30/50/80 milestone.
 *
 * OP-2516 — browser-facing pending talent picker for the Character Card.
 *
 * The card owns "which milestone is pending"; this modal owns the API
 * round trip for that one fork: GET the three options, POST the immutable
 * lock, then ask the caller to refresh canonical card state.
 */

import { AlertTriangle, Crown, Loader2, Lock } from "lucide-react"
import type { ReactElement } from "react"
import { useCallback, useEffect, useState } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import {
  ApiError,
  getTalentOptions,
  lockTalent,
  type AgentTalentOption,
} from "@/lib/api"
import { cn } from "@/lib/utils"

export interface TalentForkModalProps {
  /** Controls modal visibility — mount/unmount is owned by the caller. */
  open: boolean
  /** Agent identifier — shown in the header and emitted as a data attr. */
  agentId: string
  /** Operator-facing display name for the agent being assigned. */
  agentDisplayName: string
  /** Guild and level are required by the backend lock endpoint. */
  guild: string
  agentLevel: number
  /** Pending milestone level selected from the Character Card. */
  milestoneLevel: number
  /** Called after POST /talents/lock succeeds so the card can refetch. */
  onLocked: () => void | Promise<void>
  /**
   * Called when the operator cancels out (Esc, overlay click, or the
   * explicit Cancel button).
   */
  onCancel: () => void
  className?: string
}

export function TalentForkModal({
  open,
  agentId,
  agentDisplayName,
  guild,
  agentLevel,
  milestoneLevel,
  onLocked,
  onCancel,
  className,
}: TalentForkModalProps): ReactElement | null {
  const [options, setOptions] = useState<AgentTalentOption[]>([])
  const [loading, setLoading] = useState(false)
  const [lockingTalentId, setLockingTalentId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) onCancel()
    },
    [onCancel],
  )

  useEffect(() => {
    if (!open) return
    let cancelled = false
    Promise.resolve()
      .then(() => {
        if (cancelled) return null
        setLoading(true)
        setError(null)
        setOptions([])
        return getTalentOptions(agentId, milestoneLevel, guild)
      })
      .then((res) => {
        if (!cancelled && res) setOptions(res.options)
      })
      .catch((exc) => {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : String(exc))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [agentId, guild, milestoneLevel, open])

  const handleLock = useCallback(
    async (talentId: string) => {
      setLockingTalentId(talentId)
      setError(null)
      try {
        await lockTalent(agentId, milestoneLevel, talentId, {
          guild,
          agentLevel: Math.max(1, Math.trunc(agentLevel)),
        })
        await onLocked()
      } catch (exc) {
        if (exc instanceof ApiError && exc.status === 409) {
          setError("This milestone was already locked. Refreshing the card will show the current talent.")
        } else {
          setError(exc instanceof Error ? exc.message : String(exc))
        }
      } finally {
        setLockingTalentId(null)
      }
    },
    [agentId, agentLevel, guild, milestoneLevel, onLocked],
  )

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
        data-agent-guild={guild}
        data-milestone-level={milestoneLevel}
        data-options-count={options.length}
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
            has reached Lv {milestoneLevel} with an unresolved talent fork.
            Pick one {guild} guild talent. The choice is permanent per
            ADR-0008.
          </DialogDescription>
        </DialogHeader>

        <section
          aria-label="Pending talent forks"
          className="flex min-w-0 flex-col gap-2"
          data-testid="talent-fork-modal-milestones"
        >
          {loading ? (
            <div
              className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2 font-mono text-[11px] text-muted-foreground"
              data-testid="talent-fork-modal-loading"
            >
              <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
              Loading talent options...
            </div>
          ) : options.length === 0 ? (
            <div
              className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 font-mono text-[11px] text-amber-700 dark:text-amber-300"
              data-testid="talent-fork-modal-empty"
            >
              <AlertTriangle className="size-3.5" aria-hidden="true" />
              No talent options returned for Lv {milestoneLevel}.
            </div>
          ) : (
              <article
                className="rounded-md border bg-background/40 p-3"
                data-milestone-level={milestoneLevel}
                data-testid="talent-fork-modal-milestone"
              >
                <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <span className="inline-flex items-center gap-1.5 font-mono text-xs font-semibold">
                    <AlertTriangle
                      className="size-3.5 text-amber-500"
                      aria-hidden="true"
                    />
                    Lv {milestoneLevel} Talent
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
                  {options.map((option) => {
                    const locking = lockingTalentId === option.talent_id
                    return (
                    <button
                      key={option.talent_id}
                      type="button"
                      onClick={() => void handleLock(option.talent_id)}
                      disabled={lockingTalentId !== null}
                      className="flex flex-col gap-0.5 rounded border bg-background px-2 py-1.5 text-left hover:bg-muted"
                      data-testid="talent-fork-modal-option"
                      data-talent-option-id={option.talent_id}
                      title={option.summary ?? option.display_name}
                    >
                      <span className="inline-flex items-center gap-1.5 font-mono text-xs font-semibold">
                        {locking ? (
                          <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                        ) : null}
                        {option.display_name}
                      </span>
                      {option.summary ? (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {option.summary}
                        </span>
                      ) : null}
                      {option.routing_label ? (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          Routing:{" "}
                          <span className="font-mono text-foreground">
                            {option.routing_label}
                          </span>
                        </span>
                      ) : null}
                    </button>
                    )
                  })}
                </div>
              </article>
          )}
          {error ? (
            <p
              className="rounded-md border border-rose-500/35 bg-rose-500/10 px-3 py-2 font-mono text-[11px] text-rose-700 dark:text-rose-300"
              data-testid="talent-fork-modal-error"
            >
              {error}
            </p>
          ) : null}
        </section>

        <DialogFooter className="gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="inline-flex items-center justify-center rounded border border-border bg-card px-3 py-1.5 font-mono text-xs text-muted-foreground hover:text-foreground"
            data-testid="talent-fork-modal-cancel"
          >
            Cancel
          </button>
          <span className="inline-flex items-center justify-center gap-1 rounded border border-emerald-500/35 bg-emerald-500/10 px-3 py-1.5 font-mono text-xs text-emerald-700 dark:text-emerald-300">
            <Lock className="size-3" aria-hidden="true" />
            Immutable pick
          </span>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default TalentForkModal

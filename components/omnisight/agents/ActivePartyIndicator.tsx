"use client"

/**
 * RPG.W17.6 — Active-party indicator pill.
 *
 * Inline operator signal showing "this agent currently belongs to an
 * active party (and is either holding a Tier L+ task or sitting idle
 * in formation)". Renders next to the agent display name in lists,
 * Character Card headers, or roster pickers so the operator can spot
 * the membership at a glance — and so the party builder modal can
 * cite the active party when locking an agent out (see
 * `<PartyBuilderModal>` AC for the locked-out gate).
 *
 * Pure presentation: the caller resolves which party an agent belongs
 * to (e.g. by indexing `GET /api/v1/agents/parties` member lists
 * client-side) and passes it in. The indicator returns `null` when
 * `party` is `null` so it adds no layout weight for solo agents.
 *
 * Visual contract mirrors `<PartyBadge>` so the Party Hall and
 * roster surfaces feel like the same family:
 *   - violet accent tile / icon
 *   - "Party: <name>" headline + optional task indicator dot
 *   - optional synergy label tail
 *   - amber accent + AlertTriangle when the indicator is marked
 *     `lockedTask` (the agent's party is currently holding a Tier L+
 *     task and so cannot be re-assigned individually)
 */

import { AlertTriangle, ShieldCheck, Sparkles } from "lucide-react"
import type { ReactElement } from "react"

import { cn } from "@/lib/utils"

export interface ActivePartyIndicatorParty {
  partyId: string
  partyName: string
  /** Cosmetic synergy label (e.g. "fullstack") — omitted when null. */
  synergyLabel?: string | null
  /** Active Tier L+ task id, when the party currently holds one. */
  activeTaskId?: string | null
  /** Operator-facing label for the active task; falls back to the id. */
  activeTaskTitle?: string | null
}

export interface ActivePartyIndicatorProps {
  agentId: string
  party: ActivePartyIndicatorParty | null
  /**
   * When true, render the pill in "locked" (amber) styling — used by
   * surfaces that want to flag "this agent is currently glued to a
   * Tier L+ party task and cannot be solo-assigned". Defaults to true
   * when `party.activeTaskId` is present, false otherwise.
   */
  lockedTask?: boolean
  /** Compact rendering — drops the task/synergy tail line. */
  compact?: boolean
  className?: string
}

export function ActivePartyIndicator({
  agentId,
  party,
  lockedTask,
  compact,
  className,
}: ActivePartyIndicatorProps): ReactElement | null {
  if (!party) return null

  const taskActive = Boolean(party.activeTaskId)
  const isLocked = lockedTask ?? taskActive
  const Icon = isLocked ? AlertTriangle : ShieldCheck
  const taskLabel = taskActive
    ? party.activeTaskTitle?.trim() || party.activeTaskId
    : null
  const synergyLabel = party.synergyLabel?.trim() || null

  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 align-middle font-mono text-[11px]",
        isLocked
          ? "border-amber-500/45 bg-amber-500/10 text-amber-700 dark:text-amber-300"
          : "border-violet-500/45 bg-violet-500/10 text-violet-700 dark:text-violet-300",
        className,
      )}
      data-testid="active-party-indicator"
      data-agent-id={agentId}
      data-party-id={party.partyId}
      data-locked-task={isLocked ? "true" : "false"}
      data-active-task-id={party.activeTaskId ?? ""}
      data-synergy-label={synergyLabel ?? ""}
      title={
        taskLabel
          ? `Party ${party.partyName} · active task ${taskLabel}`
          : `Party ${party.partyName}`
      }
    >
      <Icon className="size-3 shrink-0" aria-hidden="true" />
      <span className="truncate">
        <span className="text-muted-foreground">Party:</span>{" "}
        <span className="font-semibold">{party.partyName}</span>
      </span>
      {!compact && synergyLabel ? (
        <span
          className="inline-flex items-center gap-0.5 border-l border-current/30 pl-1.5 text-[10px] uppercase opacity-80"
          data-testid="active-party-indicator-synergy"
        >
          <Sparkles className="size-2.5" aria-hidden="true" />
          {synergyLabel}
        </span>
      ) : null}
      {!compact && taskLabel ? (
        <span
          className="inline-flex max-w-[12rem] items-center gap-1 border-l border-current/30 pl-1.5"
          data-testid="active-party-indicator-task"
        >
          <span className="inline-block size-1.5 rounded-full bg-current" />
          <span className="truncate text-[10px] uppercase">{taskLabel}</span>
        </span>
      ) : null}
    </span>
  )
}

export default ActivePartyIndicator

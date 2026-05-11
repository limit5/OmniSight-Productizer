"use client"

/**
 * RPG.W17 — Party Hall grid.
 *
 * Operator-facing surface for the Party / Synergy system. Renders one
 * card per active party with:
 *   - party name + member portraits/initials
 *   - currently-assigned Tier L+ task (or "Idle")
 *   - the synergy badge from `backend/agents/synergy_registry.py`
 *
 * Data shape mirrors `GET /api/v1/agents/parties` (see
 * `backend/routers/agents.py::list_parties_endpoint`). The component is
 * presentational — fetching, loading, and error states are owned by
 * the caller page.
 */

import { ClipboardList, Sparkles, Users } from "lucide-react"
import type { ReactElement } from "react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { AgentGuild } from "@/components/omnisight/agents/CharacterCard"
import {
  PartyBadge,
  type PartySynergy,
} from "@/components/omnisight/agents/PartyBadge"

export interface PartyMember {
  agentId: string
  displayName?: string | null
  guild?: AgentGuild | null
  portraitUrl?: string | null
}

export interface Party {
  partyId: string
  name: string
  members: PartyMember[]
  activeTaskId: string | null
  activeTaskTitle?: string | null
  synergy: PartySynergy | null
}

export interface PartyHallProps {
  parties: Party[]
  className?: string
}

function memberInitials(member: PartyMember): string {
  const source = member.displayName?.trim() || member.agentId
  const parts = source.split(/[\s\-_]+/).filter(Boolean)
  if (parts.length === 0) return "?"
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

function formatMemberCount(count: number): string {
  if (!Number.isFinite(count)) return "0"
  return Math.max(0, Math.trunc(count)).toString()
}

function PartyCard({ party }: { party: Party }): ReactElement {
  const memberCount = party.members.length
  const taskLabel = party.activeTaskId
    ? party.activeTaskTitle?.trim() || party.activeTaskId
    : "Idle — no active task"

  return (
    <article
      className="min-w-0 rounded-lg border bg-gradient-to-br from-violet-500/5 via-background to-background p-4 shadow-sm"
      data-testid="party-hall-card"
      data-party-id={party.partyId}
    >
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3
            className="truncate text-sm font-semibold leading-tight"
            data-testid="party-hall-card-name"
          >
            {party.name}
          </h3>
          <p className="mt-1 truncate text-[11px] uppercase leading-none text-muted-foreground">
            {party.partyId}
          </p>
        </div>
        <Badge variant="outline" className="h-7 shrink-0 gap-1.5 px-2 text-xs">
          <Users className="size-3.5" aria-hidden="true" />
          {formatMemberCount(memberCount)}
        </Badge>
      </header>

      <div
        className="mt-3 flex -space-x-2"
        data-testid="party-hall-card-members"
        aria-label={`${memberCount} party members`}
      >
        {party.members.map((member) => (
          <div
            key={member.agentId}
            data-testid="party-hall-member"
            data-agent-id={member.agentId}
            data-guild={member.guild ?? ""}
            className={cn(
              "flex size-9 items-center justify-center rounded-full border-2 border-background",
              "bg-muted text-xs font-medium uppercase",
            )}
            title={member.displayName || member.agentId}
          >
            {member.portraitUrl ? (
              // Using a plain <img> keeps the component framework-agnostic
              // for the unit-test renderer in `test/components/party-hall.test.tsx`.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={member.portraitUrl}
                alt=""
                className="size-full rounded-full object-cover"
              />
            ) : (
              <span>{memberInitials(member)}</span>
            )}
          </div>
        ))}
      </div>

      <div
        className="mt-3 flex items-center gap-2 rounded-md border bg-background/80 px-2.5 py-1.5"
        data-testid="party-hall-card-task"
        data-active-task-id={party.activeTaskId ?? ""}
      >
        <ClipboardList
          className="size-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <span className="truncate text-xs font-medium">{taskLabel}</span>
      </div>

      <div className="mt-3" data-testid="party-hall-card-synergy">
        <PartyBadge synergy={party.synergy} />
      </div>
    </article>
  )
}

export function PartyHall({ parties, className }: PartyHallProps): ReactElement {
  const totalMembers = parties.reduce(
    (total, party) => total + party.members.length,
    0,
  )

  if (parties.length === 0) {
    return (
      <section
        aria-label="Party Hall"
        className={cn("space-y-4", className)}
        data-testid="party-hall-empty"
      >
        <div>
          <h2 className="text-lg font-semibold leading-tight">Party Hall</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Active multi-agent parties for Tier L+ tasks
          </p>
        </div>
        <div className="rounded-md border border-dashed bg-muted/20 p-6 text-center">
          <Sparkles className="mx-auto size-6 text-muted-foreground" aria-hidden="true" />
          <p className="mt-2 text-sm font-medium">No active parties</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Form a 2-5 member party to take on a Tier L+ task.
          </p>
        </div>
      </section>
    )
  }

  return (
    <section aria-label="Party Hall" className={cn("space-y-4", className)}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold leading-tight">Party Hall</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {parties.length} active {parties.length === 1 ? "party" : "parties"} ·{" "}
            {totalMembers} member{totalMembers === 1 ? "" : "s"}
          </p>
        </div>
        <Badge variant="outline" className="h-7 gap-1.5 px-2 text-xs">
          <Users className="size-3.5" aria-hidden="true" />
          {formatMemberCount(totalMembers)} total
        </Badge>
      </div>

      <div
        className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"
        data-testid="party-hall-grid"
      >
        {parties.map((party) => (
          <PartyCard key={party.partyId} party={party} />
        ))}
      </div>
    </section>
  )
}

export default PartyHall

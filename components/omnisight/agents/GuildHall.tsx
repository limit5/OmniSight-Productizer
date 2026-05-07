"use client"

/**
 * RPG.W9.1 - Guild Hall grid.
 *
 * Scope: presentational grid of the canonical RPG Guilds with member counts.
 * Data loading, filters, and drill-down routing are separate follow-ups per
 * the RPG.W9 wave.
 */

import {
  BrainCircuit,
  Code2,
  Database,
  Laptop,
  ServerCog,
  Shield,
  Smartphone,
  Users,
  Wrench,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import type { ReactElement } from "react"

import type { AgentGuild } from "@/components/omnisight/agents/CharacterCard"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export interface GuildHallGuild {
  guild: AgentGuild
  memberCount: number
}

export interface GuildHallProps {
  guilds: GuildHallGuild[]
  className?: string
}

interface GuildHallVisual {
  label: string
  description: string
  Icon: LucideIcon
  toneClass: string
  panelClass: string
}

const GUILD_ORDER: AgentGuild[] = [
  "backend",
  "frontend",
  "security",
  "devops",
  "data",
  "mobile",
  "embedded",
  "generalist",
]

const GUILD_VISUALS: Record<AgentGuild, GuildHallVisual> = {
  backend: {
    label: "Backend Guild",
    description: "Services, APIs, and persistence specialists",
    Icon: ServerCog,
    toneClass: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
    panelClass: "from-sky-500/10 via-background to-background",
  },
  frontend: {
    label: "Frontend Guild",
    description: "Interface, interaction, and client experience specialists",
    Icon: Code2,
    toneClass:
      "border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300",
    panelClass: "from-fuchsia-500/10 via-background to-background",
  },
  security: {
    label: "Security Guild",
    description: "Auth, audit, and boundary protection specialists",
    Icon: Shield,
    toneClass:
      "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300",
    panelClass: "from-rose-500/10 via-background to-background",
  },
  devops: {
    label: "DevOps Guild",
    description: "CI, release, and infrastructure operations specialists",
    Icon: Wrench,
    toneClass:
      "border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-300",
    panelClass: "from-orange-500/10 via-background to-background",
  },
  data: {
    label: "Data Guild",
    description: "Models, analytics, and data quality specialists",
    Icon: Database,
    toneClass:
      "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    panelClass: "from-emerald-500/10 via-background to-background",
  },
  mobile: {
    label: "Mobile Guild",
    description: "Device, store, and mobile UX specialists",
    Icon: Smartphone,
    toneClass:
      "border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
    panelClass: "from-cyan-500/10 via-background to-background",
  },
  embedded: {
    label: "Embedded Guild",
    description: "Firmware, SoC, and hardware-adjacent specialists",
    Icon: Laptop,
    toneClass:
      "border-lime-500/30 bg-lime-500/10 text-lime-700 dark:text-lime-300",
    panelClass: "from-lime-500/10 via-background to-background",
  },
  generalist: {
    label: "Generalist Guild",
    description: "Cross-domain agents for broad product work",
    Icon: BrainCircuit,
    toneClass:
      "border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300",
    panelClass: "from-violet-500/10 via-background to-background",
  },
}

function formatMemberCount(count: number): string {
  if (!Number.isFinite(count)) return "0"
  return Math.max(0, Math.trunc(count)).toLocaleString()
}

function memberLabel(count: number): string {
  const normalized = Number.isFinite(count) ? Math.max(0, Math.trunc(count)) : 0
  return normalized === 1 ? "member" : "members"
}

export function GuildHall({ guilds, className }: GuildHallProps): ReactElement {
  const countsByGuild = new Map(
    guilds.map((item) => [
      item.guild,
      Number.isFinite(item.memberCount) ? Math.max(0, Math.trunc(item.memberCount)) : 0,
    ]),
  )

  return (
    <section aria-label="Guild Hall" className={cn("space-y-4", className)}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold leading-tight">Guild Hall</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Agent membership across the RPG guild roster
          </p>
        </div>
        <Badge variant="outline" className="h-7 gap-1.5 px-2 text-xs">
          <Users className="size-3.5" aria-hidden="true" />
          {formatMemberCount(
            GUILD_ORDER.reduce((total, guild) => total + (countsByGuild.get(guild) ?? 0), 0),
          )}{" "}
          total
        </Badge>
      </div>

      <div
        className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
        data-testid="guild-hall-grid"
      >
        {GUILD_ORDER.map((guild) => {
          const visual = GUILD_VISUALS[guild]
          const GuildIcon = visual.Icon
          const memberCount = countsByGuild.get(guild) ?? 0

          return (
            <article
              key={guild}
              className={cn(
                "min-w-0 rounded-lg border bg-gradient-to-br p-4 shadow-sm",
                visual.panelClass,
              )}
              data-agent-guild={guild}
            >
              <div className="flex items-start justify-between gap-3">
                <div
                  aria-label={visual.label}
                  className={cn(
                    "flex size-10 shrink-0 items-center justify-center rounded-md border",
                    visual.toneClass,
                  )}
                  title={visual.label}
                >
                  <GuildIcon className="size-5" aria-hidden="true" />
                </div>
                <div className="rounded-md border bg-background/70 px-2.5 py-1.5 text-right">
                  <div className="font-mono text-base font-semibold leading-none">
                    {formatMemberCount(memberCount)}
                  </div>
                  <div className="mt-1 text-[10px] uppercase leading-none text-muted-foreground">
                    {memberLabel(memberCount)}
                  </div>
                </div>
              </div>

              <div className="mt-4 min-w-0">
                <h3 className="truncate text-sm font-semibold leading-tight">
                  {visual.label}
                </h3>
                <p className="mt-1 line-clamp-2 text-xs leading-5 text-muted-foreground">
                  {visual.description}
                </p>
              </div>
            </article>
          )
        })}
      </div>
    </section>
  )
}

export default GuildHall

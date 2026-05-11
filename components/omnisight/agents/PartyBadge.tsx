"use client"

/**
 * RPG.W17 — Synergy badge for the Party Hall.
 *
 * One badge per active synergy entry: shows the label, the two Guild
 * slugs in the pair, and the headline bonus (XP and/or skill).
 *
 * The badge intentionally degrades to a neutral "no synergy" tile when
 * `synergy` is null — the Party Hall renders this state for parties
 * whose Guild composition doesn't cover any matrix entry (per
 * `SynergyComputeFailed` degradation in `backend/agents/synergy_registry.py`
 * AC #3).
 */

import { Sparkles } from "lucide-react"
import type { ReactElement } from "react"

import type { AgentGuild } from "@/components/omnisight/agents/CharacterCard"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export interface PartySynergy {
  label: string
  displayName: string
  guilds: AgentGuild[]
  xpBonus: number
  skillBonusTarget: string | null
  skillBonus: number | null
  summary: string
}

export interface PartyBadgeProps {
  synergy: PartySynergy | null
  className?: string
}

function formatPct(bonus: number): string {
  const pct = Math.round(bonus * 100)
  if (!Number.isFinite(pct) || pct <= 0) return ""
  return `+${pct}%`
}

function bonusLine(synergy: PartySynergy): string {
  const parts: string[] = []
  const xpPct = formatPct(synergy.xpBonus)
  if (xpPct) parts.push(`${xpPct} party XP`)
  if (synergy.skillBonusTarget && synergy.skillBonus !== null) {
    const skillPct = formatPct(synergy.skillBonus)
    if (skillPct) {
      parts.push(`${skillPct} ${synergy.skillBonusTarget} skill XP`)
    }
  }
  return parts.length > 0 ? parts.join(" · ") : "no XP bonus"
}

export function PartyBadge({ synergy, className }: PartyBadgeProps): ReactElement {
  if (synergy === null) {
    return (
      <Badge
        variant="outline"
        className={cn("gap-1.5 text-xs text-muted-foreground", className)}
        data-testid="party-badge-no-synergy"
      >
        <Sparkles className="size-3" aria-hidden="true" />
        No synergy
      </Badge>
    )
  }

  return (
    <div
      className={cn(
        "rounded-md border bg-gradient-to-br from-violet-500/10 via-background to-background p-3",
        className,
      )}
      data-testid="party-badge"
      data-synergy-label={synergy.label}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Sparkles
            className="size-4 text-violet-600 dark:text-violet-400"
            aria-hidden="true"
          />
          <span className="text-sm font-semibold leading-none">
            {synergy.displayName}
          </span>
        </div>
        <div className="flex gap-1">
          {synergy.guilds.map((guild) => (
            <Badge
              key={guild}
              variant="secondary"
              className="text-[10px] uppercase"
              data-testid="party-badge-guild"
              data-guild={guild}
            >
              {guild}
            </Badge>
          ))}
        </div>
      </div>
      <p
        className="mt-2 font-mono text-xs text-violet-700 dark:text-violet-300"
        data-testid="party-badge-bonus"
      >
        {bonusLine(synergy)}
      </p>
      <p
        className="mt-1 line-clamp-2 text-[11px] leading-snug text-muted-foreground"
        data-testid="party-badge-summary"
      >
        {synergy.summary}
      </p>
    </div>
  )
}

export default PartyBadge

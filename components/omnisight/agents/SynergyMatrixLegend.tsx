"use client"

/**
 * RPG.W17.3 — Synergy matrix legend.
 *
 * Operator-facing reference table for the cross-Guild synergy matrix
 * declared in `config/synergy_matrix.yaml`. Renders one row per entry
 * with the Guild pair, label, headline XP/skill bonus, and one-line
 * summary. The caller fetches `GET /api/v1/agents/parties/synergies`
 * (see `backend/routers/agents.py::list_synergies_endpoint`) and feeds
 * the result in as `entries`; this component is purely presentational
 * so the Party Hall page can compose it next to <PartyHall>.
 *
 * Entry shape matches `PartySynergy` from PartyBadge.tsx, so the
 * legend and the per-party badge share a single type — a future YAML
 * column maps once and both surfaces pick it up.
 */

import { Sparkles } from "lucide-react"
import type { ReactElement } from "react"

import type { PartySynergy } from "@/components/omnisight/agents/PartyBadge"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

export interface SynergyMatrixLegendProps {
  entries: PartySynergy[]
  className?: string
}

function formatPct(bonus: number | null): string {
  if (bonus === null) return ""
  const pct = Math.round(bonus * 100)
  if (!Number.isFinite(pct) || pct <= 0) return ""
  return `+${pct}%`
}

function bonusCell(entry: PartySynergy): string {
  const parts: string[] = []
  const xpPct = formatPct(entry.xpBonus)
  if (xpPct) parts.push(`${xpPct} party XP`)
  if (entry.skillBonusTarget && entry.skillBonus !== null) {
    const skillPct = formatPct(entry.skillBonus)
    if (skillPct) {
      parts.push(`${skillPct} ${entry.skillBonusTarget} skill XP`)
    }
  }
  return parts.length > 0 ? parts.join(" · ") : "no XP bonus"
}

export function SynergyMatrixLegend({
  entries,
  className,
}: SynergyMatrixLegendProps): ReactElement {
  if (entries.length === 0) {
    return (
      <section
        aria-label="Synergy matrix"
        className={cn("space-y-3", className)}
        data-testid="synergy-matrix-legend-empty"
      >
        <header className="flex items-center gap-2">
          <Sparkles
            className="size-4 text-violet-600 dark:text-violet-400"
            aria-hidden="true"
          />
          <h3 className="text-sm font-semibold leading-tight">Synergy matrix</h3>
        </header>
        <p className="rounded-md border border-dashed bg-muted/20 p-4 text-xs text-muted-foreground">
          No synergy entries loaded. Check{" "}
          <code className="font-mono">config/synergy_matrix.yaml</code>.
        </p>
      </section>
    )
  }

  return (
    <section
      aria-label="Synergy matrix"
      className={cn("space-y-3", className)}
      data-testid="synergy-matrix-legend"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Sparkles
            className="size-4 text-violet-600 dark:text-violet-400"
            aria-hidden="true"
          />
          <h3 className="text-sm font-semibold leading-tight">Synergy matrix</h3>
        </div>
        <p className="text-[11px] uppercase leading-none text-muted-foreground">
          {entries.length} cross-Guild combinations
        </p>
      </header>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-[180px]">Pair</TableHead>
            <TableHead className="w-[140px]">Label</TableHead>
            <TableHead className="w-[200px]">Bonus</TableHead>
            <TableHead>Summary</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {entries.map((entry) => (
            <TableRow
              key={entry.label}
              data-testid="synergy-matrix-legend-row"
              data-synergy-label={entry.label}
            >
              <TableCell>
                <div
                  className="flex flex-wrap gap-1"
                  data-testid="synergy-matrix-legend-pair"
                >
                  {entry.guilds.map((guild) => (
                    <Badge
                      key={guild}
                      variant="secondary"
                      className="text-[10px] uppercase"
                      data-testid="synergy-matrix-legend-guild"
                      data-guild={guild}
                    >
                      {guild}
                    </Badge>
                  ))}
                </div>
              </TableCell>
              <TableCell>
                <span className="text-sm font-medium">{entry.displayName}</span>
                <span className="ml-1 font-mono text-[11px] text-muted-foreground">
                  {entry.label}
                </span>
              </TableCell>
              <TableCell
                className="font-mono text-xs text-violet-700 dark:text-violet-300"
                data-testid="synergy-matrix-legend-bonus"
              >
                {bonusCell(entry)}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {entry.summary}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  )
}

export default SynergyMatrixLegend

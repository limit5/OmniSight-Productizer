/**
 * OP-913 F15 -- agent drift trend tile.
 */
"use client"

import * as React from "react"
import { Activity, AlertTriangle, TrendingDown, TrendingUp } from "lucide-react"

import { Badge } from "@/components/ui/badge"

export interface AgentDriftTrend {
  agent_class: string
  ticket_type: string
  current_n: number
  prior_n: number
  time_delta: number | null
  success_delta: number | null
  lessons_delta: number | null
  alerts: string[]
}

export function formatDelta(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "n/a"
  const sign = value > 0 ? "+" : ""
  return `${sign}${(value * 100).toFixed(1)}%`
}

export function driftSeverity(row: AgentDriftTrend): "ok" | "warn" | "page" {
  if (row.alerts.some((a) => a.startsWith("page:"))) return "page"
  if (row.alerts.length > 0) return "warn"
  return "ok"
}

export interface AgentDriftTileProps {
  rows: AgentDriftTrend[]
  generatedAt?: string | null
  testId?: string
}

export function AgentDriftTile({
  rows,
  generatedAt,
  testId = "agent-drift-tile",
}: AgentDriftTileProps) {
  const worst = rows.some((r) => driftSeverity(r) === "page")
    ? "page"
    : rows.some((r) => driftSeverity(r) === "warn")
      ? "warn"
      : "ok"
  const colour =
    worst === "page"
      ? "var(--critical-red)"
      : worst === "warn"
        ? "var(--warning-amber)"
        : "var(--validation-emerald)"

  return (
    <section
      data-testid={testId}
      data-severity={worst}
      className="flex min-h-0 flex-col gap-2 rounded-md border border-border bg-background/60 p-3"
    >
      <header className="flex items-center justify-between gap-2">
        <h3 className="inline-flex items-center gap-2 text-sm font-semibold">
          <Activity className="size-4" aria-hidden="true" />
          Agent drift trend
        </h3>
        <Badge
          variant="outline"
          data-testid={`${testId}-severity`}
          style={{ color: colour }}
        >
          {worst}
        </Badge>
      </header>
      {generatedAt && (
        <span
          data-testid={`${testId}-generated-at`}
          className="font-mono text-[10px] text-muted-foreground"
        >
          {generatedAt}
        </span>
      )}

      {rows.length === 0 ? (
        <div
          data-testid={`${testId}-empty`}
          className="rounded-md border border-dashed border-border/60 px-3 py-4 text-xs text-muted-foreground"
        >
          No comparable runner metrics yet.
        </div>
      ) : (
        <ul data-testid={`${testId}-list`} className="flex flex-col gap-2">
          {rows.slice(0, 5).map((row) => {
            const severity = driftSeverity(row)
            return (
              <li
                key={`${row.agent_class}:${row.ticket_type}`}
                data-testid={`${testId}-row-${row.agent_class}-${row.ticket_type}`}
                data-severity={severity}
                className="rounded-md border border-border/70 px-3 py-2 text-xs"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">{row.agent_class}</span>
                  <Badge variant="secondary" className="h-5 px-1.5 text-[11px]">
                    {row.ticket_type}
                  </Badge>
                  <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                    n={row.current_n} / {row.prior_n}
                  </span>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-[10px]">
                  <span data-testid={`${testId}-row-${row.agent_class}-${row.ticket_type}-time`}>
                    time {formatDelta(row.time_delta)}
                  </span>
                  <span data-testid={`${testId}-row-${row.agent_class}-${row.ticket_type}-success`}>
                    success {formatDelta(row.success_delta)}
                  </span>
                  <span data-testid={`${testId}-row-${row.agent_class}-${row.ticket_type}-lessons`}>
                    lessons {formatDelta(row.lessons_delta)}
                  </span>
                </div>
                {row.alerts.length > 0 && (
                  <div
                    data-testid={`${testId}-row-${row.agent_class}-${row.ticket_type}-alerts`}
                    className="mt-2 flex items-center gap-1 text-[11px] text-amber-300"
                  >
                    <AlertTriangle className="size-3" aria-hidden="true" />
                    {row.alerts.join(", ")}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <footer className="flex items-center gap-1 text-[10px] text-muted-foreground">
        {worst === "ok" ? (
          <TrendingUp className="size-3" aria-hidden="true" />
        ) : (
          <TrendingDown className="size-3" aria-hidden="true" />
        )}
        rolling 30d vs prior 30d
      </footer>
    </section>
  )
}

export default AgentDriftTile

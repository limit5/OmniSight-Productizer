/**
 * OP-913 F15 -- Cognee drift alerts list.
 */
"use client"

import * as React from "react"
import { AlertOctagon, CheckCircle2, ChevronLeft, ChevronRight } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

export type CogneeDriftKind = "stale" | "missing" | "schema"

export interface CogneeDriftAlert {
  kind: CogneeDriftKind | string
  class_name: string
  key: string
  first_seen?: string | null
  age_days?: number | null
}

export function paginateAlerts<T>(rows: T[], page: number, pageSize: number): T[] {
  if (pageSize <= 0) return rows
  const start = Math.max(0, page) * pageSize
  return rows.slice(start, start + pageSize)
}

export interface DriftAlertsListProps {
  entityCount: number
  status: "ok" | "drift" | "unknown" | string
  alerts: CogneeDriftAlert[]
  pageSize?: number
  testId?: string
}

export function DriftAlertsList({
  entityCount,
  status,
  alerts,
  pageSize = 5,
  testId = "drift-alerts-list",
}: DriftAlertsListProps) {
  const [page, setPage] = React.useState(0)
  const totalPages = Math.max(1, Math.ceil(alerts.length / pageSize))
  const safePage = Math.min(page, totalPages - 1)
  const pageRows = paginateAlerts(alerts, safePage, pageSize)
  const ok = status === "ok" && alerts.length === 0

  return (
    <section
      data-testid={testId}
      data-status={status}
      className="flex min-h-0 flex-col gap-2 rounded-md border border-border bg-background/60 p-3"
    >
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="inline-flex items-center gap-2 text-sm font-semibold">
          {ok ? (
            <CheckCircle2 className="size-4" aria-hidden="true" />
          ) : (
            <AlertOctagon className="size-4" aria-hidden="true" />
          )}
          Cognee KG
        </h3>
        <Badge variant="outline" data-testid={`${testId}-entity-count`}>
          {entityCount} entities
        </Badge>
        <Badge
          variant="outline"
          data-testid={`${testId}-status`}
          style={{
            color: ok ? "var(--validation-emerald)" : "var(--warning-amber)",
          }}
        >
          {status}
        </Badge>
      </header>

      {alerts.length === 0 ? (
        <div
          data-testid={`${testId}-empty`}
          className="rounded-md border border-dashed border-border/60 px-3 py-4 text-xs text-muted-foreground"
        >
          No Cognee drift alerts.
        </div>
      ) : (
        <ul data-testid={`${testId}-items`} className="flex flex-col gap-1.5">
          {pageRows.map((alert) => (
            <li
              key={`${alert.kind}:${alert.class_name}:${alert.key}`}
              data-testid={`${testId}-item-${alert.kind}-${alert.key}`}
              className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="h-5 px-1.5 text-[11px]">
                  {alert.kind}
                </Badge>
                <span className="font-mono text-[11px]">{alert.class_name}</span>
                <code className="ml-auto rounded-sm bg-muted/50 px-1.5 py-0.5 font-mono text-[10px]">
                  {alert.key}
                </code>
              </div>
              {(alert.age_days != null || alert.first_seen) && (
                <div className="mt-1 font-mono text-[10px] text-muted-foreground">
                  {alert.age_days != null ? `${alert.age_days}d old` : ""}
                  {alert.first_seen ? ` first_seen=${alert.first_seen}` : ""}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {totalPages > 1 && (
        <nav className="flex items-center justify-end gap-2 text-[11px]">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-testid={`${testId}-prev`}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={safePage <= 0}
            className="h-7 px-2"
          >
            <ChevronLeft className="size-3" aria-hidden="true" />
          </Button>
          <span
            data-testid={`${testId}-page`}
            className="font-mono text-[10px] text-muted-foreground"
          >
            page {safePage + 1} / {totalPages}
          </span>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-testid={`${testId}-next`}
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={safePage >= totalPages - 1}
            className="h-7 px-2"
          >
            <ChevronRight className="size-3" aria-hidden="true" />
          </Button>
        </nav>
      )}
    </section>
  )
}

export default DriftAlertsList

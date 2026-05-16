/**
 * OP-913 F15 -- per-fleet Memory Tool usage tile.
 *
 * Dumb render component: the parent owns API fetch/SSE state. This
 * mirrors DeployHistoryRow's pattern from OP-889 so tests can pass
 * deterministic data without touching the network.
 */
"use client"

import * as React from "react"
import { AlertTriangle, Database, Files, HardDrive } from "lucide-react"

import { Badge } from "@/components/ui/badge"

export type MemoryAction =
  | "ok"
  | "warn"
  | "page_operator"
  | "evict_required"
  | "MonitorReadFailed"

export interface MemoryFleetUsage {
  fleet: string
  bytes: number
  file_count: number
  cap_bytes: number
  pct_cap: number
  action: MemoryAction | string
  stale_files?: Array<{ path: string; unread_days: number; bytes: number }>
  timestamp: string
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`
}

export function memoryActionTone(action: string): string {
  switch (action) {
    case "ok":
      return "var(--validation-emerald)"
    case "warn":
      return "var(--warning-amber)"
    case "page_operator":
    case "evict_required":
    case "MonitorReadFailed":
      return "var(--critical-red)"
    default:
      return "var(--muted-foreground)"
  }
}

export interface MemoryUsageTileProps {
  rows: MemoryFleetUsage[]
  testId?: string
}

export function MemoryUsageTile({
  rows,
  testId = "memory-usage-tile",
}: MemoryUsageTileProps) {
  return (
    <section
      data-testid={testId}
      className="flex min-h-0 flex-col gap-2 rounded-md border border-border bg-background/60 p-3"
    >
      <header className="flex items-center justify-between gap-2">
        <h3 className="inline-flex items-center gap-2 text-sm font-semibold">
          <HardDrive className="size-4" aria-hidden="true" />
          Memory Tool
        </h3>
        <Badge variant="outline" data-testid={`${testId}-fleet-count`}>
          {rows.length} fleets
        </Badge>
      </header>

      {rows.length === 0 ? (
        <div
          data-testid={`${testId}-empty`}
          className="rounded-md border border-dashed border-border/60 px-3 py-4 text-xs text-muted-foreground"
        >
          No fleet memory metrics available.
        </div>
      ) : (
        <ul data-testid={`${testId}-list`} className="flex flex-col gap-2">
          {rows.map((row) => {
            const tone = memoryActionTone(row.action)
            const pct = Math.max(0, Math.min(100, row.pct_cap || 0))
            const staleCount = row.stale_files?.length ?? 0
            return (
              <li
                key={row.fleet}
                data-testid={`${testId}-fleet-${row.fleet}`}
                data-action={row.action}
                className="rounded-md border border-border/70 px-3 py-2 text-xs"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge
                    variant="outline"
                    className="h-5 px-1.5 font-mono text-[11px]"
                    style={{ color: tone }}
                    data-testid={`${testId}-fleet-${row.fleet}-action`}
                  >
                    {row.action}
                  </Badge>
                  <span className="font-semibold">{row.fleet}</span>
                  <span
                    data-testid={`${testId}-fleet-${row.fleet}-bytes`}
                    className="ml-auto font-mono text-[11px] text-muted-foreground"
                  >
                    {formatBytes(row.bytes)} / {formatBytes(row.cap_bytes)}
                  </span>
                </div>
                <div className="mt-2 h-2 overflow-hidden rounded-sm bg-muted">
                  <div
                    data-testid={`${testId}-fleet-${row.fleet}-bar`}
                    className="h-full"
                    style={{ width: `${pct}%`, backgroundColor: tone }}
                  />
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-3 font-mono text-[10px] text-muted-foreground">
                  <span data-testid={`${testId}-fleet-${row.fleet}-pct`}>
                    {pct.toFixed(1)}%
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <Files className="size-3" aria-hidden="true" />
                    {row.file_count} files
                  </span>
                  {staleCount > 0 && (
                    <span
                      data-testid={`${testId}-fleet-${row.fleet}-stale`}
                      className="inline-flex items-center gap-1"
                    >
                      <AlertTriangle className="size-3" aria-hidden="true" />
                      {staleCount} stale
                    </span>
                  )}
                  <span className="inline-flex items-center gap-1">
                    <Database className="size-3" aria-hidden="true" />
                    {row.timestamp}
                  </span>
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

export default MemoryUsageTile

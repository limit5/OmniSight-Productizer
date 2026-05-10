"use client"

/**
 * OP-820 -- SDK runner per-ticket spend tile.
 *
 * Admin-only tile that reads the OP-820 tracker snapshot and keeps the
 * row hot via the router's dedicated SSE stream.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import { AlertTriangle, DollarSign, RefreshCw, Wifi } from "lucide-react"
import {
  ApiError,
  getSdkCost,
  getSdkCostStreamUrl,
  type SdkCostEvent,
  type SdkCostResponse,
  type SdkCostRow,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const ROLE_ORDER = ["viewer", "operator", "admin", "super_admin"]

function roleAtLeast(role: string | undefined, minRole: string): boolean {
  const have = role ? ROLE_ORDER.indexOf(role) : -1
  const need = ROLE_ORDER.indexOf(minRole)
  return have >= 0 && need >= 0 && have >= need
}

function usd(value: number): string {
  return `$${value.toFixed(value >= 10 ? 2 : 4)}`
}

function applyCostEvent(rows: SdkCostRow[], event: SdkCostEvent): SdkCostRow[] {
  const next = rows.filter((row) => row.ticket !== event.ticket)
  const prior = rows.find((row) => row.ticket === event.ticket)
  next.push({
    ticket: event.ticket,
    rolling_24h_usd: (prior?.rolling_24h_usd ?? 0) + event.delta_usd,
    cumulative_usd: event.cumulative_usd,
    alert_count_24h: (prior?.alert_count_24h ?? 0) + 1,
    last_alert_at: event.fired_at,
  })
  return next.sort(
    (a, b) =>
      b.rolling_24h_usd - a.rolling_24h_usd
      || a.ticket.localeCompare(b.ticket),
  )
}

export interface SdkCostTileProps {
  /** Ticket key to stream. Default keeps the OP-820 tile self-contained. */
  ticket?: string
  /** Refresh cadence in ms; 0 disables polling. Default 60s. */
  pollIntervalMs?: number
}

export default function SdkCostTile({
  ticket = "OP-820",
  pollIntervalMs = 60_000,
}: SdkCostTileProps) {
  const { user, authMode, loading: authLoading } = useAuth()

  const isAdmin = useMemo(() => {
    if (authMode === "open") return true
    return roleAtLeast(user?.role, "admin")
  }, [authMode, user?.role])

  const [data, setData] = useState<SdkCostResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [live, setLive] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await getSdkCost(ticket))
    } catch (exc) {
      const msg =
        exc instanceof ApiError
          ? exc.message
          : exc instanceof Error
          ? exc.message
          : String(exc)
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [ticket])

  useEffect(() => {
    if (authLoading) return
    if (!isAdmin) {
      setLoading(false)
      return
    }
    void refresh()
    if (pollIntervalMs > 0) {
      const id = setInterval(() => void refresh(), pollIntervalMs)
      return () => clearInterval(id)
    }
    return undefined
  }, [authLoading, isAdmin, pollIntervalMs, refresh])

  useEffect(() => {
    if (authLoading || !isAdmin || typeof window === "undefined") return
    const es = new EventSource(getSdkCostStreamUrl(ticket))
    es.addEventListener("sdk.cost.updated", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data) as (
          SdkCostResponse | SdkCostEvent
        )
        setLive(true)
        if ("rows" in payload) {
          setData(payload)
          return
        }
        setData((prev) => ({
          ticket,
          window_hours: prev?.window_hours ?? 24,
          generated_at: new Date().toISOString(),
          rows: applyCostEvent(prev?.rows ?? [], payload),
          total_rolling_24h_usd: (prev?.total_rolling_24h_usd ?? 0) + payload.delta_usd,
          total_cumulative_usd: applyCostEvent(prev?.rows ?? [], payload).reduce(
            (sum, row) => sum + row.cumulative_usd,
            0,
          ),
          events: [...(prev?.events ?? []), payload].slice(-25),
        }))
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc))
      }
    })
    es.onerror = () => setLive(false)
    return () => es.close()
  }, [authLoading, isAdmin, ticket])

  if (authLoading) {
    return (
      <div
        className="rounded-md border border-[var(--border)] bg-[var(--card)] p-4 text-xs"
        data-testid="sdk-cost-tile"
      >
        <span className="text-[var(--muted-foreground)]">Loading SDK cost…</span>
      </div>
    )
  }

  if (!isAdmin) {
    return (
      <div
        className="rounded-md border border-[var(--border)] bg-[var(--card)] p-4 text-xs"
        data-testid="sdk-cost-tile"
      >
        <div className="flex items-center gap-2 text-[var(--muted-foreground)]">
          <DollarSign size={14} />
          <span>SDK cost (admin only)</span>
        </div>
      </div>
    )
  }

  return (
    <div
      className="rounded-md border border-[var(--border)] bg-[var(--card)] p-4 text-xs flex flex-col gap-3"
      data-testid="sdk-cost-tile"
    >
      <header className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 font-semibold min-w-0">
          <DollarSign size={14} />
          <span className="truncate">SDK spend</span>
          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
            {ticket}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Wifi
            size={12}
            className={live ? "text-emerald-500" : "text-[var(--muted-foreground)]"}
          />
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-2 py-0.5 text-[10px] font-mono hover:bg-[var(--secondary)]/40 disabled:opacity-50"
            data-testid="sdk-cost-refresh"
          >
            <RefreshCw size={10} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </div>
      </header>

      {error ? (
        <div
          className="flex items-center gap-2 text-[var(--destructive)] font-mono"
          data-testid="sdk-cost-error"
        >
          <AlertTriangle size={12} />
          <span>{error}</span>
        </div>
      ) : null}

      {data ? (
        <>
          <section className="grid grid-cols-2 gap-2" data-testid="sdk-cost-totals">
            <div className="rounded bg-[var(--background)] border border-[var(--border)] p-2">
              <div className="text-[10px] text-[var(--muted-foreground)] uppercase">
                24h
              </div>
              <div className="text-lg font-semibold font-mono">
                {usd(data.total_rolling_24h_usd)}
              </div>
            </div>
            <div className="rounded bg-[var(--background)] border border-[var(--border)] p-2">
              <div className="text-[10px] text-[var(--muted-foreground)] uppercase">
                Cumulative
              </div>
              <div className="text-lg font-semibold font-mono">
                {usd(data.total_cumulative_usd)}
              </div>
            </div>
          </section>

          <div className="overflow-x-auto" data-testid="sdk-cost-table">
            <table className="w-full border-collapse font-mono text-[11px]">
              <thead className="text-[10px] uppercase text-[var(--muted-foreground)]">
                <tr className="border-b border-[var(--border)]">
                  <th className="py-1 pr-2 text-left font-medium">Ticket</th>
                  <th className="py-1 px-2 text-right font-medium">24h</th>
                  <th className="py-1 px-2 text-right font-medium">Total</th>
                  <th className="py-1 pl-2 text-right font-medium">Alerts</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-2 text-[var(--muted-foreground)]">
                      No SDK cost alerts.
                    </td>
                  </tr>
                ) : (
                  data.rows.map((row) => (
                    <tr
                      key={row.ticket}
                      className="border-b border-[var(--border)]/60 last:border-0"
                    >
                      <td className="py-1.5 pr-2">{row.ticket}</td>
                      <td className="py-1.5 px-2 text-right">
                        {usd(row.rolling_24h_usd)}
                      </td>
                      <td className="py-1.5 px-2 text-right">
                        {usd(row.cumulative_usd)}
                      </td>
                      <td className="py-1.5 pl-2 text-right text-[var(--muted-foreground)]">
                        {row.alert_count_24h}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : !error && !loading ? (
        <div className="text-[var(--muted-foreground)]">No data.</div>
      ) : null}
    </div>
  )
}

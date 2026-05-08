"use client"

/**
 * OP-746 -- /admin/conflict-trend operator dashboard tile.
 *
 * Surfaces the conflict-rate observability the daily report writes to
 * /var/log/omnisight/conflict-report-<date>.json: 24h event count, the
 * 7d trend, and the top-3 hotspot files. Operators read it at a glance
 * to know if conflict pressure is climbing across runner-fleet changes.
 *
 * Auth gating
 * -----------
 * Admin+ only. The backend route is gated by ``auth.require_admin``;
 * non-admins see the locked-state copy.
 *
 * Module-global state audit
 * -------------------------
 * None introduced. Per-component React state only; the typed
 * ``getConflictTrend()`` wrapper handles the network.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import { Activity, AlertTriangle, RefreshCw } from "lucide-react"
import {
  ApiError,
  getConflictTrend,
  type ConflictTrendResponse,
} from "@/lib/api"
import { useAuth } from "@/lib/auth-context"

const ROLE_ORDER = ["viewer", "operator", "admin", "super_admin"]

function roleAtLeast(role: string | undefined, minRole: string): boolean {
  const have = role ? ROLE_ORDER.indexOf(role) : -1
  const need = ROLE_ORDER.indexOf(minRole)
  return have >= 0 && need >= 0 && have >= need
}

export interface ConflictTrendTileProps {
  /** Refresh cadence in ms; 0 disables auto-refresh. Default 60s. */
  pollIntervalMs?: number
  /** Optional href for the "Open daily report" link in the footer. */
  reportLinkHref?: string
}

export default function ConflictTrendTile({
  pollIntervalMs = 60_000,
  reportLinkHref,
}: ConflictTrendTileProps) {
  const { user, authMode, loading: authLoading } = useAuth()

  const isAdmin = useMemo(() => {
    if (authMode === "open") return true
    return roleAtLeast(user?.role, "admin")
  }, [authMode, user?.role])

  const [data, setData] = useState<ConflictTrendResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await getConflictTrend()
      setData(res)
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
  }, [])

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

  if (authLoading) {
    return (
      <div
        className="rounded-md border border-[var(--border)] bg-[var(--card)] p-4 text-xs"
        data-testid="conflict-trend-tile"
      >
        <span className="text-[var(--muted-foreground)]">
          Loading conflict trend…
        </span>
      </div>
    )
  }

  if (!isAdmin) {
    return (
      <div
        className="rounded-md border border-[var(--border)] bg-[var(--card)] p-4 text-xs"
        data-testid="conflict-trend-tile"
      >
        <div className="flex items-center gap-2 text-[var(--muted-foreground)]">
          <Activity size={14} />
          <span>Conflict trend (admin only)</span>
        </div>
      </div>
    )
  }

  return (
    <div
      className="rounded-md border border-[var(--border)] bg-[var(--card)] p-4 text-xs flex flex-col gap-3"
      data-testid="conflict-trend-tile"
    >
      <header className="flex items-center justify-between">
        <div className="flex items-center gap-2 font-semibold">
          <Activity size={14} />
          <span>Conflict trend</span>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
          className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-2 py-0.5 text-[10px] font-mono hover:bg-[var(--secondary)]/40 disabled:opacity-50"
          data-testid="conflict-trend-refresh"
        >
          <RefreshCw size={10} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
      </header>

      {error ? (
        <div
          className="flex items-center gap-2 text-[var(--destructive)] font-mono"
          data-testid="conflict-trend-error"
        >
          <AlertTriangle size={12} />
          <span>{error}</span>
        </div>
      ) : null}

      {data ? (
        <>
          <section
            className="grid grid-cols-2 gap-2"
            data-testid="conflict-trend-totals"
          >
            <div className="rounded bg-[var(--background)] border border-[var(--border)] p-2">
              <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">
                24h events
              </div>
              <div className="text-lg font-semibold font-mono">
                {data.window_24h.total_events}
              </div>
              <div className="text-[10px] text-[var(--muted-foreground)] mt-0.5">
                {data.window_24h.distinct_changes} distinct PSes
              </div>
            </div>
            <div className="rounded bg-[var(--background)] border border-[var(--border)] p-2">
              <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider">
                7d events
              </div>
              <div className="text-lg font-semibold font-mono">
                {data.window_7d.total_events}
              </div>
              <div className="text-[10px] text-[var(--muted-foreground)] mt-0.5">
                {data.window_7d.distinct_changes} distinct PSes
              </div>
            </div>
          </section>

          <section data-testid="conflict-trend-hotspots">
            <div className="text-[10px] text-[var(--muted-foreground)] uppercase tracking-wider mb-1">
              Top hotspots (24h)
            </div>
            {data.window_24h.hotspots.length === 0 ? (
              <div className="text-[var(--muted-foreground)]">
                No conflict events recorded.
              </div>
            ) : (
              <ul className="font-mono text-[11px] space-y-0.5">
                {data.window_24h.hotspots.map((h) => (
                  <li
                    key={h.file}
                    className="flex justify-between gap-2"
                  >
                    <span className="truncate" title={h.file}>
                      {h.file}
                    </span>
                    <span className="text-[var(--muted-foreground)]">
                      {h.events}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {reportLinkHref ? (
            <footer className="text-[10px] text-[var(--muted-foreground)] font-mono">
              <a
                href={reportLinkHref}
                className="underline hover:text-[var(--foreground)]"
              >
                Open daily report →
              </a>
            </footer>
          ) : (
            <footer className="text-[10px] text-[var(--muted-foreground)] font-mono">
              Daily report at /var/log/omnisight/conflict-report-&lt;date&gt;.json
            </footer>
          )}
        </>
      ) : !error && !loading ? (
        <div className="text-[var(--muted-foreground)]">No data.</div>
      ) : null}
    </div>
  )
}

"use client"

/**
 * OP-1729 — Mount the BP fleet-lanes dispatch board.
 *
 * The BpFleetLanes component (the only surface that passes a real
 * `blockId` to <Block>, and thus the only place the block-model
 * Share / Save-as-Runbook context menu gated on `ui.block_model.enabled`
 * can appear) had zero route mounting it. This page is that mount point.
 *
 * Data sources (read-only contract, backend already shipped in
 * OP-1504 WP.10 — see `backend/routers/bp_fleet_lanes.py`):
 *   - GET  /api/v1/bp/fleet/lanes         → `getFleetLanes`
 *   - GET  /api/v1/bp/fleet/agents/{id}   → `getFleetAgentDetail`
 *   - POST /api/v1/bp/fleet/agents/{id}/revoke → `revokeFleetAgent`
 *
 * The page stays presentational on top of the existing component and
 * passes the real agent ids the snapshot provides (BpFleetLanes already
 * wires `blockId={agent.id}`) — never a fabricated blockId.
 */

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { ArrowLeft, ChevronRight, LayoutGrid, Loader2, RefreshCw } from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import {
  getFleetLanes,
  getFleetAgentDetail,
  revokeFleetAgent,
  type FleetLanesSnapshot,
} from "@/lib/api"
import { BpFleetLanes } from "@/components/omnisight/bp-fleet-lanes"

const EMPTY_SNAPSHOT: FleetLanesSnapshot = {
  lanes: { active: [], scheduled: [], ambient: [], history: [] },
  counts: { active: 0, scheduled: 0, ambient: 0, history: 0 },
}

export default function BpFleetPage() {
  const auth = useAuth()
  const router = useRouter()
  const [snapshot, setSnapshot] = useState<FleetLanesSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") {
      const next =
        typeof window !== "undefined"
          ? window.location.pathname + window.location.search
          : "/bp/fleet"
      router.replace(`/login?next=${encodeURIComponent(next)}`)
    }
  }, [auth.loading, auth.user, auth.authMode, router])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setSnapshot(await getFleetLanes())
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
      setSnapshot(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") return
    const timer = window.setTimeout(() => {
      void refresh()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [auth.loading, auth.user, auth.authMode, refresh])

  if (auth.loading || (!auth.user && auth.authMode !== "open")) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying operator session...
        </div>
      </main>
    )
  }

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="bp-fleet-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-start justify-between gap-4 mb-6 flex-wrap">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link
                href="/"
                className="hover:text-[var(--foreground)] inline-flex items-center gap-1"
              >
                <ArrowLeft size={10} /> dashboard
              </Link>
              <ChevronRight size={10} />
              <span className="text-[var(--foreground)]">bp / fleet</span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <LayoutGrid size={20} />
              Blueprint Fleet
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Four-lane dispatch board (active / scheduled / ambient /
              history) for the Blueprint fleet. Click an agent card to open
              its execution-envelope detail panel.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="bp-fleet-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        {error && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
            data-testid="bp-fleet-error"
          >
            Failed to load fleet lanes: {error}
          </div>
        )}

        {loading && !snapshot ? (
          <div className="rounded-md border border-dashed bg-muted/20 p-6 text-center text-xs font-mono text-muted-foreground">
            <Loader2 size={14} className="animate-spin inline-block mr-2" />
            Loading fleet lanes...
          </div>
        ) : (
          <BpFleetLanes
            snapshot={snapshot ?? EMPTY_SNAPSHOT}
            onLoadDetail={getFleetAgentDetail}
            onRevoke={revokeFleetAgent}
          />
        )}
      </div>
    </main>
  )
}

"use client"

/**
 * Q.3-SUB-1 (#297): workflow_runs state hook with cross-device SSE sync.
 *
 * Combines a REST refresh (via {@link api.listWorkflowRuns}) with a
 * live {@link api.subscribeEvents} listener for ``workflow_updated``
 * — when another device finishes / cancels / retries a run the local
 * list patches in place so the UI no longer waits up to POLL_MS
 * (15 s) for the next tick.
 *
 * The backend currently emits ``workflow_updated`` at
 * ``broadcast_scope='user'`` but the EventBus only enforces ``tenant``
 * scope today (see Q.4 #298). Until then we trust the bus's user-
 * scope delivery the same way Q.2's new-device-login card does, and
 * simply patch any run-id we already know about; an update for an
 * unknown run-id triggers a background refresh so a fresh run
 * created on another device appears on this one.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import * as api from "@/lib/api"
import { onTenantChange } from "@/lib/tenant-context"

const DEFAULT_POLL_MS = 15_000

export interface UseWorkflowsOptions {
  status?: string
  limit?: number
  pollMs?: number
  enabled?: boolean
}

export interface UseWorkflowsResult {
  runs: api.WorkflowRunSummary[] | null
  error: string | null
  refresh: () => Promise<void>
}

export function useWorkflows(opts: UseWorkflowsOptions = {}): UseWorkflowsResult {
  const { status, limit = 50, pollMs = DEFAULT_POLL_MS, enabled = true } = opts

  const [runs, setRuns] = useState<api.WorkflowRunSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    if (!enabled) return
    try {
      const list = await api.listWorkflowRuns({ status, limit })
      if (!mountedRef.current) return
      setRuns(list)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [status, limit, enabled])

  useEffect(() => {
    mountedRef.current = true
    if (!enabled) return () => {
      mountedRef.current = false
    }

    void refresh() // eslint-disable-line react-hooks/set-state-in-effect -- fetch-on-mount populates state from network

    const es = api.subscribeEvents((event) => {
      if (event.event !== "workflow_updated") return
      const d = event.data
      const runId = d.run_id
      const newStatus = d.status
      const newVersion = d.version
      if (!runId) return

      // Drop runs that no longer match the active status filter so
      // the list stays in sync with the REST query; patch the rest
      // in place.
      setRuns((prev) => {
        if (!prev) return prev
        const idx = prev.findIndex((r) => r.id === runId)
        if (idx < 0) {
          // Unknown run — could be freshly created on another device.
          // Kick a background refresh; do not mutate the list here.
          void refresh()
          return prev
        }
        if (status && status !== "all" && newStatus !== status) {
          return prev.filter((r) => r.id !== runId)
        }
        const next = prev.slice()
        next[idx] = { ...next[idx], status: newStatus, version: newVersion }
        return next
      })
    })

    const t = pollMs > 0 ? setInterval(() => void refresh(), pollMs) : null

    // Y8 row 1: drop the cached list and refetch when the operator
    // switches tenant. The X-Tenant-Id header is already flipped by
    // tenant-context.switchTenant() before _notifyTenantChange fires,
    // so the refresh() call below hits the new tenant.
    const unsubTenant = onTenantChange(() => {
      if (!mountedRef.current) return
      setRuns(null)
      void refresh()
    })

    return () => {
      mountedRef.current = false
      es.close()
      if (t) clearInterval(t)
      unsubTenant()
    }
  }, [refresh, status, pollMs, enabled])

  return { runs, error, refresh }
}

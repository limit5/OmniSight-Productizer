"use client"

/**
 * BS.7.4 — useInstallJobs() hook.
 *
 * Subscribes to the SSE ``installer_progress`` event channel and
 * exposes the live set of install_jobs the BS.7.3 InstallProgressDrawer
 * (and future BS.7.5 catalog card / BS.7.6 retry) consumes.
 *
 * Backend wire contract
 * ─────────────────────
 * ``backend/routers/installer.py::report_progress`` UPDATEs the
 * install_jobs row, then ``backend/events.emit_installer_progress``
 * publishes a SINGLE SSE channel — ``installer_progress`` — carrying
 * the full delta (job_id, state, stage, bytes_done, bytes_total,
 * eta_seconds, log_tail, sidecar_id, entry_id). The BS.7.4 TODO row
 * description reads as if there were three channels
 * (``install.progress`` / ``install.completed`` / ``install.failed``)
 * but there is only one — ``state`` field in the payload discriminates
 * the three semantic cases. State-based dispatch in this hook covers
 * all three without forcing a wire format split that does not exist
 * on the backend (single emit point = single SSE channel = single
 * subscription).
 *
 * Drawer / catalog-card integration
 * ─────────────────────────────────
 * The drawer (BS.7.3) is purely presentational — it filters by state
 * internally and drops ``completed``/``failed``/``cancelled`` rows
 * from its own UI. This hook therefore keeps every observed job in
 * ``jobs`` regardless of state, and lets the consumer apply its own
 * predicate. BS.7.5 catalog-card and BS.7.6 retry-button consumers
 * will read the terminal rows the drawer hides.
 *
 * No REST seed (deliberate scope limit)
 * ─────────────────────────────────────
 * The BS.7.4 row literally says "SSE subscribe + 寫進 local state".
 * On a page reload mid-install the drawer stays empty until the next
 * SSE progress tick (~250 ms — 5 s depending on the sidecar cadence),
 * which is acceptable for a first cut. BS.7.5 / BS.7.6 will optionally
 * add a one-shot ``GET /installer/jobs?state=running`` seed when they
 * wire their initial render path; today the helper is omitted to keep
 * the row's blast radius minimal.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Pure per-instance React state:
 *   - ``jobs`` (useState array) is the rendered snapshot
 *   - ``mountedRef`` guards setState after unmount
 * No module-level mutable state, no in-memory cache, no thread-locals.
 * Each tab subscribes via ``api.subscribeEvents()`` (which itself
 * shares a single EventSource per tab, not across tabs); cross-tab
 * consistency is the backend's job — ``backend/events.publish``
 * fans out to every connected SSE client in the tenant via the
 * EventBus + Redis Pub/Sub cross-worker bridge. Answer #3 of the
 * three valid audit answers — deliberately per-tab because there is
 * no cross-browser-tab "shared install drawer" state; each tab
 * derives its snapshot from the same authoritative SSE stream.
 *
 * Read-after-write timing
 * ───────────────────────
 * N/A — pure SSE consumer. The hook never writes to the backend; it
 * only reads from the SSE stream. There is no ``asyncio.gather``, no
 * write-then-read ordering risk. SSE delivery is monotonic per
 * job_id (backend UPDATE is wrapped in a transaction with FOR UPDATE
 * before the publish call), so a stale earlier event cannot land
 * after a fresher one for the same job.
 */

import { useCallback, useEffect, useRef, useState } from "react"

import * as api from "@/lib/api"
import type { InstallJob, InstallJobState } from "@/lib/api"

/** Shape of the ``installer_progress`` SSE event payload. Mirrored
 *  from ``backend/events.emit_installer_progress`` 1:1 — the bus's
 *  ``publish`` autostamps ``timestamp`` + ``_session_id`` /
 *  ``_broadcast_scope`` / ``_tenant_id``. We tolerate the underscore-
 *  prefixed fields silently (they are read by ``_shouldDeliverEvent``
 *  upstream of this consumer) and only consume the public payload. */
interface InstallerProgressData {
  job_id: string
  state: InstallJobState
  stage: string
  bytes_done: number
  bytes_total: number | null
  eta_seconds: number | null
  log_tail: string
  sidecar_id: string | null
  entry_id: string | null
  timestamp?: string
}

/**
 * BS.7.4 helper — synthesize an ``InstallJob`` shape from an
 * ``installer_progress`` SSE event when the hook has no prior row
 * for this job_id (operator opened the page after the install was
 * already running, or BS.7.5 catalog-card receives a new install
 * not in its REST snapshot).
 *
 * The event payload carries the eight fields the drawer actually
 * reads (id, entry_id, state, bytes_done, bytes_total, eta_seconds,
 * log_tail, sidecar_id); the rest of ``InstallJob``
 * (idempotency_key, requested_by, queued_at, …) are zero-filled
 * with empty strings / nulls because the drawer never reads them.
 * BS.7.5 will optionally enrich ``result_json.display_name`` via a
 * one-shot ``GET /installer/jobs/{id}`` round-trip if it needs the
 * entry's human-readable name; today the drawer falls back to
 * ``entry_id`` (visible string identifier from the catalog) when
 * ``result_json`` is null.
 */
export function synthesizeInstallJobFromProgress(
  d: InstallerProgressData,
): InstallJob {
  return {
    id: d.job_id,
    tenant_id: "",
    entry_id: d.entry_id ?? "",
    state: d.state,
    idempotency_key: "",
    sidecar_id: d.sidecar_id ?? null,
    protocol_version: 0,
    bytes_done: d.bytes_done,
    bytes_total: d.bytes_total,
    eta_seconds: d.eta_seconds,
    log_tail: d.log_tail,
    result_json: null,
    error_reason: null,
    pep_decision_id: null,
    requested_by: "",
    queued_at: "",
    claimed_at: null,
    started_at: null,
    completed_at: null,
  }
}

/**
 * BS.7.4 helper — merge a fresh ``installer_progress`` SSE event
 * payload onto a pre-existing row, preferring SSE values where
 * present and preserving fields not in the SSE payload (e.g.
 * ``result_json`` for ``display_name`` — populated by BS.7.5's REST
 * enrichment call) from the prior row.
 *
 * ``bytes_total`` is sticky: once we know the install size we keep
 * it, since the sidecar may emit later progress ticks with
 * ``bytes_total=None`` (e.g. docker pull layer-count update where
 * total is computed once at first manifest fetch). Same for
 * ``sidecar_id`` / ``entry_id`` — null in the SSE payload means
 * "unknown / not provided", not "clear it".
 */
export function mergeInstallJobFromProgress(
  prev: InstallJob,
  d: InstallerProgressData,
): InstallJob {
  return {
    ...prev,
    state: d.state,
    bytes_done: d.bytes_done,
    bytes_total: d.bytes_total ?? prev.bytes_total,
    eta_seconds: d.eta_seconds,
    log_tail: d.log_tail,
    sidecar_id: d.sidecar_id ?? prev.sidecar_id,
    entry_id: d.entry_id ?? prev.entry_id,
  }
}

export interface UseInstallJobsResult {
  /** All install_jobs the hook has observed since mount, terminal
   *  states included. Consumers (drawer / catalog card / retry) apply
   *  their own state filter. */
  jobs: InstallJob[]
  /** Drop a row from local state. Used by BS.7.7 cancel optimistic
   *  flow (operator hits cancel → row goes back to ``available`` on
   *  the catalog card before the SSE state-flip lands) and by tests. */
  removeJob: (jobId: string) => void
  /** Reset the hook's state — only used by tests today. */
  reset: () => void
}

/**
 * Subscribe to ``installer_progress`` SSE and expose the live job
 * list. Returns a stable handle whose ``jobs`` array re-renders every
 * time a new event lands.
 *
 * Idempotent under React StrictMode double-mount: ``mountedRef``
 * guards setState after unmount and the SSE handle's ``.close()``
 * cleanly removes the listener (the underlying EventSource is shared
 * across the tab, so closing here only un-registers this hook's
 * callback — no double-tear-down).
 */
export function useInstallJobs(): UseInstallJobsResult {
  const [jobs, setJobs] = useState<InstallJob[]>([])
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    const handle = api.subscribeEvents((event) => {
      if (event.event !== "installer_progress") return
      if (!mountedRef.current) return
      const d = event.data as unknown as InstallerProgressData
      if (!d || typeof d.job_id !== "string" || d.job_id.length === 0) return

      setJobs((prev) => {
        const idx = prev.findIndex((j) => j.id === d.job_id)
        if (idx < 0) {
          return [...prev, synthesizeInstallJobFromProgress(d)]
        }
        const next = prev.slice()
        next[idx] = mergeInstallJobFromProgress(prev[idx], d)
        return next
      })
    })

    return () => {
      mountedRef.current = false
      handle.close()
    }
  }, [])

  const removeJob = useCallback((jobId: string) => {
    setJobs((prev) => prev.filter((j) => j.id !== jobId))
  }, [])

  const reset = useCallback(() => {
    setJobs([])
  }, [])

  return { jobs, removeJob, reset }
}

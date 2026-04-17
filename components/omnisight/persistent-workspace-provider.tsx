/**
 * V0 #4 — Persistence-enabled wrapper around the V0 #3 `WorkspaceProvider`.
 *
 * Responsibilities:
 *   1. On first client mount, hydrate the live workspace state from
 *      `localStorage` via the provider's public setters.
 *   2. On the same mount, fetch the latest backend snapshot and, if
 *      its `savedAt` is strictly newer than the localStorage seed,
 *      overlay it on top.
 *   3. Mirror subsequent state mutations back to localStorage (sync)
 *      and to the backend (debounced, best-effort).
 *
 * Why a two-phase (render → effect → hydrate) pattern instead of
 * seeding `useState` from localStorage in a lazy initializer?
 * Next.js renders Client Components on the server to produce initial
 * HTML, then re-runs lazy initializers on the client during
 * hydration.  Because `localStorage` doesn't exist on the server, a
 * seed read in lazy init returns `null` on the server but may return
 * a real envelope on the client — React flags this as a hydration
 * mismatch.  Doing the hydration inside `useEffect` keeps the first
 * server+client paint identical (defaults) and lets the persisted
 * state replace them after mount.
 *
 * Why wrap instead of baking persistence into `WorkspaceProvider`?
 * V0 #3 deliberately keeps that provider dependency-free so the
 * state shape can be asserted in isolation.  Persistence layers on
 * top via the public setter API and the public `initialState` seam.
 *
 * Re-hydration ordering guarantees:
 *   - localStorage wins first (fast, sync after mount).
 *   - If the backend snapshot is strictly newer (by `savedAt`) it
 *     overwrites the localStorage seed — this is how cross-device
 *     edits propagate in.
 *   - During hydration the persistence write-through is suppressed
 *     via a ref flag so the snapshot we just applied doesn't
 *     round-trip back to the backend as a "fresh" save.
 */
"use client"

import * as React from "react"

import type { WorkspaceType } from "@/app/workspace/[type]/layout"
import {
  WorkspaceProvider,
  useWorkspaceContext,
  type WorkspaceContextValue,
} from "@/components/omnisight/workspace-context"
import {
  fetchWorkspaceSnapshotFromBackend,
  loadWorkspaceSnapshotFromStorage,
  pickNewerEnvelope,
  pushWorkspaceSnapshotToBackend,
  saveWorkspaceSnapshotToStorage,
  type WorkspaceSnapshotEnvelope,
  type WorkspaceSnapshotState,
} from "@/hooks/use-workspace-persistence"

// Backend sync is debounced — mashing setters in quick succession
// shouldn't translate into a PUT per keystroke.  localStorage writes
// stay synchronous so a tab switch / reload always has the latest.
const DEFAULT_BACKEND_DEBOUNCE_MS = 400

export interface PersistentWorkspaceProviderProps {
  type: WorkspaceType
  children: React.ReactNode
  /**
   * Override the backend-sync debounce in tests (default: 400ms).
   * Setting `0` fires on every state change — which is what our
   * contract tests exercise.
   */
  backendDebounceMs?: number
  /**
   * Skip the backend hydrate-on-mount and the PUT write-through.
   * Used by tests that want to exercise the localStorage-only path
   * without mocking `fetch`, and by any caller that wants a
   * pure-browser workspace.
   */
  disableBackendSync?: boolean
}

function WorkspacePersistenceBridge({
  type,
  backendDebounceMs,
  disableBackendSync,
}: {
  type: WorkspaceType
  backendDebounceMs: number
  disableBackendSync: boolean
}) {
  const ctx = useWorkspaceContext()
  const { project, agentSession, preview } = ctx

  // Setters come from context as stable useCallback refs, but we pin
  // them into mutable refs so the hydrate effect (bound to `type`)
  // can reach the latest without re-running on every ctx change.
  const setProjectRef = React.useRef(ctx.setProject)
  const setAgentRef = React.useRef(ctx.setAgentSession)
  const setPreviewRef = React.useRef(ctx.setPreviewState)
  setProjectRef.current = ctx.setProject
  setAgentRef.current = ctx.setAgentSession
  setPreviewRef.current = ctx.setPreviewState

  const hasHydratedRef = React.useRef(false)
  const suppressSaveRef = React.useRef(false)

  // ── 1. Hydrate from localStorage + backend on first mount ─────────────
  React.useEffect(() => {
    let cancelled = false
    const ctrl = new AbortController()

    const applyEnvelope = (env: WorkspaceSnapshotEnvelope) => {
      suppressSaveRef.current = true
      try {
        if (env.state.project) setProjectRef.current(env.state.project)
        if (env.state.agentSession) setAgentRef.current(env.state.agentSession)
        if (env.state.preview) setPreviewRef.current(env.state.preview)
      } finally {
        // Release on a microtask so the write-through effect — which
        // runs after React flushes the batched setters — still sees
        // the flag set and skips the save.
        queueMicrotask(() => {
          suppressSaveRef.current = false
        })
      }
    }

    const localSeed = loadWorkspaceSnapshotFromStorage(type)
    if (localSeed) applyEnvelope(localSeed)

    // Mark hydrated *after* local seed application so the write-through
    // effect skips until local hydration has had a chance to run.
    hasHydratedRef.current = true

    if (!disableBackendSync) {
      void (async () => {
        const backend = await fetchWorkspaceSnapshotFromBackend(type, {
          signal: ctrl.signal,
        })
        if (cancelled || !backend) return
        const winner = pickNewerEnvelope(localSeed, backend)
        if (winner !== backend) return
        applyEnvelope(backend)
      })()
    }

    return () => {
      cancelled = true
      ctrl.abort()
    }
  }, [type, disableBackendSync])

  // ── 2. Mirror live state changes to localStorage + backend ────────────
  React.useEffect(() => {
    if (!hasHydratedRef.current) return
    if (suppressSaveRef.current) return
    const snapshot: WorkspaceSnapshotState = { project, agentSession, preview }
    const savedAt = new Date().toISOString()
    saveWorkspaceSnapshotToStorage(type, snapshot, { savedAt })

    if (disableBackendSync) return
    const timer = setTimeout(() => {
      // Fire-and-forget — persistence errors don't surface to the UI.
      void pushWorkspaceSnapshotToBackend(type, snapshot, { savedAt })
    }, backendDebounceMs)
    return () => clearTimeout(timer)
  }, [type, project, agentSession, preview, backendDebounceMs, disableBackendSync])

  return null
}

export function PersistentWorkspaceProvider({
  type,
  children,
  backendDebounceMs = DEFAULT_BACKEND_DEBOUNCE_MS,
  disableBackendSync = false,
}: PersistentWorkspaceProviderProps) {
  return (
    <WorkspaceProvider type={type}>
      <WorkspacePersistenceBridge
        type={type}
        backendDebounceMs={backendDebounceMs}
        disableBackendSync={disableBackendSync}
      />
      {children}
    </WorkspaceProvider>
  )
}

export type { WorkspaceContextValue }
export default PersistentWorkspaceProvider

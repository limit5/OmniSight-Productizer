/**
 * Q.6 #300 (2026-04-24, checkbox 2) — restore-on-mount for draft slots.
 *
 * Companion to `useDraftPersistence`: fires a single
 * `GET /user/drafts/{slot_key}` on mount and hands the result to
 * the caller via `onRestore`. Both composers (INVOKE command bar
 * + workspace chat) call this on mount so a new device / fresh tab
 * picks up the last server-side draft.
 *
 * Design choices:
 *   1. Fire **once** on mount — the effect deps are intentionally
 *      mount-scoped (`slotKey` + `enabled`); we do not re-restore if
 *      the parent rerenders with a new callback identity.
 *   2. Swallow errors silently — a flaky restore on page load must
 *      not block the operator from typing. The composer falls back
 *      to whatever local-storage cached value the parent seeded.
 *   3. Cancellation guard against unmount — a late-resolving fetch
 *      on a torn-down composer would cause a setState warning.
 *   4. No-op on a confirmed empty row (`content === ""`). The miss
 *      path is shaped (``content: "", updated_at: null``) precisely
 *      so the caller can skip the branch — but the hook also short
 *      circuits so `onRestore` is only called when there is actually
 *      something to restore.
 *
 * Q.6 checkbox 4 (2026-04-24) — conflict-on-restore toast:
 * Compare the server-returned ``updated_at`` against the local
 * storage cache written by ``useDraftPersistence`` on the previous
 * successful PUT.
 *
 *   - server.updated_at > local.updated_at (or no local entry at all
 *     on a fresh device) → adopt remote AND emit a ``draft_synced``
 *     event on the ``onDraftSynced`` bus so the toast center fires
 *     「從他裝置同步了草稿」.
 *   - server.updated_at == local.updated_at → same row we already
 *     persisted from this device; silently adopt (or no-op) and
 *     skip the toast.
 *   - server.updated_at < local.updated_at → stale server (e.g.
 *     clock skew, or we won the race); skip the toast, still adopt
 *     on a non-empty content so the composer stays in sync with the
 *     authoritative row — the next 500 ms debounce tick from this
 *     device will overwrite it anyway per the last-writer-wins spec.
 */
"use client"

import * as React from "react"

import { getUserDraft, type DraftResponse } from "@/lib/api"
import {
  emitDraftSynced,
  readDraftLocalEntry,
  writeDraftLocalEntry,
} from "@/lib/draft-sync-bus"

export interface UseDraftRestoreOptions {
  /** Slot key — `invoke:main` / `chat:main` / `chat:<thread_id>`. */
  slotKey: string
  /**
   * Callback fired once with the restored draft when the server
   * returns a non-empty row. Never fired on a cold slot.
   */
  onRestore: (draft: DraftResponse) => void
  /** Pull-out lever: when false, no fetch is issued. */
  enabled?: boolean
  /**
   * Optional override for the underlying reader (tests inject a spy;
   * the dispatcher contract is `(slotKey) → Promise<DraftResponse>`).
   * Defaults to `getUserDraft` from `lib/api.ts`.
   */
  reader?: (slotKey: string) => Promise<DraftResponse>
}

/**
 * Fire `GET /user/drafts/{slotKey}` exactly once on mount. The
 * returned draft is passed to `onRestore` **only** when the row
 * has actual content; empty / missing rows are silent no-ops so
 * the caller does not need an "is it null" branch.
 *
 * The hook swallows errors on purpose: restore is a convenience,
 * not a correctness gate, and a failed restore must never surface
 * a toast during page load.
 */
export function useDraftRestore({
  slotKey,
  onRestore,
  enabled = true,
  reader = getUserDraft,
}: UseDraftRestoreOptions): void {
  // Keep the latest callback/reader in a ref so the effect can fire
  // exactly once on mount without being retriggered by caller-side
  // identity churn (InvokeCore / WorkspaceChat recreate inline
  // lambdas each render).
  const onRestoreRef = React.useRef(onRestore)
  const readerRef = React.useRef(reader)
  React.useEffect(() => {
    onRestoreRef.current = onRestore
    readerRef.current = reader
  }, [onRestore, reader])

  React.useEffect(() => {
    if (!enabled) return
    if (!slotKey) return
    let cancelled = false
    void (async () => {
      try {
        const res = await readerRef.current(slotKey)
        if (cancelled) return
        if (!res || typeof res.content !== "string") return
        if (res.content.length === 0) return

        // Q.6 checkbox 4 — conflict detection. Compare the server's
        // ``updated_at`` against the local-storage echo from the
        // previous successful PUT by this device. Emit a sync event
        // when the remote is strictly newer, OR when this device has
        // no local echo at all (fresh device restoring a draft from
        // a peer) — both cases surface the toast「從他裝置同步了草稿」.
        const local = readDraftLocalEntry(slotKey)
        const remoteTs = typeof res.updated_at === "number" ? res.updated_at : null
        let syncedFromPeer = false
        if (remoteTs !== null) {
          if (local === null) {
            // Fresh device — the content came from a peer by
            // definition (this device never wrote the slot).
            syncedFromPeer = true
          } else if (remoteTs > local.updated_at) {
            syncedFromPeer = true
          } else if (
            // Same timestamp but content diverges — treat as a peer
            // write we happened to be identical-ts to (extremely rare,
            // but ``updated_at`` is only second-granularity on some
            // clocks). Surface the toast for safety.
            remoteTs === local.updated_at &&
            res.content !== local.content
          ) {
            syncedFromPeer = true
          }
        }

        onRestoreRef.current(res)

        // Echo the authoritative server row into local storage so a
        // subsequent restore (same session, new tab) sees the freshly
        // adopted value as the "current local" baseline.
        if (remoteTs !== null) {
          writeDraftLocalEntry(slotKey, {
            content: res.content,
            updated_at: remoteTs,
          })
        }

        if (syncedFromPeer && remoteTs !== null) {
          emitDraftSynced({
            slotKey,
            content: res.content,
            remoteUpdatedAt: remoteTs,
            localUpdatedAt: local?.updated_at ?? null,
          })
        }
      } catch {
        // Deliberately silent — see module docstring.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [slotKey, enabled])
}

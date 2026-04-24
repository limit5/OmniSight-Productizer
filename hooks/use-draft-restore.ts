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
 * Q.6 checkbox 4 layers the conflict toast on top of this by reading
 * `updated_at` against a local-storage cache — out of scope here.
 */
"use client"

import * as React from "react"

import { getUserDraft, type DraftResponse } from "@/lib/api"

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
        onRestoreRef.current(res)
      } catch {
        // Deliberately silent — see module docstring.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [slotKey, enabled])
}

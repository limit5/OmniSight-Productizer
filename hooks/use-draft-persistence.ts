/**
 * Q.6 #300 (2026-04-24, checkbox 1) — debounced draft persistence.
 *
 * Both the INVOKE command bar (`components/omnisight/invoke-core.tsx`)
 * and the workspace chat composer (`components/omnisight/workspace-chat.tsx`)
 * need the same behaviour:
 *
 *   1. Watch a string state.
 *   2. After 500 ms of quiet, PUT it to `/user/drafts/{slot_key}`.
 *   3. Skip the write entirely if the operator is not signed in
 *      (open auth mode: there is no PG row to upsert against).
 *   4. Swallow errors silently — the operator is typing, a toast on
 *      a flaky tunnel would be hostile.
 *
 * Future Q.6 checkboxes (2 = restore on new device, 3 = 24 h GC,
 * 4 = conflict toast on restore) layer on top of this — restore is
 * a one-shot fetch on mount; this hook stays write-only.
 *
 * The hook returns nothing; callers wire it up purely for its side
 * effect on `value` changes. The `enabled` flag is the pull-out
 * lever for tests + open-mode logout: when false the effect is
 * inert (no timer, no fetch).
 */
"use client"

import * as React from "react"

import { putUserDraft } from "@/lib/api"

export const DRAFT_DEBOUNCE_MS = 500

export interface UseDraftPersistenceOptions {
  /** Slot key — `invoke:main` / `chat:main` / `chat:<thread_id>`. */
  slotKey: string
  /** Current composer text (driven by parent state). */
  value: string
  /** Pull-out lever: when false, no debounce timer is scheduled. */
  enabled?: boolean
  /**
   * Optional override for the underlying writer (tests inject a
   * spy; the dispatcher contract is `(slotKey, content) → Promise`).
   * Defaults to `putUserDraft` from `lib/api.ts`.
   */
  writer?: (slotKey: string, content: string) => Promise<unknown>
  /** Override for the debounce window — defaults to 500 ms. */
  debounceMs?: number
}

/**
 * Watch `value` and, after `debounceMs` of quiet, send a PUT to the
 * draft endpoint. The most recent value wins — earlier in-flight
 * writes are cancelled by the timer reset, and any rejection is
 * swallowed (the next typing tick will overwrite it anyway).
 *
 * The hook intentionally does NOT track an "inflight" boolean —
 * concurrent PUTs on the same slot are a no-op for correctness
 * (last-writer-wins per the Q.6 conflict spec) and the network
 * cost is dominated by the operator's own typing cadence, not by
 * the chance of an overlap.
 */
export function useDraftPersistence({
  slotKey,
  value,
  enabled = true,
  writer = putUserDraft,
  debounceMs = DRAFT_DEBOUNCE_MS,
}: UseDraftPersistenceOptions): void {
  // First render is a "load" event from the operator's perspective
  // (the parent might set `value` from local storage before the
  // first human keystroke). Skipping it avoids an immediate-on-mount
  // PUT that would clobber the server-side row with the local cache
  // before the restore flow has a chance to run.
  const isFirstRender = React.useRef<boolean>(true)

  React.useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false
      return
    }
    if (!enabled) return
    if (!slotKey) return
    const timer = setTimeout(() => {
      void writer(slotKey, value).catch(() => {
        // Deliberately silent — the operator is typing, the next
        // tick will retry naturally, and the restore-on-new-device
        // flow tolerates a missed write since it only reads the
        // most recent committed row.
      })
    }, debounceMs)
    return () => clearTimeout(timer)
  }, [slotKey, value, enabled, writer, debounceMs])
}

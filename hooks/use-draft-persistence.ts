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
 * Q.6 checkbox 4 (2026-04-24) adds a final step: on a successful PUT,
 * echo ``{content, updated_at}`` into local storage so the next
 * restore (on this device, new tab, or after a refresh) can compare
 * the server's ``updated_at`` against what this device knows about —
 * if the server is newer the difference must have come from a peer
 * device, and ``useDraftRestore`` fires the "synced from another
 * device" toast.
 *
 * The hook returns nothing; callers wire it up purely for its side
 * effect on `value` changes. The `enabled` flag is the pull-out
 * lever for tests + open-mode logout: when false the effect is
 * inert (no timer, no fetch).
 */
"use client"

import * as React from "react"

import { putUserDraft, type DraftResponse } from "@/lib/api"
import { writeDraftLocalEntry } from "@/lib/draft-sync-bus"

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
  /**
   * When ``true`` (default), successful PUTs echo the returned
   * ``{content, updated_at}`` into local storage so Q.6 checkbox 4's
   * conflict-on-restore comparison has something to compare against.
   * Tests that stub the writer with a non-DraftResponse shape can
   * disable this without wedging on the type guard.
   */
  persistLocalEcho?: boolean
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
  persistLocalEcho = true,
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
      void writer(slotKey, value)
        .then((res) => {
          if (!persistLocalEcho) return
          // Q.6 checkbox 4 — echo server-committed {content, updated_at}
          // so the next restore can tell whether the remote row came
          // from this device or a peer. Defensive typing: we accept
          // any writer override that returns at least
          // ``{content: string, updated_at: number}``; anything else
          // (e.g. unit-test mocks returning ``{}``) is silently
          // skipped so tests do not have to mirror the full shape.
          const r = res as Partial<DraftResponse> | null | undefined
          if (!r) return
          if (typeof r.content !== "string") return
          if (typeof r.updated_at !== "number") return
          writeDraftLocalEntry(slotKey, {
            content: r.content,
            updated_at: r.updated_at,
          })
        })
        .catch(() => {
          // Deliberately silent — the operator is typing, the next
          // tick will retry naturally, and the restore-on-new-device
          // flow tolerates a missed write since it only reads the
          // most recent committed row.
        })
    }, debounceMs)
    return () => clearTimeout(timer)
  }, [slotKey, value, enabled, writer, debounceMs, persistLocalEcho])
}

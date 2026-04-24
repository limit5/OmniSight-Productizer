/**
 * Q.6 #300 (2026-04-24, checkbox 4) — cross-device draft sync bus.
 *
 * Conflict policy: draft is ephemeral; no optimistic lock. Last writer
 * to the server wins (UPSERT under read-committed — see Q.6 checkbox 1
 * spec). The only cross-device awareness happens at restore time:
 * when the composer mounts and the server's ``updated_at`` is newer
 * than the local-storage cache of the last known write for the same
 * slot, another device must have written in the meantime. We then:
 *
 *   1. Adopt the remote content (the local cache is stale).
 *   2. Emit a ``draft_synced`` event on the in-process listener bus so
 *      the ``<DraftSyncToastCenter />`` mounted at the provider root
 *      can surface a toast「從他裝置同步了草稿」.
 *
 * The bus mirrors the ``onApiError`` pattern from ``lib/api.ts``: keep
 * this module callback-only so no React imports leak into the write
 * path, and the toast layer + tests can both subscribe without
 * stubbing fetch / localStorage internals.
 *
 * Module-global audit (per SOP Step 1):
 *   - ``_listeners`` is a per-browser-VM Set, not shared across
 *     workers (browsers are single-thread per-tab); mirrors
 *     ``_apiErrorListeners`` in ``lib/api.ts``.
 *   - localStorage is scoped per-origin and atomic per key, so two
 *     tabs on the same device racing on the same slot is at worst a
 *     harmless clobber (both see the same server row anyway).
 */

/* ─── local storage ─────────────────────────────────────────────── */

const _LS_PREFIX = "omnisight:draft:"

export interface DraftLocalEntry {
  content: string
  updated_at: number
}

function _hasLocalStorage(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined"
}

function _contentKey(slotKey: string): string {
  return `${_LS_PREFIX}${slotKey}:content`
}

function _updatedAtKey(slotKey: string): string {
  return `${_LS_PREFIX}${slotKey}:updated_at`
}

/**
 * Read the last-known-good draft state for a slot from local storage.
 * Returns ``null`` when storage is unavailable (SSR / private mode /
 * quota), when the slot has never been written, or when the stored
 * value fails the schema guard. Never throws.
 */
export function readDraftLocalEntry(slotKey: string): DraftLocalEntry | null {
  if (!_hasLocalStorage()) return null
  if (!slotKey) return null
  try {
    const content = window.localStorage.getItem(_contentKey(slotKey))
    const updatedRaw = window.localStorage.getItem(_updatedAtKey(slotKey))
    if (content === null || updatedRaw === null) return null
    const updated_at = Number.parseFloat(updatedRaw)
    if (!Number.isFinite(updated_at)) return null
    return { content, updated_at }
  } catch {
    return null
  }
}

/**
 * Persist a draft slot's post-write state locally so the next restore
 * knows whether a remote ``updated_at`` is stale or fresh. Writes are
 * atomic per key but the pair (content, updated_at) is not — the
 * restore flow tolerates partial reads by treating any missing half
 * as "no local state".
 */
export function writeDraftLocalEntry(
  slotKey: string,
  entry: DraftLocalEntry,
): void {
  if (!_hasLocalStorage()) return
  if (!slotKey) return
  try {
    window.localStorage.setItem(_contentKey(slotKey), entry.content)
    window.localStorage.setItem(
      _updatedAtKey(slotKey),
      String(entry.updated_at),
    )
  } catch {
    // Quota / Safari private mode — typing must not surface an error.
  }
}

export function clearDraftLocalEntry(slotKey: string): void {
  if (!_hasLocalStorage()) return
  if (!slotKey) return
  try {
    window.localStorage.removeItem(_contentKey(slotKey))
    window.localStorage.removeItem(_updatedAtKey(slotKey))
  } catch {
    /* ignore */
  }
}

/* ─── sync bus ──────────────────────────────────────────────────── */

export interface DraftSyncEvent {
  slotKey: string
  /** The content the remote device committed — already adopted by the composer. */
  content: string
  /** Server-committed timestamp of the remote write. */
  remoteUpdatedAt: number
  /**
   * The local-storage ``updated_at`` we held before the restore, or
   * ``null`` when the slot had never been written locally (e.g. fresh
   * device / cleared storage). ``null`` means "new device restore",
   * and the caller may choose a different toast copy.
   */
  localUpdatedAt: number | null
}

type DraftSyncListener = (event: DraftSyncEvent) => void

const _listeners = new Set<DraftSyncListener>()

/**
 * Subscribe to draft-synced events emitted by ``useDraftRestore``.
 * Returns an unsubscribe. Mirrors the ``onApiError`` bus.
 */
export function onDraftSynced(listener: DraftSyncListener): () => void {
  _listeners.add(listener)
  return () => {
    _listeners.delete(listener)
  }
}

/**
 * Fire a ``draft_synced`` event to every subscriber. Listener
 * exceptions are logged and swallowed so a flaky listener can never
 * starve the others.
 */
export function emitDraftSynced(event: DraftSyncEvent): void {
  for (const l of Array.from(_listeners)) {
    try {
      l(event)
    } catch (e) {
      if (typeof console !== "undefined") {
        console.warn("[onDraftSynced]", e)
      }
    }
  }
}

/**
 * Test helper — drop every subscriber. Never call from production code.
 */
export function _resetDraftSyncListenersForTests(): void {
  _listeners.clear()
}

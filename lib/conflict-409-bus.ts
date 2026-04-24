/**
 * Q.7 #301 — cross-component 409 conflict bus.
 *
 * The J2 ``workflow_runs`` optimistic-lock pattern lived inline in
 * ``run-history-panel.tsx`` ("if msg.includes('409') setConflictMsg"),
 * so every consumer had to re-parse the error string. Q.7 ports the
 * pattern to ``PATCH /tasks/{id}`` / ``PUT /runtime/npi`` /
 * ``PUT /secrets/{id}`` / ``PATCH /projects/runs/{id}``, which made
 * copying the inline check prohibitive.
 *
 * This module provides:
 *   - ``onConflict409(listener) → unsubscribe`` — subscribe to 409
 *     events; ``<Conflict409ToastCenter />`` is the canonical
 *     subscriber, tests subscribe their own to assert behaviour.
 *   - ``emitConflict409(event)`` — emit from the callsite's catch
 *     branch once the caller has classified the error as a 409.
 *   - ``handleConflict409(err, resolution)`` — convenience wrapper:
 *     if ``err`` is an ``ApiError`` with ``kind === 'conflict'``,
 *     parse the shaped detail body (``{current_version,
 *     your_version, hint}``) and emit; otherwise re-throw so the
 *     generic ``ApiErrorToastCenter`` picks it up.
 *
 * Module-global state: one ``Set<listener>`` (per-browser-VM; JS is
 * single-VM-per-tab so there is no cross-worker bleed — mirrors the
 * ``onApiError`` / ``onDraftSynced`` buses).
 */

import type { ApiError } from "@/lib/api"

/** Event emitted to the bus whenever a PATCH / PUT returns 409. */
export interface Conflict409Event {
  /** Unique per-event id — stable across dedupe + dismiss. */
  id: string
  /** Resource label from the server body (``task`` / ``tenant_secret`` / ...). */
  resource: string
  /** Version the server row is currently at (post-winner-commit). */
  currentVersion: number | null
  /** Version the loser client sent in ``If-Match``. */
  yourVersion: number
  /** Operator-facing copy; backend defaults to「另一裝置已修改，請重載」. */
  hint: string
  /**
   * Caller-supplied resolution handlers. ``onReload`` is mandatory —
   * it is the UI default and anchors the Q.7 spec ("預設重載，符合
   * 社群平台多數做法"). ``onOverwrite`` / ``onMerge`` may be omitted
   * when the caller can't safely clobber or has no merge strategy;
   * the toast hides the button for any missing handler.
   */
  onReload: () => void | Promise<void>
  onOverwrite?: () => void | Promise<void>
  onMerge?: () => void | Promise<void>
}

type Conflict409Listener = (event: Conflict409Event) => void
const _listeners = new Set<Conflict409Listener>()

export function onConflict409(listener: Conflict409Listener): () => void {
  _listeners.add(listener)
  return () => {
    _listeners.delete(listener)
  }
}

export function emitConflict409(event: Conflict409Event): void {
  for (const listener of Array.from(_listeners)) {
    try {
      listener(event)
    } catch (err) {
      console.warn("[onConflict409]", err)
    }
  }
}

/** Test helper — wipes subscribers between tests (matches draft-sync-bus). */
export function _resetConflict409ListenersForTests(): void {
  _listeners.clear()
}

function _randomId(): string {
  return Math.random().toString(36).slice(2, 10)
}

/**
 * Shape of the detail body the backend attaches to 409 responses.
 * Produced by ``backend/optimistic_lock.py::raise_conflict``.
 */
interface _ServerConflictDetail {
  current_version?: unknown
  your_version?: unknown
  hint?: unknown
  resource?: unknown
}

function _toNumberOrNull(v: unknown): number | null {
  if (typeof v !== "number") return null
  return Number.isFinite(v) ? v : null
}

function _toNumberOrZero(v: unknown): number {
  if (typeof v !== "number" || !Number.isFinite(v)) return 0
  return v
}

/**
 * Parse the server's ``detail: {...}`` body into the bus event.
 * Returns ``null`` when the shape doesn't match the Q.7 contract —
 * caller re-throws so the generic ApiErrorToastCenter can surface a
 * fallback (protects against third-party 409s that don't follow our
 * schema).
 */
export function parseConflictBody(err: ApiError): {
  resource: string
  currentVersion: number | null
  yourVersion: number
  hint: string
} | null {
  if (err.status !== 409) return null
  const parsed = err.parsed
  if (!parsed) return null
  const detailRaw = (parsed as Record<string, unknown>).detail
  if (!detailRaw || typeof detailRaw !== "object") return null
  const detail = detailRaw as _ServerConflictDetail
  return {
    resource: typeof detail.resource === "string" ? detail.resource : "resource",
    currentVersion: _toNumberOrNull(detail.current_version),
    yourVersion: _toNumberOrZero(detail.your_version),
    hint:
      typeof detail.hint === "string" && detail.hint.length > 0
        ? detail.hint
        : "另一裝置已修改，請重載",
  }
}

export interface Conflict409ResolutionHandlers {
  onReload: () => void | Promise<void>
  onOverwrite?: () => void | Promise<void>
  onMerge?: () => void | Promise<void>
}

/**
 * Catch-branch convenience: call with the caught error + your
 * resolution callbacks. If the error is a 409 with the Q.7 body
 * shape, we emit to the bus and return ``true`` (caller can
 * ``return`` safely). Otherwise returns ``false`` so the caller
 * re-throws / re-branches to its generic path.
 */
export function handleConflict409(
  err: unknown,
  resolution: Conflict409ResolutionHandlers,
): boolean {
  if (!err || typeof err !== "object") return false
  const e = err as ApiError
  if (e.status !== 409) return false
  const parsed = parseConflictBody(e)
  if (!parsed) return false
  emitConflict409({
    id: `conflict-${Date.now()}-${_randomId()}`,
    resource: parsed.resource,
    currentVersion: parsed.currentVersion,
    yourVersion: parsed.yourVersion,
    hint: parsed.hint,
    onReload: resolution.onReload,
    onOverwrite: resolution.onOverwrite,
    onMerge: resolution.onMerge,
  })
  return true
}

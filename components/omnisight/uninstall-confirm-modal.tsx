"use client"

/**
 * BS.8.4 — Per-row uninstall confirm modal with dependency-check gate.
 *
 * Operator entry point for the per-row "Uninstall" overflow action on the
 * Platforms → Installed tab. Where BS.8.2's `<CleanupUnusedModal />`
 * handles a *bulk* idle-scan flow, this row gates the *single-entry*
 * uninstall path: when other installed entries declare the target as a
 * dependency (via `catalog_entries.depends_on`), the modal renders a
 * hard warning listing them and forces an explicit second confirm. When
 * the dependents list is empty, the modal still renders so the operator
 * sees one consistent surface for the destructive action — there is no
 * silent-fall-through path.
 *
 * Why an explicit confirm gate
 * ────────────────────────────
 * Catalog entries can declare `depends_on: ["entry-id", ...]` (alembic
 * 0051's JSONB column). Today an operator who removes a base SDK while
 * a derived workspace toolchain still depends on it produces a broken
 * derived chain — the workspace's next install runs against a missing
 * dependency. The fix isn't to *block* the operator (an admin may
 * legitimately want to force the removal as part of a planned migration);
 * it's to surface the consequences and require an extra click.
 *
 * Caller wiring contract
 * ──────────────────────
 * Purely controlled — the page wrapper passes `entry` (or null), and
 * toggles `open` from a parent useState. On open, the modal fetches
 * `listEntryDependents(entry.id)` once. On confirm, it calls
 * `bulkUninstallEntries([entry.id])` (single-entry batch — backend's
 * existing PEP HOLD path applies; the dependency check is a *frontend*
 * affordance, not a backend gate, so an admin acting via curl still
 * goes through the standard PEP gate without seeing this UI).
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Per-component-instance React state only:
 *   - `dependents` (Array<InstalledEntry> | null) — fetched on open
 *   - `loading` (boolean) — dependents fetch in flight
 *   - `confirmed` (boolean) — operator clicked first confirm; final
 *     submit unlocks (only used when dependents.length > 0)
 *   - `submitting` (boolean) — uninstall round-trip in flight
 *   - `result` / `lastError` — banner state mirroring cleanup modal
 * No module-level mutable state.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Two API round-trips: (1) GET /installer/installed/{id}/dependents on
 * open (read-only — no R-A-W race), (2) POST /installer/uninstall on
 * confirm (commit-before-return — `onCompleted()` triggers the page
 * wrapper's `refreshInstalledEntries()` which sees the post-commit
 * state via PG MVCC). Stale-dependents window: another operator could
 * install a new dependent between (1) and (2); the worst case is the
 * operator confirms on slightly stale data. The PEP coaching card
 * still gates (2), so a destructive proceed-anyway decision is
 * audit-logged either way.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertTriangle, Trash2, Users, Loader2 } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  type InstalledEntry,
} from "@/components/omnisight/installed-tab"
import { installedEntryFromRow } from "@/hooks/use-installed-entries"
import {
  bulkUninstallEntries,
  listEntryDependents,
  type BulkUninstallResponse,
  type ListEntryDependentsResponse,
} from "@/lib/api"

export interface UninstallConfirmModalProps {
  /** ``true`` opens the dialog; toggles to ``false`` close it. The
   *  controlled-open pattern matches the cleanup-unused modal. */
  open: boolean
  /** The entry the operator wants to remove. ``null`` is a noop — the
   *  modal renders nothing until the page wrapper passes a non-null
   *  entry alongside ``open=true``. */
  entry: InstalledEntry | null
  /** Fired when the operator dismisses the modal (Esc, overlay click,
   *  Close button, or after a successful uninstall keeps modal open
   *  until the operator clicks Close). */
  onClose: () => void
  /** Optional override for the dependents fetch — tests inject a stub
   *  to avoid mocking global ``fetch``. Defaults to
   *  :func:`listEntryDependents`. */
  onFetchDependents?: (entryId: string) => Promise<ListEntryDependentsResponse>
  /** Optional override for the uninstall submit — tests inject a stub
   *  to assert the call shape. Defaults to a single-entry
   *  :func:`bulkUninstallEntries` invocation. */
  onUninstallConfirmed?: (
    entryId: string,
  ) => Promise<BulkUninstallResponse>
  /** Optional callback fired AFTER a successful uninstall — page
   *  wrappers use this to refresh ``useInstalledEntries()``. */
  onCompleted?: (result: BulkUninstallResponse) => void
}

interface ResultBanner {
  approvedCount: number
  deniedCount: number
}

export function UninstallConfirmModal({
  open,
  entry,
  onClose,
  onFetchDependents,
  onUninstallConfirmed,
  onCompleted,
}: UninstallConfirmModalProps) {
  const [dependents, setDependents] = useState<InstalledEntry[] | null>(null)
  const [loading, setLoading] = useState<boolean>(false)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState<boolean>(false)
  const [submitting, setSubmitting] = useState<boolean>(false)
  const [result, setResult] = useState<ResultBanner | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  // Fetch dependents whenever the modal opens for a new entry. The
  // useRef-guarded setState pattern matches `useInstalledEntries` so
  // a fast close-then-reopen cannot leak stale state into the next view.
  useEffect(() => {
    if (!open || !entry) return
    let cancelled = false
    setDependents(null)
    setFetchError(null)
    setConfirmed(false)
    setResult(null)
    setLastError(null)
    setLoading(true)
    const fetcher = onFetchDependents ?? listEntryDependents
    void (async () => {
      try {
        const res = await fetcher(entry.id)
        if (cancelled || !mountedRef.current) return
        const items = (res.items ?? []).map(installedEntryFromRow)
        setDependents(items)
        setLoading(false)
      } catch (err) {
        if (cancelled || !mountedRef.current) return
        const message =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "failed to load dependents"
        setFetchError(message)
        setDependents([])
        setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, entry, onFetchDependents])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) {
        // Reset transient state so a re-open starts clean.
        setDependents(null)
        setFetchError(null)
        setConfirmed(false)
        setResult(null)
        setLastError(null)
        onClose()
      }
    },
    [onClose],
  )

  // The "two-step" gate: when there ARE dependents, the operator must
  // first acknowledge the warning (sets `confirmed=true`) and then click
  // the destructive submit. When there are no dependents (or the fetch
  // is still pending) the second-step button is the primary action.
  const hasDependents = (dependents?.length ?? 0) > 0
  const dependentsKnown = dependents !== null && !loading
  const needsExplicitConfirm = hasDependents && !confirmed

  const handleAcknowledge = useCallback(() => {
    setConfirmed(true)
  }, [])

  const handleSubmit = useCallback(async () => {
    if (!entry || submitting) return
    if (needsExplicitConfirm) return  // first click on a dependents-warning modal
    setSubmitting(true)
    setLastError(null)
    try {
      const fn =
        onUninstallConfirmed ??
        ((id: string) => bulkUninstallEntries([id]))
      const res = await fn(entry.id)
      setResult({
        approvedCount: res.approved_count,
        deniedCount: res.denied_count,
      })
      // Clear the confirmed flag so a re-open of the same entry forces
      // the operator to re-confirm; the modal stays open until the
      // operator clicks Close so they see the result banner.
      setConfirmed(false)
      if (onCompleted) onCompleted(res)
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : typeof err === "string"
            ? err
            : "uninstall failed"
      setLastError(message)
    } finally {
      setSubmitting(false)
    }
  }, [entry, submitting, needsExplicitConfirm, onUninstallConfirmed, onCompleted])

  const dependentCount = dependents?.length ?? 0

  // Pure derivation — exposes a stable testid count for BS.8.7's row
  // contract test even when the fetch is pending.
  const dataState = useMemo(() => {
    if (loading) return "loading"
    if (fetchError) return "fetch-error"
    if (!dependentsKnown) return "idle"
    if (hasDependents) return "has-dependents"
    return "no-dependents"
  }, [loading, fetchError, dependentsKnown, hasDependents])

  return (
    <Dialog open={open && entry !== null} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="uninstall-confirm-modal"
        data-entry-id={entry?.id ?? ""}
        data-dependent-count={dependentCount}
        data-data-state={dataState}
        data-confirmed={confirmed ? "true" : "false"}
        className="max-w-xl"
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="uninstall-confirm-modal-title"
          >
            <Trash2
              size={14}
              className="text-[var(--critical-red)]"
              aria-hidden
            />
            <span>Uninstall {entry?.displayName ?? "entry"}</span>
          </DialogTitle>
          <DialogDescription
            className="font-mono text-[11px] text-[var(--muted-foreground)]"
            data-testid="uninstall-confirm-modal-description"
          >
            {entry?.vendor}
            {entry?.version ? ` · v${entry.version}` : ""}
            {" — "}
            Removal still runs through the standard PEP gate. Operator
            approval is recorded in the audit log.
          </DialogDescription>
        </DialogHeader>

        {/* ── Dependents section ──────────────────────────────────── */}
        {loading ? (
          <div
            className="flex items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--card)]/40 px-3 py-3 font-mono text-[11px] text-[var(--muted-foreground)]"
            data-testid="uninstall-confirm-modal-loading"
          >
            <Loader2 size={12} aria-hidden className="animate-spin" />
            Checking dependents…
          </div>
        ) : fetchError !== null ? (
          <div
            className="flex items-start gap-2 rounded border border-amber-500/45 bg-amber-500/5 px-3 py-2 font-mono text-[11px] text-amber-300"
            data-testid="uninstall-confirm-modal-fetch-error"
          >
            <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
            <span>
              Could not load dependents — {fetchError}. Proceed with extra
              caution.
            </span>
          </div>
        ) : hasDependents ? (
          <div
            className="flex flex-col gap-2 rounded border border-[var(--critical-red)]/55 bg-[var(--critical-red)]/5 px-3 py-3"
            data-testid="uninstall-confirm-modal-dependents-warning"
          >
            <div className="flex items-center gap-2 font-mono text-xs text-[var(--critical-red)]">
              <AlertTriangle size={14} aria-hidden />
              <span data-testid="uninstall-confirm-modal-dependents-headline">
                {dependentCount} other installed{" "}
                {dependentCount === 1 ? "entry depends" : "entries depend"} on
                this
              </span>
            </div>
            <ul
              className="flex max-h-[200px] flex-col divide-y divide-[var(--border)] overflow-y-auto rounded-md border border-[var(--border)] bg-[var(--card)]"
              data-testid="uninstall-confirm-modal-dependents-list"
            >
              {dependents!.map((dep) => (
                <li
                  key={dep.id}
                  data-testid={`uninstall-confirm-modal-dependent-${dep.id}`}
                  data-dependent-id={dep.id}
                  className="flex items-center gap-2 px-3 py-2"
                >
                  <Users
                    size={12}
                    aria-hidden
                    className="shrink-0 text-[var(--muted-foreground)]"
                  />
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span
                      className="truncate font-orbitron text-xs tracking-wide text-[var(--foreground)]"
                      title={dep.displayName}
                    >
                      {dep.displayName}
                    </span>
                    <span className="truncate font-mono text-[10px] text-[var(--muted-foreground)]">
                      {dep.vendor}
                      {dep.version ? ` · v${dep.version}` : ""}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
            <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
              Removing this entry will leave the listed{" "}
              {dependentCount === 1 ? "entry" : "entries"} pointing at a
              missing dependency. Click{" "}
              <span className="text-[var(--critical-red)]">
                I understand
              </span>{" "}
              to acknowledge before sending the uninstall job.
            </p>
          </div>
        ) : dependentsKnown ? (
          <div
            className="rounded border border-emerald-500/40 bg-emerald-500/5 px-3 py-2 font-mono text-[11px] text-emerald-300"
            data-testid="uninstall-confirm-modal-no-dependents"
          >
            No other installed entries depend on this one. Safe to proceed.
          </div>
        ) : null}

        {/* ── Result + inline error banners ─────────────────────── */}
        {result !== null && (
          <div
            className="rounded border border-emerald-500/40 bg-emerald-500/5 px-3 py-2 font-mono text-[11px] text-emerald-300"
            data-testid="uninstall-confirm-modal-result"
          >
            Uninstalled {result.approvedCount}; rejected {result.deniedCount}.
          </div>
        )}
        {lastError !== null && (
          <div
            className="flex items-start gap-2 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-3 py-2 font-mono text-[11px] text-[var(--critical-red)]"
            data-testid="uninstall-confirm-modal-error"
          >
            <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
            <span>{lastError}</span>
          </div>
        )}

        <DialogFooter className="gap-2">
          <button
            type="button"
            onClick={() => handleOpenChange(false)}
            disabled={submitting}
            className="inline-flex items-center justify-center rounded border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="uninstall-confirm-modal-close"
          >
            {result === null ? "Cancel" : "Close"}
          </button>
          {needsExplicitConfirm ? (
            <button
              type="button"
              onClick={handleAcknowledge}
              disabled={submitting}
              className="inline-flex items-center justify-center gap-1 rounded border border-amber-500/55 bg-amber-500/10 px-3 py-1.5 font-mono text-xs text-amber-300 hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="uninstall-confirm-modal-acknowledge"
            >
              <AlertTriangle size={12} aria-hidden />
              I understand
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={submitting || loading || result !== null || !entry}
              className="inline-flex items-center justify-center gap-1 rounded bg-[var(--critical-red)]/15 px-3 py-1.5 font-mono text-xs text-[var(--critical-red)] ring-1 ring-[var(--critical-red)]/55 hover:bg-[var(--critical-red)]/25 disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="uninstall-confirm-modal-confirm"
            >
              <Trash2 size={12} aria-hidden />
              {submitting
                ? "Uninstalling…"
                : result !== null
                  ? "Done"
                  : "Uninstall"}
            </button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default UninstallConfirmModal

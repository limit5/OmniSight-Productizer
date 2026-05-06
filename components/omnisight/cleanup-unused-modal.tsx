"use client"

/**
 * BS.8.2 — Cleanup-unused modal.
 *
 * Operator entry point for the "30-day idle" cleanup flow on the
 * Platforms → Installed tab. The modal renders a candidate list
 * derived from the current installed entries via :func:`isCleanupCandidate`
 * (lives in `lib/api.ts`), lets the operator multi-select via per-row
 * checkboxes (plus a "Select all" master toggle), and on click of
 * "Uninstall N selected" calls `bulkUninstallEntries(entryIds)` —
 * which fires ONE PEP HOLD covering every entry in the batch.
 *
 * Why "still goes through PEP" matters
 * ────────────────────────────────────
 * The TODO row's literal "仍走 PEP" reminder is critical: the bulk
 * cleanup must NOT bypass the PEP gateway. The frontend POSTs every
 * entry id to `/installer/uninstall` with `tool="uninstall_entry"`,
 * which lands in the gateway's `tier_unlisted` HOLD branch (no
 * whitelist match) and therefore raises a Decision Engine proposal
 * the operator must approve via the global ToastCenter coaching card
 * before any row gets inserted with state='completed'. PEP-deny → 403
 * surfaced through `<ApiErrorToastCenter />`; the modal stays open so
 * the operator sees that nothing was uninstalled.
 *
 * Caller wiring contract
 * ──────────────────────
 * Purely presentational with respect to the candidate filter — the
 * page wrapper passes the full `entries` list (typically from
 * `useInstalledEntries()`); the modal applies `isCleanupCandidate`
 * itself so the toolbar's "Cleanup unused (N)" badge stays in sync
 * with what the modal renders. The `onUninstallSelected` callback
 * receives the selected entry ids, performs the network call, and
 * resolves with a result so the modal can render a "Uninstalled X /
 * rejected Y" summary.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Per-component-instance React state only:
 *   - `selected` (Set<string>) — checked entry ids
 *   - `submitting` (boolean) — disables buttons during the round-trip
 *   - `result` (object | null) — last batch outcome banner
 *   - `lastError` (string | null) — surfaces 403/422 message inline
 * No module-level mutable state, no in-memory cache. Rendering is
 * browser-only — uvicorn `--workers N` model does not apply (every
 * tab derives the same view from the same REST snapshot the page
 * wrapper passes in).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * The bulk-uninstall round-trip is single-threaded inside the
 * component's submit handler. After resolution we call `onCompleted()`
 * (when wired) so the page wrapper can refresh its
 * `useInstalledEntries()` snapshot — that fresh GET sees the just-
 * inserted uninstall rows by PG MVCC, so the candidates that were
 * just uninstalled disappear from the modal on the next refresh.
 */

import { useCallback, useMemo, useState } from "react"
import { CheckSquare, Square, Trash2, AlertTriangle } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  formatRelativeDuration,
  type InstalledEntry,
} from "@/components/omnisight/installed-tab"
import { formatInstallBytes } from "@/components/omnisight/install-progress-drawer"
import {
  INSTALLED_ENTRY_IDLE_THRESHOLD_MS,
  bulkUninstallEntries,
  isCleanupCandidate,
  type BulkUninstallResponse,
} from "@/lib/api"

/** 30 days, expressed as days for the human-readable copy. */
const IDLE_DAYS_THRESHOLD = Math.round(
  INSTALLED_ENTRY_IDLE_THRESHOLD_MS / (24 * 60 * 60 * 1000),
)

export interface CleanupUnusedModalProps {
  /** ``true`` opens the dialog; toggles to ``false`` close it. The
   *  controlled-open pattern matches `<InstallLogModal />`. */
  open: boolean
  /** All currently-installed entries (typically the
   *  `useInstalledEntries()` snapshot). The modal filters internally
   *  to surface only 30-day idle candidates. */
  entries: ReadonlyArray<InstalledEntry>
  /** Optional clock override — tests pin "now" so the candidate
   *  filter is deterministic. Defaults to a fresh `new Date()` each
   *  render. */
  now?: Date
  /** Fired when the operator dismisses the modal (Esc, overlay click,
   *  Close button, or after a successful uninstall). */
  onClose: () => void
  /** Custom uninstall handler. When omitted (the default), the modal
   *  calls :func:`bulkUninstallEntries` directly; tests inject a stub
   *  to assert the call shape without mocking global ``fetch``. */
  onUninstallSelected?: (
    entryIds: ReadonlyArray<string>,
  ) => Promise<BulkUninstallResponse>
  /** Optional callback fired AFTER a successful uninstall — page
   *  wrappers use this to refresh `useInstalledEntries()` so the just-
   *  uninstalled rows fall out of the modal on the next render. */
  onCompleted?: (result: BulkUninstallResponse) => void
}

interface ResultBanner {
  approvedCount: number
  deniedCount: number
}

/** Pure helper — pick the cleanup candidates from a snapshot. Exported
 *  so the toolbar badge in `app/settings/platforms/page.tsx` can reuse
 *  the same predicate the modal applies. */
export function pickCleanupCandidates(
  entries: ReadonlyArray<InstalledEntry>,
  now: Date = new Date(),
): InstalledEntry[] {
  return entries.filter((e) =>
    isCleanupCandidate(
      {
        lastUsedAt: e.lastUsedAt ?? null,
        installedAt: e.installedAt ?? null,
        usedByWorkspaceCount: e.usedByWorkspaceCount,
      },
      now,
    ),
  )
}

export function CleanupUnusedModal({
  open,
  entries,
  now,
  onClose,
  onUninstallSelected,
  onCompleted,
}: CleanupUnusedModalProps) {
  const candidates = useMemo(
    () => pickCleanupCandidates(entries, now),
    [entries, now],
  )

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<ResultBanner | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) {
        // Reset transient state so a re-open starts clean.
        setSelected(new Set())
        setResult(null)
        setLastError(null)
        onClose()
      }
    },
    [onClose],
  )

  const toggleEntry = useCallback((entryId: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(entryId)) {
        next.delete(entryId)
      } else {
        next.add(entryId)
      }
      return next
    })
  }, [])

  const allCandidateIds = useMemo(
    () => candidates.map((c) => c.id),
    [candidates],
  )
  const allSelected =
    candidates.length > 0 &&
    allCandidateIds.every((id) => selected.has(id))
  const noneSelected = selected.size === 0

  const toggleAll = useCallback(() => {
    setSelected((prev) => {
      // If everything is already selected, clear the selection;
      // otherwise add every visible candidate id.
      const allHere = allCandidateIds.every((id) => prev.has(id))
      if (allHere) return new Set()
      return new Set(allCandidateIds)
    })
  }, [allCandidateIds])

  const handleConfirm = useCallback(async () => {
    if (noneSelected || submitting) return
    const ids = Array.from(selected)
    setSubmitting(true)
    setLastError(null)
    try {
      const fn = onUninstallSelected ?? bulkUninstallEntries
      const res = await fn(ids)
      setResult({
        approvedCount: res.approved_count,
        deniedCount: res.denied_count,
      })
      // Clear the selection so the operator does not re-fire the same
      // batch by accident; the candidates list refreshes via
      // onCompleted() → page wrapper's hook refresh.
      setSelected(new Set())
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
  }, [noneSelected, submitting, selected, onUninstallSelected, onCompleted])

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="cleanup-unused-modal"
        data-candidate-count={candidates.length}
        data-selected-count={selected.size}
        className="max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="cleanup-unused-modal-title"
          >
            <Trash2
              size={14}
              className="text-[var(--muted-foreground)]"
              aria-hidden
            />
            <span>Cleanup unused</span>
            <span
              className="ml-auto rounded border border-[var(--border)] bg-[var(--muted)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)]"
              data-testid="cleanup-unused-modal-count"
            >
              {candidates.length} idle ≥ {IDLE_DAYS_THRESHOLD}d
            </span>
          </DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-[var(--muted-foreground)]">
            Entries with no workspace activity for {IDLE_DAYS_THRESHOLD} days+
            and zero current dependants. Bulk uninstall still runs through the
            standard PEP gate — operator approval required.
          </DialogDescription>
        </DialogHeader>

        {/* ── Empty state ─────────────────────────────────────────── */}
        {candidates.length === 0 ? (
          <div
            className="flex min-h-[120px] items-center justify-center rounded-md border border-dashed border-[var(--border)] bg-[var(--card)]/30 p-6 font-mono text-xs text-[var(--muted-foreground)]"
            data-testid="cleanup-unused-modal-empty"
          >
            No idle entries to clean up — every installed entry was used
            within the last {IDLE_DAYS_THRESHOLD} days or has active
            workspace dependants.
          </div>
        ) : (
          <>
            {/* Master toolbar */}
            <div
              className="flex items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--card)]/40 px-3 py-2"
              data-testid="cleanup-unused-modal-toolbar"
            >
              <button
                type="button"
                onClick={toggleAll}
                className="inline-flex items-center gap-2 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] focus:outline-none focus-visible:outline-none focus:ring-2 focus-visible:ring-2 focus:ring-[var(--neural-blue)] focus-visible:ring-[var(--neural-blue)]"
                data-testid="cleanup-unused-modal-toggle-all"
                aria-pressed={allSelected}
                aria-label={
                  allSelected ? "Deselect all candidates" : "Select all candidates"
                }
              >
                {allSelected ? (
                  <CheckSquare size={14} aria-hidden />
                ) : (
                  <Square size={14} aria-hidden />
                )}
                {allSelected ? "Deselect all" : "Select all"}
              </button>
              <span className="ml-auto font-mono text-[10px] text-[var(--muted-foreground)]">
                {selected.size} / {candidates.length} selected
              </span>
            </div>

            {/* Candidate list */}
            <ul
              className="flex max-h-[320px] flex-col divide-y divide-[var(--border)] overflow-y-auto rounded-md border border-[var(--border)] bg-[var(--card)]"
              data-testid="cleanup-unused-modal-list"
            >
              {candidates.map((entry) => {
                const isChecked = selected.has(entry.id)
                const lastUsedLabel = formatRelativeDuration(
                  entry.lastUsedAt ?? null,
                  now,
                )
                const installedLabel = formatRelativeDuration(
                  entry.installedAt ?? null,
                  now,
                )
                const idleLabel =
                  entry.lastUsedAt
                    ? `Last used ${lastUsedLabel}`
                    : `Installed ${installedLabel}, never used`
                return (
                  <li
                    key={entry.id}
                    data-testid={`cleanup-unused-modal-row-${entry.id}`}
                    data-selected={isChecked ? "true" : "false"}
                    className="flex items-center gap-3 px-3 py-2"
                  >
                    <button
                      type="button"
                      onClick={() => toggleEntry(entry.id)}
                      className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded border border-[var(--border)] text-[var(--muted-foreground)] hover:text-[var(--foreground)] focus:outline-none focus-visible:outline-none focus:ring-2 focus-visible:ring-2 focus:ring-[var(--neural-blue)] focus-visible:ring-[var(--neural-blue)]"
                      data-testid={`cleanup-unused-modal-checkbox-${entry.id}`}
                      aria-pressed={isChecked}
                      aria-label={
                        isChecked
                          ? `Deselect ${entry.displayName}`
                          : `Select ${entry.displayName}`
                      }
                    >
                      {isChecked ? <CheckSquare size={12} /> : <Square size={12} />}
                    </button>

                    <div className="flex min-w-0 flex-1 flex-col">
                      <span
                        className="truncate font-orbitron text-xs tracking-wide text-[var(--foreground)]"
                        title={entry.displayName}
                      >
                        {entry.displayName}
                      </span>
                      <span className="truncate font-mono text-[10px] text-[var(--muted-foreground)]">
                        {entry.vendor}
                        {entry.version ? ` · v${entry.version}` : ""}
                        {" · "}
                        {idleLabel}
                      </span>
                    </div>

                    <span
                      className="shrink-0 font-mono text-[10px] tabular-nums text-[var(--muted-foreground)]"
                      data-testid={`cleanup-unused-modal-disk-${entry.id}`}
                    >
                      {formatInstallBytes(entry.diskUsageBytes ?? null)}
                    </span>
                  </li>
                )
              })}
            </ul>
          </>
        )}

        {/* ── Result banner / inline error ────────────────────────── */}
        {result !== null && (
          <div
            className="rounded border border-emerald-500/40 bg-emerald-500/5 px-3 py-2 font-mono text-[11px] text-emerald-300"
            data-testid="cleanup-unused-modal-result"
          >
            Uninstalled {result.approvedCount}; rejected {result.deniedCount}.
          </div>
        )}
        {lastError !== null && (
          <div
            className="flex items-start gap-2 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-3 py-2 font-mono text-[11px] text-[var(--critical-red)]"
            data-testid="cleanup-unused-modal-error"
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
            data-testid="cleanup-unused-modal-close"
          >
            Close
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting || noneSelected}
            className="inline-flex items-center justify-center gap-1 rounded bg-[var(--critical-red)]/15 px-3 py-1.5 font-mono text-xs text-[var(--critical-red)] ring-1 ring-[var(--critical-red)]/55 hover:bg-[var(--critical-red)]/25 disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="cleanup-unused-modal-confirm"
          >
            <Trash2 size={12} aria-hidden />
            {submitting
              ? "Uninstalling…"
              : noneSelected
                ? "Uninstall selected"
                : `Uninstall ${selected.size} selected`}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default CleanupUnusedModal

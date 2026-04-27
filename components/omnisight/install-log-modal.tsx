"use client"

/**
 * BS.7.6 — Install log modal.
 *
 * Surfaces an install_jobs row's tail log + error_reason when the
 * operator clicks the "log" / "View log" button on a failed catalog
 * card or detail panel. Pairs with the retry button (also wired in
 * BS.7.6) so the post-mortem read-and-retry flow lives in one place.
 *
 * Caller wiring contract
 * ──────────────────────
 * The modal is **purely presentational** — it does not subscribe to
 * SSE or fetch by itself. The page wrapper picks the freshest
 * InstallJob row for the selected entry from ``useInstallJobs()`` and
 * passes it via the ``job`` prop. When ``job`` is ``null`` the modal
 * stays closed (rather than rendering a blank dialog), matching the
 * Dialog convention of "controlled open via prop presence".
 *
 * For pages that load mid-install when the SSE stream has not yet
 * filled the local snapshot, callers may pre-fetch a single row via
 * ``getInstallJob(jobId)`` and pass it in here; the modal does not
 * refetch on its own to keep the surface row-level and avoid coupling
 * to the SSE hook.
 *
 * State pill / error reason / log tail
 * ────────────────────────────────────
 *   • State pill mirrors the catalog-card 5-state palette so the
 *     operator does not have to translate between surfaces.
 *   • ``error_reason`` is shown as a single-line summary above the
 *     log, prefixed with the same critical-red colour the card uses
 *     for state 5; null reason reads as "(no reason recorded)".
 *   • ``log_tail`` renders inside a fixed-height scrollable monospace
 *     box so a 4 KiB tail does not blow out the dialog. The "Copy"
 *     button copies the raw tail to the clipboard for paste-into-
 *     ticket workflows.
 *
 * Module-global state audit
 * ─────────────────────────
 * Pure per-component-instance state (``copied`` flag for the 1.5 s
 * "Copied!" hint). No module-level mutable state, no in-memory cache,
 * no thread-locals. Rendering is browser-only — uvicorn ``--workers
 * N`` model does not apply.
 *
 * Read-after-write timing
 * ───────────────────────
 * N/A — pure presentation. The clipboard write is fire-and-forget
 * inside a single render thread; the success / failure toggle is local
 * state only.
 */

import { useCallback, useEffect, useState } from "react"
import { AlertOctagon, CheckCircle2, Copy, RefreshCw } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { InstallJob } from "@/lib/api"

/** Empty / null log_tail placeholder — operator-friendly hint that
 *  the sidecar has not yet emitted any progress with a log payload. */
export const INSTALL_LOG_EMPTY_PLACEHOLDER = "(no log output yet)"

/** Null error_reason placeholder — the row failed but the sidecar /
 *  install method did not record a reason (rare; usually only on a
 *  cancelled-via-PEP-deny row before the sidecar claims it). */
export const INSTALL_LOG_NO_REASON_PLACEHOLDER = "(no reason recorded)"

/** State → human label, mirroring backend lifecycle. Exported for
 *  test contract lock. */
export const INSTALL_LOG_STATE_LABELS: Record<InstallJob["state"], string> = {
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
}

export interface InstallLogModalProps {
  /** The install job row to display. ``null`` keeps the modal closed
   *  (do not render an empty dialog body). */
  job: InstallJob | null
  /** Optional human display name for the catalog entry the job belongs
   *  to. Falls back to the row's ``entry_id`` when omitted (matches
   *  the install-progress-drawer fallback chain). */
  entryDisplayName?: string
  /** Fired when the operator dismisses the modal (Esc, overlay click,
   *  Close button). Page wrapper clears the "viewing" state here. */
  onClose: () => void
  /** Optional retry handler — when wired the modal renders a "Retry
   *  install" button in the footer that calls ``onRetry(job)`` and
   *  closes the modal. Page wrapper passes the same handler the card
   *  uses, so the retry path is identical from either surface. */
  onRetry?: (job: InstallJob) => void
  /** Disable the retry button (e.g. while a previous retry POST is
   *  still in flight). The button still renders so the affordance is
   *  visible. */
  retryDisabled?: boolean
  /** Optional clipboard override — tests inject a stub since jsdom
   *  does not provide a fully spec-compliant ``navigator.clipboard``. */
  copyToClipboard?: (text: string) => Promise<void>
}

const COPIED_TIMEOUT_MS = 1500

/** Fall back to entry_id when no display name is supplied. */
function resolveEntryLabel(job: InstallJob, override?: string): string {
  if (override && override.trim().length > 0) return override
  return job.entry_id || job.id
}

async function defaultCopy(text: string): Promise<void> {
  if (typeof navigator === "undefined" || !navigator.clipboard) {
    throw new Error("clipboard unavailable")
  }
  await navigator.clipboard.writeText(text)
}

export function InstallLogModal({
  job,
  entryDisplayName,
  onClose,
  onRetry,
  retryDisabled,
  copyToClipboard,
}: InstallLogModalProps) {
  const [copied, setCopied] = useState(false)
  const open = job !== null

  // Reset the "Copied!" badge whenever the modal opens / switches to a
  // different job so a stale flash does not bleed into the next view.
  useEffect(() => {
    if (!open) setCopied(false)
  }, [open, job?.id])

  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) onClose()
    },
    [onClose],
  )

  const handleCopy = useCallback(async () => {
    if (!job) return
    const writer = copyToClipboard ?? defaultCopy
    try {
      await writer(job.log_tail ?? "")
      setCopied(true)
      setTimeout(() => setCopied(false), COPIED_TIMEOUT_MS)
    } catch {
      // Clipboard failures are silent — the operator can still
      // select-and-copy the log text manually from the modal body.
    }
  }, [job, copyToClipboard])

  const handleRetry = useCallback(() => {
    if (!job || !onRetry) return
    onRetry(job)
    onClose()
  }, [job, onRetry, onClose])

  if (!job) return null

  const entryLabel = resolveEntryLabel(job, entryDisplayName)
  const stateLabel = INSTALL_LOG_STATE_LABELS[job.state]
  const reason = job.error_reason && job.error_reason.length > 0
    ? job.error_reason
    : INSTALL_LOG_NO_REASON_PLACEHOLDER
  const logBody = job.log_tail && job.log_tail.length > 0
    ? job.log_tail
    : INSTALL_LOG_EMPTY_PLACEHOLDER
  const isFailed = job.state === "failed"

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        data-testid="install-log-modal"
        data-state-label={stateLabel}
        className="max-w-2xl"
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="install-log-modal-title"
          >
            {isFailed ? (
              <AlertOctagon
                size={14}
                className="text-[var(--critical-red)]"
                aria-hidden
              />
            ) : null}
            <span>{entryLabel}</span>
            <span
              className={[
                "ml-auto rounded border px-1.5 py-0.5 font-mono text-[10px]",
                isFailed
                  ? "border-[var(--critical-red)]/55 bg-[var(--critical-red)]/10 text-[var(--critical-red)]"
                  : "border-[var(--border)] bg-[var(--muted)] text-[var(--muted-foreground)]",
              ].join(" ")}
              data-testid="install-log-modal-state"
            >
              {stateLabel}
            </span>
          </DialogTitle>
          <DialogDescription
            className="font-mono text-[11px] text-[var(--muted-foreground)]"
            data-testid="install-log-modal-job-id"
          >
            install job · {job.id}
          </DialogDescription>
        </DialogHeader>

        {isFailed && (
          <div
            className="rounded border border-[var(--critical-red)]/40 bg-[var(--critical-red)]/5 px-3 py-2 font-mono text-[11px] text-[var(--critical-red)]"
            data-testid="install-log-modal-error-reason"
          >
            {reason}
          </div>
        )}

        <div className="flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
            log_tail
          </span>
          <button
            type="button"
            onClick={handleCopy}
            disabled={
              !job.log_tail || job.log_tail.length === 0
            }
            className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-2 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
            data-testid="install-log-modal-copy"
            aria-label="Copy log tail to clipboard"
          >
            {copied ? (
              <>
                <CheckCircle2 size={10} />
                Copied
              </>
            ) : (
              <>
                <Copy size={10} />
                Copy
              </>
            )}
          </button>
        </div>

        <pre
          className="max-h-[16rem] overflow-auto whitespace-pre-wrap break-words rounded border border-[var(--border)] bg-[var(--muted)]/30 p-3 font-mono text-[11px] leading-snug text-[var(--foreground)]"
          data-testid="install-log-modal-log-body"
        >
          {logBody}
        </pre>

        <DialogFooter>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-2 py-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            data-testid="install-log-modal-close"
          >
            Close
          </button>
          {onRetry ? (
            <button
              type="button"
              onClick={handleRetry}
              disabled={retryDisabled}
              className="inline-flex items-center gap-1 rounded border border-[var(--critical-red)]/55 px-2 py-1 font-mono text-[10px] text-[var(--critical-red)] hover:bg-[var(--critical-red)]/10 disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="install-log-modal-retry"
            >
              <RefreshCw size={10} />
              Retry install
            </button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default InstallLogModal

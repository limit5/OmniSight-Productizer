"use client"

/**
 * BS.8.5 — Sources tab (admin only).
 *
 * The third sub-tab on Settings → Platforms. Renders the per-tenant list
 * of catalog feed subscriptions (rows in `catalog_subscriptions`) and
 * exposes admin-only CRUD over them:
 *   • Add a new subscription via an inline form (URL + auth method +
 *     refresh interval).
 *   • Per-row "Sync now" button (POST /catalog/sources/{id}/sync —
 *     stamps the row to be picked up by the feed worker on the next
 *     tick).
 *   • Per-row "Remove" button (DELETE /catalog/sources/{id}, with a
 *     small inline confirm overlay so a misclick doesn't blow away a
 *     subscription).
 *
 * Why purely presentational + caller-supplied callbacks
 * ─────────────────────────────────────────────────────
 * The page wrapper owns the data flow (`useCatalogSources()` + a
 * round-trip after each mutation triggers refresh). This component is
 * deliberately UI-only so:
 *   1. Tests can assert behaviour without spinning a real network mock —
 *      they pass `vi.fn()` callbacks and inspect the calls.
 *   2. Future auth gates (e.g. enable / disable based on tenant flags)
 *      live in the page wrapper, not deep inside the component tree.
 *   3. The catch-all `<ApiErrorToastCenter />` already surfaces 403 /
 *      409 / 422; the modal still renders an inline error banner so
 *      the operator sees the cause without leaving the form context.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Per-component-instance React state only:
 *   - `formOpen` / `formFields` / `formError` — add-source form state
 *   - `submitting` / `lastError` — round-trip status
 *   - `pendingDeleteId` / `pendingSyncId` — per-row in-flight markers
 *   - `lastSyncId` / `lastSyncStatus` — fades after the next render
 * No module-level mutable state, no in-memory cache. Browser-only —
 * cross-worker / multi-tab consistency comes from the backend reading
 * from PG; each tab refreshes its own snapshot.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Mutations resolve (commit happens before HTTP 200 returns); the
 * page wrapper's `onChanged` callback fires `refreshSources()`. The
 * fresh GET sees the post-commit row via PG MVCC, so the table re-
 * renders with the new state. There is no shared in-memory cache to
 * lag.
 */

import { useCallback, useMemo, useState } from "react"
import {
  AlertTriangle,
  Loader2,
  Plus,
  RefreshCw,
  Rss,
  Trash2,
  X,
} from "lucide-react"

import {
  CATALOG_SOURCE_AUTH_METHODS,
  CATALOG_SOURCE_REFRESH_DEFAULT_S,
  CATALOG_SOURCE_REFRESH_MAX_S,
  CATALOG_SOURCE_REFRESH_MIN_S,
  validateCatalogSourceAuthSecretRef,
  validateCatalogSourceFeedUrl,
  validateCatalogSourceRefreshInterval,
  type CatalogSource,
  type CatalogSourceAuthMethod,
} from "@/lib/api"

const AUTH_METHOD_LABEL: Record<CatalogSourceAuthMethod, string> = {
  none: "None",
  basic: "Basic auth",
  bearer: "Bearer token",
  signed_url: "Signed URL",
}

const SYNC_STATUS_TONE: Record<string, "ok" | "warn" | "info"> = {
  ok: "ok",
  success: "ok",
  pending_manual: "info",
  pending: "info",
  failed: "warn",
  error: "warn",
}

const REFRESH_PRESETS: ReadonlyArray<{ label: string; seconds: number }> = [
  { label: "1 hour", seconds: 60 * 60 },
  { label: "6 hours", seconds: 6 * 60 * 60 },
  { label: "24 hours", seconds: 24 * 60 * 60 },
  { label: "7 days", seconds: 7 * 24 * 60 * 60 },
  { label: "30 days", seconds: 30 * 24 * 60 * 60 },
]

export interface SourcesTabAddPayload {
  feedUrl: string
  authMethod: CatalogSourceAuthMethod
  authSecretRef: string | null
  refreshIntervalS: number
}

export interface SourcesTabProps {
  /** Snapshot of subscriptions for the current tenant — typically piped
   *  from `useCatalogSources()` in the page wrapper. */
  sources?: ReadonlyArray<CatalogSource>
  /** True while the page wrapper is fetching the snapshot for the first
   *  time. The toolbar shows a small spinner instead of "(N)". */
  loading?: boolean
  /** Last error message from the snapshot fetch (or null). Surfaced as
   *  a small banner above the list so the operator sees why nothing
   *  is rendered. */
  fetchError?: string | null
  /** Add-source submit. Resolves with the newly-created row. The page
   *  wrapper typically calls `createCatalogSource()` then triggers a
   *  refresh; this component closes the inline form on success. */
  onAdd?: (payload: SourcesTabAddPayload) => Promise<CatalogSource>
  /** Sync-now button. Resolves with the updated row (the page wrapper
   *  typically refreshes the list to show the new `last_sync_status`). */
  onSync?: (source: CatalogSource) => Promise<CatalogSource>
  /** Remove button. Resolves once the backend has confirmed the row is
   *  gone; the page wrapper refreshes the list afterwards. */
  onRemove?: (source: CatalogSource) => Promise<void>
  /** Optional retry trigger when the snapshot fetch failed. */
  onRetry?: () => void
  className?: string
}

interface FormFields {
  feedUrl: string
  authMethod: CatalogSourceAuthMethod
  authSecretRef: string
  refreshIntervalS: number
}

const FORM_INITIAL: FormFields = {
  feedUrl: "",
  authMethod: "none",
  authSecretRef: "",
  refreshIntervalS: CATALOG_SOURCE_REFRESH_DEFAULT_S,
}

/** Format a duration in seconds for display in the table. Uses the
 *  largest whole-unit fit (s / m / h / d) so the row stays terse. */
export function formatRefreshInterval(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—"
  if (seconds % 86400 === 0) {
    const d = seconds / 86400
    return `${d}d`
  }
  if (seconds % 3600 === 0) {
    const h = seconds / 3600
    return `${h}h`
  }
  if (seconds % 60 === 0) {
    const m = seconds / 60
    return `${m}m`
  }
  return `${seconds}s`
}

/** Parse the wire `last_synced_at` ISO string into a relative duration
 *  string ("2h ago" / "3d ago" / "never"). Pure helper, exported so
 *  unit tests can pin the clock without touching component state. */
export function formatLastSyncedRelative(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "never"
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return "—"
  const deltaMs = now.getTime() - t
  if (deltaMs < 0) return "just now"
  const seconds = Math.floor(deltaMs / 1000)
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  const months = Math.floor(days / 30)
  if (months < 12) return `${months}mo ago`
  const years = Math.floor(days / 365)
  return `${years}y ago`
}

/** Pure validation pass over the add-source form fields. Returns null
 *  when the form is valid; otherwise returns the first user-facing
 *  message that should be surfaced. Exported so tests can exercise the
 *  validation matrix without mounting the component. */
export function validateSourcesTabForm(fields: FormFields): string | null {
  const urlMsg = validateCatalogSourceFeedUrl(fields.feedUrl)
  if (urlMsg) return urlMsg
  if (fields.authMethod !== "none" && fields.authSecretRef.trim().length === 0) {
    return "auth_secret_ref is required when auth method is not 'none'"
  }
  const refMsg = validateCatalogSourceAuthSecretRef(fields.authSecretRef)
  if (refMsg) return refMsg
  const intervalMsg = validateCatalogSourceRefreshInterval(fields.refreshIntervalS)
  if (intervalMsg) return intervalMsg
  return null
}

export function SourcesTab({
  sources,
  loading,
  fetchError,
  onAdd,
  onSync,
  onRemove,
  onRetry,
  className,
}: SourcesTabProps) {
  const rows = useMemo<ReadonlyArray<CatalogSource>>(
    () => (sources ?? []).slice().sort((a, b) => {
      // Newest first so a freshly-added subscription bubbles to the top.
      const ta = Date.parse(a.created_at) || 0
      const tb = Date.parse(b.created_at) || 0
      if (tb !== ta) return tb - ta
      return a.id.localeCompare(b.id)
    }),
    [sources],
  )

  const [formOpen, setFormOpen] = useState<boolean>(false)
  const [formFields, setFormFields] = useState<FormFields>(FORM_INITIAL)
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState<boolean>(false)

  const [pendingSyncId, setPendingSyncId] = useState<string | null>(null)
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)

  const handleOpenForm = useCallback(() => {
    setFormFields(FORM_INITIAL)
    setFormError(null)
    setFormOpen(true)
  }, [])

  const handleCloseForm = useCallback(() => {
    setFormOpen(false)
    setFormError(null)
  }, [])

  const handleSubmitForm = useCallback(
    async (event?: React.FormEvent<HTMLFormElement>) => {
      if (event) event.preventDefault()
      if (submitting) return
      const validation = validateSourcesTabForm(formFields)
      if (validation) {
        setFormError(validation)
        return
      }
      if (!onAdd) {
        // No handler wired — close the form silently to avoid the
        // operator seeing a perpetual "Submitting…" spinner. This
        // branch should never fire in production wiring; tests still
        // exercise it to guarantee the component does not throw.
        setFormOpen(false)
        setFormError(null)
        return
      }
      setSubmitting(true)
      setFormError(null)
      setLastError(null)
      try {
        await onAdd({
          feedUrl: formFields.feedUrl.trim(),
          authMethod: formFields.authMethod,
          authSecretRef:
            formFields.authMethod === "none" || formFields.authSecretRef.length === 0
              ? null
              : formFields.authSecretRef,
          refreshIntervalS: formFields.refreshIntervalS,
        })
        setFormOpen(false)
        setFormFields(FORM_INITIAL)
      } catch (err) {
        const message =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "failed to add source"
        setFormError(message)
      } finally {
        setSubmitting(false)
      }
    },
    [formFields, onAdd, submitting],
  )

  const handleSync = useCallback(
    async (source: CatalogSource) => {
      if (pendingSyncId) return
      if (!onSync) return
      setPendingSyncId(source.id)
      setLastError(null)
      try {
        await onSync(source)
      } catch (err) {
        const message =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "sync failed"
        setLastError(`Sync failed for ${source.feed_url}: ${message}`)
      } finally {
        setPendingSyncId(null)
      }
    },
    [onSync, pendingSyncId],
  )

  const handleDeleteRequest = useCallback((source: CatalogSource) => {
    setConfirmDeleteId(source.id)
    setLastError(null)
  }, [])

  const handleDeleteCancel = useCallback(() => {
    setConfirmDeleteId(null)
  }, [])

  const handleDeleteConfirm = useCallback(
    async (source: CatalogSource) => {
      if (pendingDeleteId) return
      if (!onRemove) {
        setConfirmDeleteId(null)
        return
      }
      setPendingDeleteId(source.id)
      setLastError(null)
      try {
        await onRemove(source)
        setConfirmDeleteId(null)
      } catch (err) {
        const message =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "remove failed"
        setLastError(`Remove failed for ${source.feed_url}: ${message}`)
      } finally {
        setPendingDeleteId(null)
      }
    },
    [onRemove, pendingDeleteId],
  )

  const renderSyncStatusChip = (source: CatalogSource): React.ReactNode => {
    const status = source.last_sync_status
    if (!status) {
      return (
        <span
          className="inline-flex items-center rounded border border-[var(--border)] bg-[var(--card)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)]"
          data-testid={`sources-tab-row-status-${source.id}`}
          data-status="none"
        >
          never synced
        </span>
      )
    }
    const tone = SYNC_STATUS_TONE[status] ?? "info"
    const cls =
      tone === "ok"
        ? "border-emerald-500/55 bg-emerald-500/10 text-emerald-300"
        : tone === "warn"
          ? "border-amber-500/55 bg-amber-500/10 text-amber-300"
          : "border-[var(--neural-blue)]/45 bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]"
    return (
      <span
        className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] ${cls}`}
        data-testid={`sources-tab-row-status-${source.id}`}
        data-status={status}
      >
        {status}
      </span>
    )
  }

  return (
    <div
      className={["flex flex-col gap-3", className].filter(Boolean).join(" ")}
      data-testid="sources-tab"
    >
      {/* ── Toolbar ──────────────────────────────────────────────── */}
      <div
        className="flex items-center justify-between gap-2"
        data-testid="sources-tab-toolbar"
      >
        <div className="flex items-center gap-2 font-mono text-[11px] text-[var(--muted-foreground)]">
          <Rss size={12} aria-hidden />
          <span data-testid="sources-tab-count">
            {loading
              ? "Loading sources…"
              : `${rows.length} ${rows.length === 1 ? "source" : "sources"}`}
          </span>
        </div>
        <button
          type="button"
          onClick={handleOpenForm}
          disabled={formOpen || submitting}
          className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--card)] px-2.5 py-1 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
          data-testid="sources-tab-add-button"
        >
          <Plus size={12} aria-hidden />
          Add source
        </button>
      </div>

      {/* ── Snapshot fetch error ─────────────────────────────────── */}
      {fetchError ? (
        <div
          className="flex items-center justify-between gap-2 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-3 py-2 font-mono text-[11px] text-[var(--critical-red)]"
          data-testid="sources-tab-fetch-error"
        >
          <span className="flex items-start gap-2">
            <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
            <span>Could not load sources — {fetchError}</span>
          </span>
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex items-center gap-1 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/10 px-2 py-0.5 text-[10px] hover:bg-[var(--critical-red)]/20"
              data-testid="sources-tab-fetch-retry"
            >
              <RefreshCw size={10} aria-hidden />
              Retry
            </button>
          ) : null}
        </div>
      ) : null}

      {/* ── Inline error from sync / remove ──────────────────────── */}
      {lastError ? (
        <div
          className="flex items-start gap-2 rounded border border-amber-500/45 bg-amber-500/5 px-3 py-2 font-mono text-[11px] text-amber-300"
          data-testid="sources-tab-error"
        >
          <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
          <span>{lastError}</span>
        </div>
      ) : null}

      {/* ── Add-source form ──────────────────────────────────────── */}
      {formOpen ? (
        <form
          onSubmit={handleSubmitForm}
          className="flex flex-col gap-3 rounded border border-[var(--border)] bg-[var(--card)]/50 p-3"
          data-testid="sources-tab-form"
        >
          <div className="flex items-center justify-between">
            <span className="font-mono text-xs text-[var(--foreground)]">
              Add catalog feed source
            </span>
            <button
              type="button"
              onClick={handleCloseForm}
              className="inline-flex items-center rounded p-1 text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
              data-testid="sources-tab-form-close"
              aria-label="Close add-source form"
            >
              <X size={12} aria-hidden />
            </button>
          </div>

          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              Feed URL
            </span>
            <input
              type="url"
              required
              value={formFields.feedUrl}
              onChange={(e) =>
                setFormFields((f) => ({ ...f, feedUrl: e.target.value }))
              }
              placeholder="https://feeds.example.com/catalog.json"
              className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
              data-testid="sources-tab-form-feed-url"
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              Auth method
            </span>
            <select
              value={formFields.authMethod}
              onChange={(e) =>
                setFormFields((f) => ({
                  ...f,
                  authMethod: e.target.value as CatalogSourceAuthMethod,
                }))
              }
              className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)]"
              data-testid="sources-tab-form-auth-method"
            >
              {CATALOG_SOURCE_AUTH_METHODS.map((m) => (
                <option key={m} value={m}>
                  {AUTH_METHOD_LABEL[m]}
                </option>
              ))}
            </select>
          </label>

          {formFields.authMethod !== "none" ? (
            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Auth secret ref
              </span>
              <input
                type="text"
                value={formFields.authSecretRef}
                onChange={(e) =>
                  setFormFields((f) => ({ ...f, authSecretRef: e.target.value }))
                }
                placeholder="secret-store key, no whitespace"
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
                data-testid="sources-tab-form-auth-secret-ref"
              />
              <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                Reference into the tenant secret store. Never paste a literal token.
              </span>
            </label>
          ) : null}

          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              Refresh interval (seconds)
            </span>
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={CATALOG_SOURCE_REFRESH_MIN_S}
                max={CATALOG_SOURCE_REFRESH_MAX_S}
                step={1}
                value={formFields.refreshIntervalS}
                onChange={(e) =>
                  setFormFields((f) => ({
                    ...f,
                    refreshIntervalS: Number.parseInt(e.target.value, 10),
                  }))
                }
                className="h-8 w-32 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)]"
                data-testid="sources-tab-form-refresh-interval"
              />
              <select
                value={formFields.refreshIntervalS}
                onChange={(e) =>
                  setFormFields((f) => ({
                    ...f,
                    refreshIntervalS: Number.parseInt(e.target.value, 10),
                  }))
                }
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--muted-foreground)]"
                data-testid="sources-tab-form-refresh-preset"
                aria-label="Refresh interval preset"
              >
                <option value={formFields.refreshIntervalS}>preset…</option>
                {REFRESH_PRESETS.map((p) => (
                  <option key={p.label} value={p.seconds}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>
          </label>

          {formError ? (
            <div
              className="flex items-start gap-2 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-2 py-1.5 font-mono text-[11px] text-[var(--critical-red)]"
              data-testid="sources-tab-form-error"
            >
              <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
              <span>{formError}</span>
            </div>
          ) : null}

          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={handleCloseForm}
              disabled={submitting}
              className="inline-flex items-center justify-center rounded border border-[var(--border)] bg-[var(--card)] px-3 py-1.5 font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="sources-tab-form-cancel"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center justify-center gap-1 rounded bg-[var(--neural-blue)]/15 px-3 py-1.5 font-mono text-xs text-[var(--neural-blue)] ring-1 ring-[var(--neural-blue)]/55 hover:bg-[var(--neural-blue)]/25 disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="sources-tab-form-submit"
            >
              {submitting ? (
                <Loader2 size={12} aria-hidden className="animate-spin" />
              ) : (
                <Plus size={12} aria-hidden />
              )}
              {submitting ? "Adding…" : "Add source"}
            </button>
          </div>
        </form>
      ) : null}

      {/* ── List body ────────────────────────────────────────────── */}
      {rows.length === 0 && !loading ? (
        <div
          className="rounded border border-dashed border-[var(--border)] bg-[var(--card)]/40 p-6 text-center font-mono text-[11px] text-[var(--muted-foreground)]"
          data-testid="sources-tab-empty"
        >
          No catalog feed subscriptions yet. Click <span className="text-[var(--foreground)]">Add source</span> to start syncing.
        </div>
      ) : (
        <ul
          className="flex flex-col divide-y divide-[var(--border)] overflow-hidden rounded border border-[var(--border)]"
          data-testid="sources-tab-list"
        >
          {rows.map((source) => {
            const isSyncing = pendingSyncId === source.id
            const isDeleting = pendingDeleteId === source.id
            const isConfirming = confirmDeleteId === source.id
            return (
              <li
                key={source.id}
                className={[
                  "flex flex-col gap-2 bg-[var(--card)]/30 p-3",
                  source.enabled ? "" : "opacity-60",
                ].join(" ")}
                data-testid={`sources-tab-row-${source.id}`}
                data-source-id={source.id}
                data-enabled={source.enabled ? "true" : "false"}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span
                      className="truncate font-mono text-xs text-[var(--foreground)]"
                      title={source.feed_url}
                      data-testid={`sources-tab-row-url-${source.id}`}
                    >
                      {source.feed_url}
                    </span>
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-[var(--muted-foreground)]">
                      <span data-testid={`sources-tab-row-auth-${source.id}`}>
                        {AUTH_METHOD_LABEL[source.auth_method]}
                      </span>
                      <span aria-hidden>·</span>
                      <span data-testid={`sources-tab-row-interval-${source.id}`}>
                        every {formatRefreshInterval(source.refresh_interval_s)}
                      </span>
                      <span aria-hidden>·</span>
                      <span data-testid={`sources-tab-row-last-synced-${source.id}`}>
                        last synced {formatLastSyncedRelative(source.last_synced_at)}
                      </span>
                      {!source.enabled ? (
                        <>
                          <span aria-hidden>·</span>
                          <span
                            className="rounded border border-amber-500/55 bg-amber-500/10 px-1.5 py-0.5 text-amber-300"
                            data-testid={`sources-tab-row-disabled-${source.id}`}
                          >
                            disabled
                          </span>
                        </>
                      ) : null}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {renderSyncStatusChip(source)}
                    <button
                      type="button"
                      onClick={() => handleSync(source)}
                      disabled={isSyncing || isDeleting || !onSync}
                      className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--card)] px-2 py-1 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
                      data-testid={`sources-tab-row-sync-${source.id}`}
                      aria-label={`Sync ${source.feed_url} now`}
                    >
                      {isSyncing ? (
                        <Loader2 size={12} aria-hidden className="animate-spin" />
                      ) : (
                        <RefreshCw size={12} aria-hidden />
                      )}
                      {isSyncing ? "Syncing…" : "Sync now"}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDeleteRequest(source)}
                      disabled={isSyncing || isDeleting || !onRemove}
                      className="inline-flex items-center gap-1 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-2 py-1 font-mono text-[11px] text-[var(--critical-red)] hover:bg-[var(--critical-red)]/15 disabled:cursor-not-allowed disabled:opacity-50"
                      data-testid={`sources-tab-row-remove-${source.id}`}
                      aria-label={`Remove ${source.feed_url}`}
                    >
                      <Trash2 size={12} aria-hidden />
                      Remove
                    </button>
                  </div>
                </div>

                {isConfirming ? (
                  <div
                    className="flex flex-wrap items-center justify-between gap-2 rounded border border-[var(--critical-red)]/55 bg-[var(--critical-red)]/5 px-3 py-2 font-mono text-[11px] text-[var(--critical-red)]"
                    data-testid={`sources-tab-row-confirm-${source.id}`}
                  >
                    <span className="flex items-start gap-2">
                      <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
                      Remove this subscription? Catalog entries already pulled in
                      will stay; future refreshes will stop.
                    </span>
                    <span className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={handleDeleteCancel}
                        disabled={isDeleting}
                        className="inline-flex items-center rounded border border-[var(--border)] bg-[var(--card)] px-2 py-0.5 text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
                        data-testid={`sources-tab-row-confirm-cancel-${source.id}`}
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDeleteConfirm(source)}
                        disabled={isDeleting}
                        className="inline-flex items-center gap-1 rounded bg-[var(--critical-red)]/15 px-2 py-0.5 text-[10px] text-[var(--critical-red)] ring-1 ring-[var(--critical-red)]/55 hover:bg-[var(--critical-red)]/25 disabled:cursor-not-allowed disabled:opacity-50"
                        data-testid={`sources-tab-row-confirm-delete-${source.id}`}
                      >
                        {isDeleting ? (
                          <Loader2 size={10} aria-hidden className="animate-spin" />
                        ) : (
                          <Trash2 size={10} aria-hidden />
                        )}
                        {isDeleting ? "Removing…" : "Remove"}
                      </button>
                    </span>
                  </div>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

"use client"

/**
 * BS.8.6 — Custom catalog entry CRUD form (admin only).
 *
 * The fourth sub-tab on Settings → Platforms. Renders the per-tenant
 * list of `operator` / `override` catalog entries (the "custom" set —
 * everything that isn't a `shipped` seed or a `subscription` feed
 * import) and exposes admin-only CRUD over them:
 *   • Add a new entry via an inline full-CRUD form covering vendor /
 *     family / version / install method / install URL / sha256 /
 *     license / size estimate / depends_on multi-select.
 *   • Per-row Edit button — opens the same inline form pre-populated
 *     with the row's current values so the admin can update.
 *   • Per-row Remove button (DELETE /catalog/entries/{id}) with an
 *     inline confirm overlay so a misclick doesn't tombstone an
 *     entry.
 *   • Form validation: backend regex-mirrored id format, sha256 hex
 *     digest format, install URL scheme + length, size_bytes range,
 *     unique-id check against the in-memory snapshot, and an opt-in
 *     URL ping that performs a HEAD request through the global fetch
 *     to surface obviously-unreachable URLs early. The backend's
 *     409 / 422 / 404 still has the final word — frontend validation
 *     keeps the form responsive.
 *
 * Why purely presentational + caller-supplied callbacks
 * ─────────────────────────────────────────────────────
 * Mirrors the `<SourcesTab />` design (BS.8.5): the page wrapper owns
 * the data flow (`useCatalogEntries()` snapshot + a refresh after each
 * mutation). The component is UI-only so:
 *   1. Tests can assert behaviour without spinning a real network mock —
 *      they pass `vi.fn()` callbacks and inspect the calls.
 *   2. Future page-level admin gates live in the wrapper, not deep
 *      inside the component tree.
 *   3. The catch-all `<ApiErrorToastCenter />` already surfaces 403 /
 *      409 / 422 from the backend; this form additionally renders an
 *      inline form-error banner so the operator sees the cause without
 *      leaving the form context.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * Per-component-instance React state only:
 *   - `formMode` / `formFields` / `formError` / `submitting` — form state
 *   - `pendingDeleteId` / `confirmDeleteId` — per-row delete state
 *   - `urlPingState` — per-form URL ping result (transient, fades on
 *     next form open)
 *   - `lastError` — surfaced after a failed delete
 * No module-level mutable state, no in-memory cache. Browser-only —
 * cross-worker / multi-tab consistency comes from the backend reading
 * from PG; each tab refreshes its own snapshot via the page wrapper.
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Every mutation (create / patch / delete) commits before the HTTP 200
 * returns; the page wrapper's `onChanged` callback fires `refresh()`
 * which re-reads via PG MVCC, so the table re-renders with the new
 * state. Two admins editing the same id can race — the loser sees a
 * 409 (duplicate) or 404 (already-deleted) which surfaces as a form
 * error banner; this is a race, not a bug.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  Edit3,
  HardDrive,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react"

import {
  CATALOG_ENTRY_FAMILIES,
  CATALOG_ENTRY_INSTALL_METHODS,
  CATALOG_ENTRY_INSTALL_URL_MAX_LEN,
  CATALOG_ENTRY_WRITABLE_SOURCES,
  normaliseCatalogEntryId,
  validateCatalogEntryDisplayName,
  validateCatalogEntryId,
  validateCatalogEntryInstallUrl,
  validateCatalogEntrySha256,
  validateCatalogEntrySizeBytes,
  validateCatalogEntryVendor,
  validateCatalogEntryVersion,
  type CatalogEntryDetail,
  type CatalogEntryFamily,
  type CatalogEntryInstallMethod,
  type CatalogEntryWritableSource,
} from "@/lib/api"

const FAMILY_LABEL: Record<CatalogEntryFamily, string> = {
  mobile: "Mobile",
  embedded: "Embedded",
  web: "Web",
  software: "Software",
  rtos: "RTOS",
  "cross-toolchain": "Cross toolchain",
  custom: "Custom",
}

const INSTALL_METHOD_LABEL: Record<CatalogEntryInstallMethod, string> = {
  noop: "No-op (manual)",
  docker_pull: "docker pull",
  shell_script: "Shell script",
  vendor_installer: "Vendor installer",
}

const SIZE_PRESETS: ReadonlyArray<{ label: string; bytes: number }> = [
  { label: "10 MB", bytes: 10 * 1024 * 1024 },
  { label: "100 MB", bytes: 100 * 1024 * 1024 },
  { label: "500 MB", bytes: 500 * 1024 * 1024 },
  { label: "1 GB", bytes: 1024 * 1024 * 1024 },
  { label: "5 GB", bytes: 5 * 1024 * 1024 * 1024 },
]

export type CustomEntryFormMode =
  | { kind: "closed" }
  | { kind: "create" }
  | { kind: "edit"; entryId: string }

export interface CustomEntryFormPayload {
  id: string
  source: CatalogEntryWritableSource
  vendor: string | null
  family: CatalogEntryFamily | null
  display_name: string | null
  version: string | null
  install_method: CatalogEntryInstallMethod | null
  install_url: string | null
  sha256: string | null
  size_bytes: number | null
  depends_on: string[]
  metadata: Record<string, unknown>
}

export interface CustomEntryFormProps {
  /** Snapshot of CRUD-able entries (operator / override sources) — the
   *  upper list view binds to this filtered slice. */
  entries?: ReadonlyArray<CatalogEntryDetail>
  /** Full catalog snapshot (operator + override + shipped + subscription).
   *  Used to populate the `depends_on` multi-select so an admin can
   *  declare a dependency on any visible catalog row, not just the
   *  custom subset. */
  allEntries?: ReadonlyArray<CatalogEntryDetail>
  /** True while the page wrapper is still fetching the snapshot. */
  loading?: boolean
  /** Last error from the snapshot fetch, surfaced as a banner. */
  fetchError?: string | null
  /** Async URL ping. Defaults to a HEAD fetch via `globalThis.fetch`
   *  with a short timeout. Tests inject a fake to avoid network. */
  pingUrlFn?: (url: string) => Promise<UrlPingResult>
  /** Add-entry submit. Resolves with the newly-created row. */
  onCreate?: (payload: CustomEntryFormPayload) => Promise<CatalogEntryDetail>
  /** Patch-entry submit. Resolves with the updated row. */
  onPatch?: (
    entryId: string,
    payload: CustomEntryFormPayload,
  ) => Promise<CatalogEntryDetail>
  /** Per-row remove. Resolves once the backend confirms the soft-
   *  delete. */
  onRemove?: (entry: CatalogEntryDetail) => Promise<void>
  /** Optional retry trigger when the snapshot fetch failed. */
  onRetry?: () => void
  className?: string
}

export type UrlPingResult =
  | { kind: "ok"; status?: number }
  | { kind: "error"; message: string }
  | { kind: "skipped" }

interface FormFields {
  id: string
  source: CatalogEntryWritableSource
  vendor: string
  family: CatalogEntryFamily | ""
  display_name: string
  version: string
  install_method: CatalogEntryInstallMethod | ""
  install_url: string
  sha256: string
  size_bytes: string // raw input, parsed at submit
  license: string
  depends_on: string[]
}

const FORM_INITIAL: FormFields = {
  id: "",
  source: "operator",
  vendor: "",
  family: "",
  display_name: "",
  version: "",
  install_method: "",
  install_url: "",
  sha256: "",
  size_bytes: "",
  license: "",
  depends_on: [],
}

/** Default URL ping. Performs a HEAD with a 5 s timeout via
 *  `AbortController` and returns one of the three outcomes. CORS
 *  failures fall back to `{kind: "skipped"}` — many vendor URLs
 *  refuse cross-origin HEAD, and we don't want to flag a typical
 *  Google Play / vendor CDN URL as broken. */
export async function defaultPingCustomEntryUrl(
  url: string,
): Promise<UrlPingResult> {
  if (typeof globalThis.fetch !== "function") {
    return { kind: "skipped" }
  }
  const ctrl = new AbortController()
  const tid = setTimeout(() => ctrl.abort(), 5000)
  try {
    const res = await globalThis.fetch(url, {
      method: "HEAD",
      mode: "no-cors",
      redirect: "follow",
      signal: ctrl.signal,
    })
    clearTimeout(tid)
    // `no-cors` always yields opaque responses with status=0. The
    // mere fact the fetch resolved without throwing is the evidence
    // we surface — DNS + TCP succeeded.
    if (res.type === "opaque") return { kind: "skipped" }
    if (res.ok) return { kind: "ok", status: res.status }
    return { kind: "error", message: `HEAD returned ${res.status}` }
  } catch (err) {
    clearTimeout(tid)
    const message =
      err instanceof Error ? err.message : typeof err === "string" ? err : "fetch failed"
    return { kind: "error", message }
  }
}

/** Pure validation pass over the inline form fields. Returns null when
 *  the form is valid; otherwise returns the first user-facing message
 *  that should be surfaced. Exported so tests can exercise the
 *  validation matrix without mounting the component.
 *
 *  When `existingIds` is supplied and `mode === "create"`, this also
 *  enforces the "id is unique against the in-memory snapshot" rule —
 *  the backend's UNIQUE constraint still has the final word, but the
 *  frontend pre-check avoids a wasted round-trip on the obvious case.
 *  In `edit` mode the id is pinned to the originally-selected entry,
 *  so no uniqueness check fires.
 */
export function validateCustomEntryFormFields(
  fields: FormFields,
  options?: { mode: "create" | "edit"; existingIds?: ReadonlyArray<string> },
): string | null {
  const idMsg = validateCatalogEntryId(fields.id)
  if (idMsg) return idMsg
  if (
    options?.mode === "create" &&
    options.existingIds &&
    options.existingIds.includes(normaliseCatalogEntryId(fields.id))
  ) {
    return `entry id "${fields.id}" already exists in the catalog`
  }
  if (fields.source === "operator") {
    // Operator rows are standalone — every required col must be set.
    if (fields.vendor.trim().length === 0) return "vendor is required for operator entries"
    if (fields.family.length === 0) return "family is required for operator entries"
    if (fields.display_name.trim().length === 0) {
      return "display_name is required for operator entries"
    }
    if (fields.version.trim().length === 0) {
      return "version is required for operator entries"
    }
    if (fields.install_method.length === 0) {
      return "install_method is required for operator entries"
    }
  }
  const vendorMsg = validateCatalogEntryVendor(fields.vendor)
  if (vendorMsg) return vendorMsg
  const displayMsg = validateCatalogEntryDisplayName(fields.display_name)
  if (displayMsg) return displayMsg
  const versionMsg = validateCatalogEntryVersion(fields.version)
  if (versionMsg) return versionMsg
  const urlMsg = validateCatalogEntryInstallUrl(fields.install_url)
  if (urlMsg) return urlMsg
  const sha256Msg = validateCatalogEntrySha256(fields.sha256)
  if (sha256Msg) return sha256Msg
  const sizeBytes = parseSizeBytesInput(fields.size_bytes)
  if (sizeBytes !== null && typeof sizeBytes === "string") {
    return sizeBytes
  }
  return null
}

/** Parse the size_bytes input. Accepts a plain decimal integer ("104857600")
 *  or a human-readable suffix ("100 MB"). Returns the byte count as a
 *  number on success, `null` for an empty input, or a string error
 *  message when malformed.
 *
 *  Exported so tests can lock the parsing contract without mounting
 *  the component. */
export function parseSizeBytesInput(raw: string): number | null | string {
  const v = raw.trim()
  if (v.length === 0) return null
  // Accept numeric forms: "12345", "12_345", "12 345" — strip spaces
  // and underscores. Do not strip commas (locale-dependent and could
  // be misread).
  const numericOnly = v.replace(/[\s_]/g, "")
  if (/^\d+$/.test(numericOnly)) {
    const n = Number.parseInt(numericOnly, 10)
    return validateCatalogEntrySizeBytes(n) ?? n
  }
  // Suffix forms: "100MB", "100 MB", "1.5 GB", "2tb", etc.
  const m = v.match(/^([0-9]+(?:\.[0-9]+)?)\s*([KMGT]i?B?)$/i)
  if (!m) return "size must be a number of bytes or a suffix like 100MB / 1.5GB"
  const value = Number.parseFloat(m[1]!)
  const suffix = m[2]!.toUpperCase()
  const factor: Record<string, number> = {
    K: 1000, KB: 1000, KIB: 1024,
    M: 1000 ** 2, MB: 1000 ** 2, MIB: 1024 ** 2,
    G: 1000 ** 3, GB: 1000 ** 3, GIB: 1024 ** 3,
    T: 1000 ** 4, TB: 1000 ** 4, TIB: 1024 ** 4,
  }
  const f = factor[suffix]
  if (!f) return "unknown size suffix"
  const bytes = Math.round(value * f)
  return validateCatalogEntrySizeBytes(bytes) ?? bytes
}

/** Format size_bytes for display in the list. Picks the largest
 *  whole-unit fit, rounded to one decimal. */
export function formatSizeBytes(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return "—"
  if (bytes === 0) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let i = 0
  let v = bytes
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

/** Pick the list of entry ids that can appear in a depends_on multi-
 *  select for the given form context. Excludes the current id (a row
 *  can't depend on itself) and any hidden rows the snapshot includes
 *  defensively. */
export function pickAvailableDependsOnIds(
  allEntries: ReadonlyArray<CatalogEntryDetail>,
  selfId: string | null,
): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const e of allEntries) {
    if (!e || typeof e.id !== "string") continue
    if (selfId && e.id === selfId) continue
    if (e.hidden) continue
    if (seen.has(e.id)) continue
    seen.add(e.id)
    out.push(e.id)
  }
  out.sort((a, b) => a.localeCompare(b))
  return out
}

function entryToFormFields(entry: CatalogEntryDetail): FormFields {
  // Pull `license` out of the entry's metadata so the operator can edit
  // it as a first-class field. We deliberately leave the rest of
  // metadata alone — round-tripping the JSONB via a textarea risks
  // accidental data loss, and the form does not commit to "edit any
  // metadata key".
  const license =
    typeof entry.metadata?.license === "string" ? (entry.metadata.license as string) : ""
  const source: CatalogEntryWritableSource =
    entry.source === "override" ? "override" : "operator"
  return {
    id: entry.id,
    source,
    vendor: entry.vendor ?? "",
    family: (entry.family ?? "") as CatalogEntryFamily | "",
    display_name: entry.display_name ?? "",
    version: entry.version ?? "",
    install_method: (entry.install_method ?? "") as CatalogEntryInstallMethod | "",
    install_url: entry.install_url ?? "",
    sha256: entry.sha256 ?? "",
    size_bytes: entry.size_bytes !== null ? String(entry.size_bytes) : "",
    license,
    depends_on: Array.isArray(entry.depends_on) ? [...entry.depends_on] : [],
  }
}

function formFieldsToPayload(
  fields: FormFields,
  baseMetadata: Record<string, unknown>,
): CustomEntryFormPayload | string {
  const sizeBytes = parseSizeBytesInput(fields.size_bytes)
  if (typeof sizeBytes === "string") return sizeBytes
  // Merge license back into metadata so the JSONB column carries it.
  // An empty license clears the key (operator wants it gone).
  const metadata: Record<string, unknown> = { ...baseMetadata }
  const lic = fields.license.trim()
  if (lic.length > 0) {
    metadata.license = lic
  } else if ("license" in metadata) {
    delete metadata.license
  }
  const family = fields.family.length > 0 ? fields.family : null
  const installMethod =
    fields.install_method.length > 0 ? fields.install_method : null
  const vendor = fields.vendor.trim().length > 0 ? fields.vendor.trim() : null
  const displayName =
    fields.display_name.trim().length > 0 ? fields.display_name.trim() : null
  const version = fields.version.trim().length > 0 ? fields.version.trim() : null
  const installUrl =
    fields.install_url.trim().length > 0 ? fields.install_url.trim() : null
  const sha256 = fields.sha256.trim().length > 0 ? fields.sha256.trim() : null
  return {
    id: normaliseCatalogEntryId(fields.id),
    source: fields.source,
    vendor,
    family,
    display_name: displayName,
    version,
    install_method: installMethod,
    install_url: installUrl,
    sha256,
    size_bytes: sizeBytes,
    depends_on: [...fields.depends_on],
    metadata,
  }
}

export function CustomEntryForm({
  entries,
  allEntries,
  loading,
  fetchError,
  pingUrlFn,
  onCreate,
  onPatch,
  onRemove,
  onRetry,
  className,
}: CustomEntryFormProps) {
  const rows = useMemo<ReadonlyArray<CatalogEntryDetail>>(
    () =>
      (entries ?? []).slice().sort((a, b) => {
        const ta = Date.parse(a.created_at) || 0
        const tb = Date.parse(b.created_at) || 0
        if (tb !== ta) return tb - ta
        return a.id.localeCompare(b.id)
      }),
    [entries],
  )

  const allRows = useMemo<ReadonlyArray<CatalogEntryDetail>>(
    () => allEntries ?? entries ?? [],
    [allEntries, entries],
  )

  const [formMode, setFormMode] = useState<CustomEntryFormMode>({ kind: "closed" })
  const [formFields, setFormFields] = useState<FormFields>(FORM_INITIAL)
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState<boolean>(false)
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)
  const [urlPingState, setUrlPingState] = useState<
    { kind: "idle" } | { kind: "pinging" } | { kind: "result"; result: UrlPingResult }
  >({ kind: "idle" })
  const [baseMetadata, setBaseMetadata] = useState<Record<string, unknown>>({})

  const existingIds = useMemo(
    () => allRows.map((e) => e.id),
    [allRows],
  )

  const dependsOnOptions = useMemo(() => {
    const selfId =
      formMode.kind === "edit" ? formMode.entryId : null
    return pickAvailableDependsOnIds(allRows, selfId)
  }, [allRows, formMode])

  const handleOpenCreate = useCallback(() => {
    setFormMode({ kind: "create" })
    setFormFields(FORM_INITIAL)
    setBaseMetadata({})
    setFormError(null)
    setUrlPingState({ kind: "idle" })
  }, [])

  const handleOpenEdit = useCallback(
    (entry: CatalogEntryDetail) => {
      setFormMode({ kind: "edit", entryId: entry.id })
      setFormFields(entryToFormFields(entry))
      const meta = entry.metadata && typeof entry.metadata === "object"
        ? { ...entry.metadata }
        : {}
      setBaseMetadata(meta)
      setFormError(null)
      setUrlPingState({ kind: "idle" })
    },
    [],
  )

  const handleCloseForm = useCallback(() => {
    setFormMode({ kind: "closed" })
    setFormError(null)
    setUrlPingState({ kind: "idle" })
  }, [])

  // Reset URL ping state whenever the URL field changes — the previous
  // result is no longer relevant.
  useEffect(() => {
    setUrlPingState((prev) => (prev.kind === "idle" ? prev : { kind: "idle" }))
  }, [formFields.install_url])

  const handleSubmitForm = useCallback(
    async (event?: React.FormEvent<HTMLFormElement>) => {
      if (event) event.preventDefault()
      if (submitting) return
      const mode = formMode.kind === "create" ? "create" : "edit"
      const validation = validateCustomEntryFormFields(formFields, {
        mode,
        existingIds: mode === "create" ? existingIds : undefined,
      })
      if (validation) {
        setFormError(validation)
        return
      }
      const payloadOrError = formFieldsToPayload(formFields, baseMetadata)
      if (typeof payloadOrError === "string") {
        setFormError(payloadOrError)
        return
      }
      setSubmitting(true)
      setFormError(null)
      setLastError(null)
      try {
        if (formMode.kind === "create") {
          if (!onCreate) {
            setFormMode({ kind: "closed" })
            return
          }
          await onCreate(payloadOrError)
        } else if (formMode.kind === "edit") {
          if (!onPatch) {
            setFormMode({ kind: "closed" })
            return
          }
          await onPatch(formMode.entryId, payloadOrError)
        }
        setFormMode({ kind: "closed" })
        setFormFields(FORM_INITIAL)
        setBaseMetadata({})
      } catch (err) {
        const message =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "submit failed"
        setFormError(message)
      } finally {
        setSubmitting(false)
      }
    },
    [
      baseMetadata,
      existingIds,
      formFields,
      formMode,
      onCreate,
      onPatch,
      submitting,
    ],
  )

  const handleUrlPing = useCallback(async () => {
    const url = formFields.install_url.trim()
    const urlMsg = validateCatalogEntryInstallUrl(url)
    if (urlMsg || url.length === 0) {
      setUrlPingState({
        kind: "result",
        result: {
          kind: "error",
          message: urlMsg ?? "install URL is empty",
        },
      })
      return
    }
    setUrlPingState({ kind: "pinging" })
    const fn = pingUrlFn ?? defaultPingCustomEntryUrl
    let result: UrlPingResult
    try {
      result = await fn(url)
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : typeof err === "string"
            ? err
            : "ping failed"
      result = { kind: "error", message }
    }
    setUrlPingState({ kind: "result", result })
  }, [formFields.install_url, pingUrlFn])

  const handleDeleteRequest = useCallback((entry: CatalogEntryDetail) => {
    setConfirmDeleteId(entry.id)
    setLastError(null)
  }, [])

  const handleDeleteCancel = useCallback(() => {
    setConfirmDeleteId(null)
  }, [])

  const handleDeleteConfirm = useCallback(
    async (entry: CatalogEntryDetail) => {
      if (pendingDeleteId) return
      if (!onRemove) {
        setConfirmDeleteId(null)
        return
      }
      setPendingDeleteId(entry.id)
      setLastError(null)
      try {
        await onRemove(entry)
        setConfirmDeleteId(null)
      } catch (err) {
        const message =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "remove failed"
        setLastError(`Remove failed for ${entry.id}: ${message}`)
      } finally {
        setPendingDeleteId(null)
      }
    },
    [onRemove, pendingDeleteId],
  )

  const handleToggleDependsOn = useCallback((entryId: string) => {
    setFormFields((f) => {
      const next = f.depends_on.includes(entryId)
        ? f.depends_on.filter((x) => x !== entryId)
        : [...f.depends_on, entryId]
      return { ...f, depends_on: next }
    })
  }, [])

  const isFormOpen = formMode.kind !== "closed"
  const isEditMode = formMode.kind === "edit"

  return (
    <div
      className={["flex flex-col gap-3", className].filter(Boolean).join(" ")}
      data-testid="custom-entry-form"
    >
      {/* ── Toolbar ──────────────────────────────────────────────── */}
      <div
        className="flex items-center justify-between gap-2"
        data-testid="custom-entry-form-toolbar"
      >
        <div className="flex items-center gap-2 font-mono text-[11px] text-[var(--muted-foreground)]">
          <HardDrive size={12} aria-hidden />
          <span data-testid="custom-entry-form-count">
            {loading
              ? "Loading custom entries…"
              : `${rows.length} custom ${rows.length === 1 ? "entry" : "entries"}`}
          </span>
        </div>
        <button
          type="button"
          onClick={handleOpenCreate}
          disabled={isFormOpen || submitting}
          className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--card)] px-2.5 py-1 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
          data-testid="custom-entry-form-add-button"
        >
          <Plus size={12} aria-hidden />
          Add custom entry
        </button>
      </div>

      {/* ── Snapshot fetch error ─────────────────────────────────── */}
      {fetchError ? (
        <div
          className="flex items-center justify-between gap-2 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-3 py-2 font-mono text-[11px] text-[var(--critical-red)]"
          data-testid="custom-entry-form-fetch-error"
        >
          <span className="flex items-start gap-2">
            <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
            <span>Could not load custom entries — {fetchError}</span>
          </span>
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex items-center gap-1 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/10 px-2 py-0.5 text-[10px] hover:bg-[var(--critical-red)]/20"
              data-testid="custom-entry-form-fetch-retry"
            >
              <RefreshCw size={10} aria-hidden />
              Retry
            </button>
          ) : null}
        </div>
      ) : null}

      {/* ── Inline error from delete ─────────────────────────────── */}
      {lastError ? (
        <div
          className="flex items-start gap-2 rounded border border-amber-500/45 bg-amber-500/5 px-3 py-2 font-mono text-[11px] text-amber-300"
          data-testid="custom-entry-form-error"
        >
          <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
          <span>{lastError}</span>
        </div>
      ) : null}

      {/* ── Inline form (create or edit) ─────────────────────────── */}
      {isFormOpen ? (
        <form
          onSubmit={handleSubmitForm}
          className="flex flex-col gap-3 rounded border border-[var(--border)] bg-[var(--card)]/50 p-3"
          data-testid="custom-entry-form-form"
          data-form-mode={formMode.kind}
        >
          <div className="flex items-center justify-between">
            <span className="font-mono text-xs text-[var(--foreground)]">
              {isEditMode ? "Edit custom entry" : "Add custom catalog entry"}
            </span>
            <button
              type="button"
              onClick={handleCloseForm}
              className="inline-flex items-center rounded p-1 text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
              data-testid="custom-entry-form-form-close"
              aria-label="Close custom-entry form"
            >
              <X size={12} aria-hidden />
            </button>
          </div>

          {/* ── Identity ───────────────────────────────────────── */}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Entry ID *
              </span>
              <input
                type="text"
                required
                value={formFields.id}
                onChange={(e) =>
                  setFormFields((f) => ({ ...f, id: e.target.value }))
                }
                disabled={isEditMode}
                placeholder="vendor-product-suffix"
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60 disabled:opacity-60"
                data-testid="custom-entry-form-field-id"
              />
              <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                kebab-case, ≤ 64 chars. Cannot be edited after creation.
              </span>
            </label>

            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Source
              </span>
              <select
                value={formFields.source}
                onChange={(e) =>
                  setFormFields((f) => ({
                    ...f,
                    source: e.target.value as CatalogEntryWritableSource,
                  }))
                }
                disabled={isEditMode}
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] disabled:opacity-60"
                data-testid="custom-entry-form-field-source"
              >
                {CATALOG_ENTRY_WRITABLE_SOURCES.map((s) => (
                  <option key={s} value={s}>
                    {s === "operator" ? "operator (standalone)" : "override (overlay shipped)"}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Vendor {formFields.source === "operator" ? "*" : ""}
              </span>
              <input
                type="text"
                value={formFields.vendor}
                onChange={(e) =>
                  setFormFields((f) => ({ ...f, vendor: e.target.value }))
                }
                placeholder="NXP, Google, Yocto Project, …"
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
                data-testid="custom-entry-form-field-vendor"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Family {formFields.source === "operator" ? "*" : ""}
              </span>
              <select
                value={formFields.family}
                onChange={(e) =>
                  setFormFields((f) => ({
                    ...f,
                    family: e.target.value as CatalogEntryFamily | "",
                  }))
                }
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)]"
                data-testid="custom-entry-form-field-family"
              >
                <option value="">— select —</option>
                {CATALOG_ENTRY_FAMILIES.map((fam) => (
                  <option key={fam} value={fam}>
                    {FAMILY_LABEL[fam] ?? fam}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Display name {formFields.source === "operator" ? "*" : ""}
              </span>
              <input
                type="text"
                value={formFields.display_name}
                onChange={(e) =>
                  setFormFields((f) => ({ ...f, display_name: e.target.value }))
                }
                placeholder="i.MX 8M Mini SDK"
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
                data-testid="custom-entry-form-field-display-name"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Version {formFields.source === "operator" ? "*" : ""}
              </span>
              <input
                type="text"
                value={formFields.version}
                onChange={(e) =>
                  setFormFields((f) => ({ ...f, version: e.target.value }))
                }
                placeholder="1.0.0"
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
                data-testid="custom-entry-form-field-version"
              />
            </label>
          </div>

          {/* ── Install method ─────────────────────────────────── */}
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              Install method {formFields.source === "operator" ? "*" : ""}
            </span>
            <select
              value={formFields.install_method}
              onChange={(e) =>
                setFormFields((f) => ({
                  ...f,
                  install_method: e.target.value as CatalogEntryInstallMethod | "",
                }))
              }
              className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)]"
              data-testid="custom-entry-form-field-install-method"
            >
              <option value="">— select —</option>
              {CATALOG_ENTRY_INSTALL_METHODS.map((m) => (
                <option key={m} value={m}>
                  {INSTALL_METHOD_LABEL[m] ?? m}
                </option>
              ))}
            </select>
          </label>

          {/* ── URL + sha256 ──────────────────────────────────── */}
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              Install URL
            </span>
            <div className="flex items-center gap-2">
              <input
                type="url"
                value={formFields.install_url}
                onChange={(e) =>
                  setFormFields((f) => ({ ...f, install_url: e.target.value }))
                }
                placeholder="https://downloads.example.com/sdk.tar.gz"
                maxLength={CATALOG_ENTRY_INSTALL_URL_MAX_LEN}
                className="h-8 flex-1 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
                data-testid="custom-entry-form-field-install-url"
              />
              <button
                type="button"
                onClick={handleUrlPing}
                disabled={
                  urlPingState.kind === "pinging" ||
                  formFields.install_url.trim().length === 0
                }
                className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--card)] px-2 py-1 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
                data-testid="custom-entry-form-url-ping-button"
              >
                {urlPingState.kind === "pinging" ? (
                  <Loader2 size={12} aria-hidden className="animate-spin" />
                ) : (
                  <RefreshCw size={12} aria-hidden />
                )}
                Test URL
              </button>
            </div>
            {urlPingState.kind === "result" ? (
              <span
                data-testid="custom-entry-form-url-ping-result"
                data-ping-kind={urlPingState.result.kind}
                className={[
                  "font-mono text-[10px]",
                  urlPingState.result.kind === "ok"
                    ? "text-emerald-300"
                    : urlPingState.result.kind === "error"
                      ? "text-amber-300"
                      : "text-[var(--muted-foreground)]",
                ].join(" ")}
              >
                {urlPingState.result.kind === "ok"
                  ? `URL reachable (HTTP ${urlPingState.result.status ?? "200"})`
                  : urlPingState.result.kind === "skipped"
                    ? "URL ping skipped — opaque CORS response (DNS + TCP succeeded)"
                    : `URL ping failed: ${urlPingState.result.message}`}
              </span>
            ) : null}
          </label>

          <label className="flex flex-col gap-1">
            <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              SHA-256 checksum
            </span>
            <input
              type="text"
              value={formFields.sha256}
              onChange={(e) =>
                setFormFields((f) => ({ ...f, sha256: e.target.value }))
              }
              placeholder="64 lowercase hex chars"
              className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
              data-testid="custom-entry-form-field-sha256"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck="false"
            />
          </label>

          {/* ── License + size ─────────────────────────────────── */}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                License (stored in metadata)
              </span>
              <input
                type="text"
                value={formFields.license}
                onChange={(e) =>
                  setFormFields((f) => ({ ...f, license: e.target.value }))
                }
                placeholder="MIT, Apache-2.0, …"
                className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
                data-testid="custom-entry-form-field-license"
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                Size estimate
              </span>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={formFields.size_bytes}
                  onChange={(e) =>
                    setFormFields((f) => ({ ...f, size_bytes: e.target.value }))
                  }
                  placeholder="100MB / 524288000"
                  className="h-8 flex-1 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)]/60"
                  data-testid="custom-entry-form-field-size-bytes"
                />
                <select
                  value=""
                  onChange={(e) => {
                    const bytes = Number.parseInt(e.target.value, 10)
                    if (Number.isFinite(bytes) && bytes > 0) {
                      setFormFields((f) => ({
                        ...f,
                        size_bytes: String(bytes),
                      }))
                    }
                  }}
                  className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--muted-foreground)]"
                  data-testid="custom-entry-form-field-size-preset"
                  aria-label="Size estimate preset"
                >
                  <option value="">preset…</option>
                  {SIZE_PRESETS.map((p) => (
                    <option key={p.label} value={p.bytes}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </div>
            </label>
          </div>

          {/* ── depends_on multi-select ────────────────────────── */}
          <fieldset
            className="flex flex-col gap-1 rounded border border-[var(--border)] p-2"
            data-testid="custom-entry-form-field-depends-on"
          >
            <legend className="px-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              Depends on
            </legend>
            {dependsOnOptions.length === 0 ? (
              <span
                className="font-mono text-[10px] text-[var(--muted-foreground)]"
                data-testid="custom-entry-form-depends-on-empty"
              >
                No other catalog entries to depend on yet.
              </span>
            ) : (
              <div
                className="flex max-h-40 flex-wrap gap-1 overflow-y-auto"
                data-testid="custom-entry-form-depends-on-list"
              >
                {dependsOnOptions.map((id) => {
                  const checked = formFields.depends_on.includes(id)
                  return (
                    <label
                      key={id}
                      className={[
                        "inline-flex cursor-pointer items-center gap-1 rounded border px-2 py-0.5 font-mono text-[11px]",
                        checked
                          ? "border-[var(--neural-blue)]/55 bg-[var(--neural-blue)]/15 text-[var(--foreground)]"
                          : "border-[var(--border)] bg-[var(--card)]/40 text-[var(--muted-foreground)]",
                      ].join(" ")}
                      data-testid={`custom-entry-form-depends-on-option-${id}`}
                      data-checked={checked ? "true" : "false"}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => handleToggleDependsOn(id)}
                        className="h-3 w-3 cursor-pointer accent-[var(--neural-blue)]"
                        data-testid={`custom-entry-form-depends-on-checkbox-${id}`}
                      />
                      {id}
                    </label>
                  )
                })}
              </div>
            )}
          </fieldset>

          {/* ── Form error banner ──────────────────────────────── */}
          {formError ? (
            <div
              className="flex items-start gap-2 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-2 py-1.5 font-mono text-[11px] text-[var(--critical-red)]"
              data-testid="custom-entry-form-form-error"
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
              data-testid="custom-entry-form-form-cancel"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="inline-flex items-center justify-center gap-1 rounded bg-[var(--neural-blue)]/15 px-3 py-1.5 font-mono text-xs text-[var(--neural-blue)] ring-1 ring-[var(--neural-blue)]/55 hover:bg-[var(--neural-blue)]/25 disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="custom-entry-form-form-submit"
            >
              {submitting ? (
                <Loader2 size={12} aria-hidden className="animate-spin" />
              ) : isEditMode ? (
                <CheckCircle2 size={12} aria-hidden />
              ) : (
                <Plus size={12} aria-hidden />
              )}
              {submitting
                ? isEditMode
                  ? "Saving…"
                  : "Adding…"
                : isEditMode
                  ? "Save changes"
                  : "Add entry"}
            </button>
          </div>
        </form>
      ) : null}

      {/* ── List body ────────────────────────────────────────────── */}
      {rows.length === 0 && !loading ? (
        <div
          className="rounded border border-dashed border-[var(--border)] bg-[var(--card)]/40 p-6 text-center font-mono text-[11px] text-[var(--muted-foreground)]"
          data-testid="custom-entry-form-empty"
        >
          No custom catalog entries yet. Click <span className="text-[var(--foreground)]">Add custom entry</span> to create one.
        </div>
      ) : (
        <ul
          className="flex flex-col divide-y divide-[var(--border)] overflow-hidden rounded border border-[var(--border)]"
          data-testid="custom-entry-form-list"
        >
          {rows.map((entry) => {
            const isDeleting = pendingDeleteId === entry.id
            const isConfirming = confirmDeleteId === entry.id
            return (
              <li
                key={entry.id}
                className="flex flex-col gap-2 bg-[var(--card)]/30 p-3"
                data-testid={`custom-entry-form-row-${entry.id}`}
                data-entry-id={entry.id}
                data-entry-source={entry.source}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span
                      className="truncate font-mono text-xs text-[var(--foreground)]"
                      title={entry.id}
                      data-testid={`custom-entry-form-row-name-${entry.id}`}
                    >
                      {entry.display_name ?? entry.id}
                    </span>
                    <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-[var(--muted-foreground)]">
                      <span data-testid={`custom-entry-form-row-id-${entry.id}`}>
                        {entry.id}
                      </span>
                      <span aria-hidden>·</span>
                      <span data-testid={`custom-entry-form-row-vendor-${entry.id}`}>
                        {entry.vendor ?? "—"}
                      </span>
                      <span aria-hidden>·</span>
                      <span
                        data-testid={`custom-entry-form-row-family-${entry.id}`}
                        data-family={entry.family ?? ""}
                      >
                        {entry.family ? FAMILY_LABEL[entry.family] : "—"}
                      </span>
                      <span aria-hidden>·</span>
                      <span data-testid={`custom-entry-form-row-version-${entry.id}`}>
                        v{entry.version ?? "—"}
                      </span>
                      <span aria-hidden>·</span>
                      <span data-testid={`custom-entry-form-row-size-${entry.id}`}>
                        {formatSizeBytes(entry.size_bytes)}
                      </span>
                      <span aria-hidden>·</span>
                      <span
                        className="rounded border border-[var(--border)] bg-[var(--card)] px-1 py-0.5"
                        data-testid={`custom-entry-form-row-source-${entry.id}`}
                        data-source={entry.source}
                      >
                        {entry.source}
                      </span>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => handleOpenEdit(entry)}
                      disabled={isFormOpen || isDeleting}
                      className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--card)] px-2 py-1 font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
                      data-testid={`custom-entry-form-row-edit-${entry.id}`}
                      aria-label={`Edit ${entry.id}`}
                    >
                      <Edit3 size={12} aria-hidden />
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDeleteRequest(entry)}
                      disabled={isFormOpen || isDeleting || !onRemove}
                      className="inline-flex items-center gap-1 rounded border border-[var(--critical-red)]/45 bg-[var(--critical-red)]/5 px-2 py-1 font-mono text-[11px] text-[var(--critical-red)] hover:bg-[var(--critical-red)]/15 disabled:cursor-not-allowed disabled:opacity-50"
                      data-testid={`custom-entry-form-row-remove-${entry.id}`}
                      aria-label={`Remove ${entry.id}`}
                    >
                      <Trash2 size={12} aria-hidden />
                      Remove
                    </button>
                  </div>
                </div>

                {isConfirming ? (
                  <div
                    className="flex flex-wrap items-center justify-between gap-2 rounded border border-[var(--critical-red)]/55 bg-[var(--critical-red)]/5 px-3 py-2 font-mono text-[11px] text-[var(--critical-red)]"
                    data-testid={`custom-entry-form-row-confirm-${entry.id}`}
                  >
                    <span className="flex items-start gap-2">
                      <AlertTriangle size={12} aria-hidden className="mt-0.5 shrink-0" />
                      Remove this entry? Tenants who already installed it
                      keep the local copy; future installs are blocked.
                    </span>
                    <span className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={handleDeleteCancel}
                        disabled={isDeleting}
                        className="inline-flex items-center rounded border border-[var(--border)] bg-[var(--card)] px-2 py-0.5 text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-50"
                        data-testid={`custom-entry-form-row-confirm-cancel-${entry.id}`}
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDeleteConfirm(entry)}
                        disabled={isDeleting}
                        className="inline-flex items-center gap-1 rounded bg-[var(--critical-red)]/15 px-2 py-0.5 text-[10px] text-[var(--critical-red)] ring-1 ring-[var(--critical-red)]/55 hover:bg-[var(--critical-red)]/25 disabled:cursor-not-allowed disabled:opacity-50"
                        data-testid={`custom-entry-form-row-confirm-delete-${entry.id}`}
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

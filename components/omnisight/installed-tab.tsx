"use client"

/**
 * BS.8.1 — Installed tab list view.
 *
 * Sister surface to BS.6's `<CatalogTab />`. Where catalog-tab browses
 * the *available* entries an operator can install, this row enumerates
 * the entries already deployed onto the current tenant's host so the
 * operator can read the post-install bookkeeping (disk usage / how
 * many workspaces still depend on this entry / when it was last used)
 * and trigger the four common per-row actions (view log / update /
 * reinstall / uninstall) from a single overflow ``⋮`` menu.
 *
 * Caller wiring contract
 * ──────────────────────
 * The component is **purely presentational** — it does not fetch or
 * subscribe. The page wrapper (``app/settings/platforms/page.tsx``)
 * passes:
 *
 *   • ``entries`` — list of installed entries surfaced by the future
 *     ``useInstalledEntries()`` hook (lands in BS.8.2 alongside the
 *     30-day idle scan + cleanup view). Today the page wrapper
 *     supplies an empty array; the empty-state copy spells out that
 *     the data hook is still on the way.
 *   • ``onViewLog`` / ``onUpdate`` / ``onReinstall`` / ``onUninstall``
 *     — handlers that route through the existing BS.7.* PEP-gated
 *     install pipeline. Update + reinstall map onto
 *     ``createInstallJob`` (re-runs the same R20-A HOLD + sidecar);
 *     uninstall maps onto a future ``createUninstallJob`` (BS.8.4
 *     dependency check ships first); view-log shares the same
 *     ``<InstallLogModal />`` the catalog card already opens.
 *
 * Per-row visual contract
 * ───────────────────────
 *   left column ─────────────────
 *     • Display name in `font-orbitron` + family chip (mirrors
 *       catalog-card 5-state palette so a glance from "browse" to
 *       "installed" carries the same colour vocabulary).
 *     • Vendor · v{version} subtitle.
 *     • One-line description (truncated) when supplied.
 *
 *   metric column ───────────────
 *     • Disk usage (reuses ``formatInstallBytes`` so the unit cascade
 *       matches the BS.7.3 install drawer 1:1 — no operator confusion
 *       between "1.2 MB downloaded" and "1.2 MB on disk").
 *     • Used by ``N`` workspace(s) — links nowhere today, but the
 *       count surfaces the dependency footprint so an operator can
 *       eyeball "still in use" before clicking uninstall (BS.8.4
 *       upgrades this into a hard confirm-on-conflict gate).
 *     • Last used — relative duration ("2h ago", "3d ago", "never")
 *       computed via ``formatRelativeDuration`` from the row's
 *       ``lastUsedAt`` ISO timestamp. Surface only — the 30-day idle
 *       scan reads the same field on the backend in BS.8.2.
 *     • Update-available chip when ``updateAvailable=true`` (the
 *       catalog feed's lookahead flagged a newer version) — pulls the
 *       same amber accent the BS.6.2 card uses for state 4.
 *
 *   actions column ──────────────
 *     • ``⋮`` overflow trigger that opens a 4-item Radix dropdown:
 *         · View log    → ``onViewLog(entry)``
 *         · Update      → ``onUpdate(entry)``     (disabled if no update)
 *         · Reinstall   → ``onReinstall(entry)``
 *         · Uninstall   → ``onUninstall(entry)``  (critical-red accent)
 *
 *       Disabled menu items still render so the affordance stays
 *       discoverable — the catalog card / drawer pattern of "hide when
 *       not applicable" doesn't apply here because the operator may
 *       legitimately want to know "I can re-run this install" before
 *       the catalog feed has marked the row update-available.
 *
 * Sort + filter
 * ─────────────
 * BS.8.1 ships the `name-asc / name-desc / size-desc / last-used-desc`
 * sort dropdown. Filter / search are intentionally deferred to a
 * follow-up so this row stays scoped to the list shell — the search
 * input would land in the same toolbar slot the catalog tab uses, and
 * we keep the option open by mirroring the toolbar layout.
 *
 * Module-global state audit (SOP Step 1)
 * ──────────────────────────────────────
 * No module-level mutable state. Sort selection is per-component
 * ``useState``; the dropdown menu is Radix-controlled (open/close
 * state lives inside `<DropdownMenu />` per row, not on this module).
 * Browser-only — uvicorn ``--workers N`` model does not apply (every
 * tab derives the same view from the same SSE / REST snapshot).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Pure presentation. Action callbacks are fired into the page wrapper
 * which owns the API round-trips; this module never observes a
 * write→read race because it never reads from anywhere. Sort is a
 * pure derivation from the entries prop.
 */

import { useCallback, useMemo, useState, type ChangeEvent } from "react"
import {
  AlertTriangle,
  ArrowDownAZ,
  ArrowUpAZ,
  Database,
  Download,
  FileText,
  HardDrive,
  History,
  MoreVertical,
  RefreshCw,
  Trash2,
  Users,
} from "lucide-react"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  CATALOG_FAMILIES,
  type CatalogFamily,
  coerceFamily,
} from "@/components/omnisight/catalog-tab"
import { formatInstallBytes } from "@/components/omnisight/install-progress-drawer"

// ─────────────────────────────────────────────────────────────────────
// Public types — frozen so BS.8.2 / BS.8.7 share one contract.
// ─────────────────────────────────────────────────────────────────────

/** A single installed entry surfaced in the list. Mirrors the fields
 *  the BS.6.2 catalog-card already understands (id / displayName /
 *  vendor / family / version) and adds the post-install bookkeeping
 *  fields BS.8 needs (disk usage / dependency count / last-used / a
 *  catalog-feed-driven update-available flag). The shape stays a plain
 *  TypeScript interface so future BS.8.x rows can extend it in
 *  ``metadata`` without breaking this row's consumers. */
export interface InstalledEntry {
  id: string
  displayName: string
  vendor: string
  family: CatalogFamily
  version?: string
  description?: string
  /** Bytes consumed on the host's catalog/cache volume. ``null`` =
   *  size unknown (e.g. an entry installed before BS.8.2 started
   *  recording disk usage); the row renders ``"—"`` in that case. */
  diskUsageBytes?: number | null
  /** Workspace count currently importing / running this entry. Zero
   *  means the entry is a candidate for cleanup-unused (BS.8.2). */
  usedByWorkspaceCount?: number
  /** ISO timestamp of the last workspace activation that touched this
   *  entry. ``null`` / undefined ⇒ never used since install — same
   *  field BS.8.2's 30-day idle scan reads on the backend. */
  lastUsedAt?: string | null
  /** ISO timestamp the install completed at. Used to default-sort and
   *  for an audit-line tooltip; not rendered on the row by default. */
  installedAt?: string | null
  /** Catalog feed flagged a newer version available — drives the
   *  "Update" menu item enabled state and the amber chip. */
  updateAvailable?: boolean
  /** Optional hint about the freshly-available version (rendered next
   *  to the chip when supplied). */
  availableVersion?: string
  /** Catalog source — same vocabulary `<CatalogEntry />` uses. */
  source?: "shipped" | "operator" | "override"
  /** Open-ended metadata — kept for forward compat per BS ADR §3.4. */
  metadata?: Record<string, unknown>
}

/** Sort keys exposed in the toolbar dropdown. ``last-used-desc`` puts
 *  rows with no ``lastUsedAt`` last so freshly-installed-but-never-run
 *  entries don't unfairly leap to the top. */
export const INSTALLED_TAB_SORTS = [
  "name-asc",
  "name-desc",
  "size-desc",
  "last-used-desc",
] as const
export type InstalledTabSortKey = (typeof INSTALLED_TAB_SORTS)[number]
export const INSTALLED_TAB_DEFAULT_SORT: InstalledTabSortKey = "name-asc"

const SORT_LABEL: Record<InstalledTabSortKey, string> = {
  "name-asc": "Name (A→Z)",
  "name-desc": "Name (Z→A)",
  "size-desc": "Disk usage (large → small)",
  "last-used-desc": "Last used (recent → old)",
}

const SORT_ICON: Record<InstalledTabSortKey, typeof ArrowDownAZ> = {
  "name-asc": ArrowDownAZ,
  "name-desc": ArrowUpAZ,
  "size-desc": HardDrive,
  "last-used-desc": History,
}

/** Family chip palette — kept in sync with `<CatalogTab />`'s palette
 *  so the Installed tab and the Catalog tab share visual vocabulary. */
const FAMILY_LABEL: Record<CatalogFamily, string> = {
  mobile: "Mobile",
  embedded: "Embedded",
  web: "Web",
  software: "Software",
  custom: "Custom",
}

const FAMILY_ACCENT: Record<CatalogFamily, string> = {
  mobile: "border-emerald-500/60 text-emerald-300",
  embedded: "border-amber-500/60 text-amber-300",
  web: "border-sky-500/60 text-sky-300",
  software: "border-violet-500/60 text-violet-300",
  custom: "border-rose-500/60 text-rose-300",
}

export function coerceInstalledTabSort(
  value: string | null | undefined,
): InstalledTabSortKey {
  if (value && (INSTALLED_TAB_SORTS as readonly string[]).includes(value)) {
    return value as InstalledTabSortKey
  }
  return INSTALLED_TAB_DEFAULT_SORT
}

/** Pure sort pipeline. Exported so BS.8.7 unit tests can lock the
 *  ordering contract without RTL. Returns a fresh array (no in-place
 *  mutation of the input). */
export function sortInstalledEntries(
  entries: ReadonlyArray<InstalledEntry>,
  sort: InstalledTabSortKey,
): InstalledEntry[] {
  const out = [...entries]
  out.sort((a, b) => {
    switch (sort) {
      case "name-asc":
        return a.displayName.localeCompare(b.displayName)
      case "name-desc":
        return b.displayName.localeCompare(a.displayName)
      case "size-desc": {
        const av =
          typeof a.diskUsageBytes === "number" && Number.isFinite(a.diskUsageBytes)
            ? a.diskUsageBytes
            : -1
        const bv =
          typeof b.diskUsageBytes === "number" && Number.isFinite(b.diskUsageBytes)
            ? b.diskUsageBytes
            : -1
        if (bv !== av) return bv - av
        return a.displayName.localeCompare(b.displayName)
      }
      case "last-used-desc": {
        const at = a.lastUsedAt ? Date.parse(a.lastUsedAt) : 0
        const bt = b.lastUsedAt ? Date.parse(b.lastUsedAt) : 0
        if (bt !== at) return bt - at
        return a.displayName.localeCompare(b.displayName)
      }
      default:
        return 0
    }
  })
  return out
}

/** Format an ISO timestamp as a short relative duration. Returns
 *  ``"never"`` for null / empty inputs and a coarse "Xs/m/h/d/mo/y
 *  ago" string for a valid timestamp. The precision is intentionally
 *  low — this is a list-view glance, not an audit log; BS.8.2's idle
 *  scan reads the raw timestamp directly. */
export function formatRelativeDuration(
  iso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!iso) return "never"
  const t = Date.parse(iso)
  if (!Number.isFinite(t)) return "never"
  const diff = now.getTime() - t
  if (diff < 0) return "just now"
  const sec = Math.floor(diff / 1000)
  if (sec < 60) return `${sec}s ago`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}h ago`
  const day = Math.floor(hr / 24)
  if (day < 30) return `${day}d ago`
  const mo = Math.floor(day / 30)
  if (mo < 12) return `${mo}mo ago`
  const yr = Math.floor(day / 365)
  return `${yr}y ago`
}

// ─────────────────────────────────────────────────────────────────────
// Public component props.
// ─────────────────────────────────────────────────────────────────────

export type InstalledTabAction = (entry: InstalledEntry) => void

export interface InstalledTabProps {
  /** Installed entries fed by the future BS.8.2 ``useInstalledEntries()``
   *  hook (or by tests / preview hosts). Defaults to empty so the
   *  component renders cleanly before the data hook lands. */
  entries?: ReadonlyArray<InstalledEntry>
  /** Per-row action handlers. Each is optional so a host that hasn't
   *  wired the corresponding pipeline (e.g. uninstall before BS.8.4)
   *  can omit it; the dropdown item then renders disabled rather
   *  than disappearing — keeps the affordance discoverable. */
  onViewLog?: InstalledTabAction
  onUpdate?: InstalledTabAction
  onReinstall?: InstalledTabAction
  onUninstall?: InstalledTabAction
  /** Optional override for the empty-state node (e.g. for a first-run
   *  shimmer or a "loading installed entries" hint). Defaults to the
   *  static "no installed entries" copy. */
  emptyState?: React.ReactNode
  /** Optional `Date.now`-style override used by ``formatRelativeDuration``.
   *  Tests pin this so "2h ago" rendering is deterministic. */
  now?: Date
  className?: string
}

// ─────────────────────────────────────────────────────────────────────
// InstalledTab — main export.
// ─────────────────────────────────────────────────────────────────────

const EMPTY_ENTRIES: ReadonlyArray<InstalledEntry> = []

export function InstalledTab({
  entries = EMPTY_ENTRIES,
  onViewLog,
  onUpdate,
  onReinstall,
  onUninstall,
  emptyState,
  now,
  className,
}: InstalledTabProps) {
  const [sort, setSort] = useState<InstalledTabSortKey>(INSTALLED_TAB_DEFAULT_SORT)

  const onSortChange = useCallback(
    (e: ChangeEvent<HTMLSelectElement>) =>
      setSort(coerceInstalledTabSort(e.target.value)),
    [],
  )

  const sorted = useMemo(() => sortInstalledEntries(entries, sort), [entries, sort])

  const totalCount = entries.length
  const visibleCount = sorted.length
  // Sum disk usage across every row that reports a finite value. Unknown
  // rows are skipped — surfacing "1.2 GB +?" would be more confusing than
  // a low estimate. The footer chip flags the missing-data case via
  // ``data-disk-usage-known`` so BS.8.7 tests can lock the contract.
  const totalDiskBytes = useMemo(() => {
    let total = 0
    let anyKnown = false
    for (const e of entries) {
      if (
        typeof e.diskUsageBytes === "number" &&
        Number.isFinite(e.diskUsageBytes) &&
        e.diskUsageBytes >= 0
      ) {
        total += e.diskUsageBytes
        anyKnown = true
      }
    }
    return anyKnown ? total : null
  }, [entries])

  return (
    <div
      data-testid="installed-tab"
      data-installed-sort={sort}
      data-installed-visible={visibleCount}
      data-installed-total={totalCount}
      className={[
        "flex flex-col gap-4",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {/* ── Toolbar ─────────────────────────────────────────────── */}
      <div
        data-testid="installed-tab-toolbar"
        className="flex flex-col gap-3 rounded-md border border-[var(--border)] bg-[var(--card)]/40 p-3 md:flex-row md:items-center md:justify-between"
      >
        <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
          <Database size={12} aria-hidden />
          <span data-testid="installed-tab-result-count">
            {visibleCount} / {totalCount} installed
          </span>
          <span aria-hidden>·</span>
          <span
            data-testid="installed-tab-disk-total"
            data-disk-usage-known={totalDiskBytes !== null ? "true" : "false"}
          >
            disk · {totalDiskBytes !== null ? formatInstallBytes(totalDiskBytes) : "—"}
          </span>
        </div>

        <div className="flex items-center gap-1">
          <label htmlFor="installed-tab-sort" className="sr-only">
            Sort installed entries
          </label>
          {(() => {
            const Icon = SORT_ICON[sort]
            return (
              <Icon
                size={12}
                aria-hidden
                className="text-[var(--muted-foreground)]"
              />
            )
          })()}
          <select
            id="installed-tab-sort"
            data-testid="installed-tab-sort-select"
            value={sort}
            onChange={onSortChange}
            className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] focus:border-[var(--neural-blue)] focus:outline-none"
          >
            {INSTALLED_TAB_SORTS.map((key) => (
              <option key={key} value={key}>
                {SORT_LABEL[key]}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* ── List body ───────────────────────────────────────────── */}
      {visibleCount === 0 ? (
        <div
          data-testid="installed-tab-empty"
          className="flex min-h-[140px] items-center justify-center rounded-md border border-dashed border-[var(--border)] bg-[var(--card)]/30 p-6 font-mono text-xs text-[var(--muted-foreground)]"
        >
          {emptyState ?? (
            <span>
              No installed entries yet — install something from the Catalog tab
              to see it here.
            </span>
          )}
        </div>
      ) : (
        <ul
          data-testid="installed-tab-list"
          className="flex flex-col divide-y divide-[var(--border)] overflow-hidden rounded-md border border-[var(--border)] bg-[var(--card)]"
        >
          {sorted.map((entry) => (
            <InstalledTabRow
              key={entry.id}
              entry={entry}
              now={now}
              onViewLog={onViewLog}
              onUpdate={onUpdate}
              onReinstall={onReinstall}
              onUninstall={onUninstall}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Sub-components — file-private.
// ─────────────────────────────────────────────────────────────────────

interface InstalledTabRowProps {
  entry: InstalledEntry
  now?: Date
  onViewLog?: InstalledTabAction
  onUpdate?: InstalledTabAction
  onReinstall?: InstalledTabAction
  onUninstall?: InstalledTabAction
}

function InstalledTabRow({
  entry,
  now,
  onViewLog,
  onUpdate,
  onReinstall,
  onUninstall,
}: InstalledTabRowProps) {
  const family = coerceFamily(entry.family)
  const familyLabel = FAMILY_LABEL[family]
  const familyAccent = FAMILY_ACCENT[family]
  const diskLabel = formatInstallBytes(entry.diskUsageBytes ?? null)
  const usedByCount =
    typeof entry.usedByWorkspaceCount === "number" &&
    Number.isFinite(entry.usedByWorkspaceCount) &&
    entry.usedByWorkspaceCount >= 0
      ? entry.usedByWorkspaceCount
      : 0
  const lastUsedLabel = formatRelativeDuration(entry.lastUsedAt ?? null, now)
  const updateAvailable = Boolean(entry.updateAvailable)

  // Wrap each action so menu items can be wired even when the parent
  // omitted a handler (item renders disabled). Capturing the handler
  // through the closure keeps the per-row testid contract stable so
  // BS.8.7 unit tests can poke individual menu items by id.
  const fireViewLog = useCallback(() => {
    if (onViewLog) onViewLog(entry)
  }, [entry, onViewLog])
  const fireUpdate = useCallback(() => {
    if (onUpdate) onUpdate(entry)
  }, [entry, onUpdate])
  const fireReinstall = useCallback(() => {
    if (onReinstall) onReinstall(entry)
  }, [entry, onReinstall])
  const fireUninstall = useCallback(() => {
    if (onUninstall) onUninstall(entry)
  }, [entry, onUninstall])

  return (
    <li
      data-testid={`installed-tab-row-${entry.id}`}
      data-entry-id={entry.id}
      data-entry-family={family}
      data-update-available={updateAvailable ? "true" : "false"}
      className="flex flex-col gap-2 px-4 py-3 md:flex-row md:items-center md:gap-4"
    >
      {/* ── Left: name + vendor + description ─────────────────── */}
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center gap-2">
          <span
            data-testid={`installed-tab-row-name-${entry.id}`}
            className="truncate font-orbitron text-sm tracking-wide text-[var(--foreground)]"
            title={entry.displayName}
          >
            {entry.displayName}
          </span>
          <span
            className={[
              "inline-flex items-center rounded-full border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider",
              familyAccent,
            ].join(" ")}
            data-testid={`installed-tab-row-family-${entry.id}`}
          >
            {familyLabel}
          </span>
          {updateAvailable && (
            <span
              data-testid={`installed-tab-row-update-chip-${entry.id}`}
              className="inline-flex items-center gap-1 rounded-full border border-amber-500/60 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-amber-300"
              title={
                entry.availableVersion
                  ? `Update available — v${entry.availableVersion}`
                  : "Update available"
              }
            >
              <AlertTriangle size={10} aria-hidden />
              update
              {entry.availableVersion ? ` · v${entry.availableVersion}` : ""}
            </span>
          )}
        </div>
        <div className="truncate font-mono text-[10px] text-[var(--muted-foreground)]">
          {entry.vendor}
          {entry.version ? ` · v${entry.version}` : ""}
        </div>
        {entry.description && (
          <div
            className="truncate font-mono text-[10px] text-[var(--muted-foreground)]/80"
            title={entry.description}
          >
            {entry.description}
          </div>
        )}
      </div>

      {/* ── Middle: metric column ─────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-3 font-mono text-[10px] text-[var(--muted-foreground)] md:flex-nowrap md:gap-4">
        <span
          className="inline-flex items-center gap-1 tabular-nums"
          data-testid={`installed-tab-row-disk-${entry.id}`}
          title="Disk usage"
        >
          <HardDrive size={12} aria-hidden />
          {diskLabel}
        </span>
        <span
          className="inline-flex items-center gap-1 tabular-nums"
          data-testid={`installed-tab-row-usedby-${entry.id}`}
          title={`Used by ${usedByCount} workspace${usedByCount === 1 ? "" : "s"}`}
        >
          <Users size={12} aria-hidden />
          {usedByCount} workspace{usedByCount === 1 ? "" : "s"}
        </span>
        <span
          className="inline-flex items-center gap-1"
          data-testid={`installed-tab-row-lastused-${entry.id}`}
          title={
            entry.lastUsedAt
              ? `Last used ${entry.lastUsedAt}`
              : "Never used since install"
          }
        >
          <History size={12} aria-hidden />
          {lastUsedLabel}
        </span>
      </div>

      {/* ── Right: actions ⋮ ──────────────────────────────────── */}
      <div className="flex items-center justify-end">
        <DropdownMenu>
          <DropdownMenuTrigger
            data-testid={`installed-tab-row-actions-${entry.id}`}
            aria-label={`Actions for ${entry.displayName}`}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-[var(--muted-foreground)] hover:border-[var(--border)] hover:text-[var(--foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--neural-blue)]"
          >
            <MoreVertical size={14} aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuLabel className="font-mono text-[10px] uppercase tracking-wider">
              {entry.displayName}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              data-testid={`installed-tab-row-action-viewlog-${entry.id}`}
              onSelect={fireViewLog}
              disabled={!onViewLog}
            >
              <FileText size={12} aria-hidden />
              <span>View log</span>
            </DropdownMenuItem>
            <DropdownMenuItem
              data-testid={`installed-tab-row-action-update-${entry.id}`}
              onSelect={fireUpdate}
              disabled={!onUpdate || !updateAvailable}
            >
              <Download size={12} aria-hidden />
              <span>
                Update
                {entry.availableVersion ? ` · v${entry.availableVersion}` : ""}
              </span>
            </DropdownMenuItem>
            <DropdownMenuItem
              data-testid={`installed-tab-row-action-reinstall-${entry.id}`}
              onSelect={fireReinstall}
              disabled={!onReinstall}
            >
              <RefreshCw size={12} aria-hidden />
              <span>Reinstall</span>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              data-testid={`installed-tab-row-action-uninstall-${entry.id}`}
              onSelect={fireUninstall}
              disabled={!onUninstall}
              variant="destructive"
            >
              <Trash2 size={12} aria-hidden />
              <span>Uninstall</span>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </li>
  )
}

// Re-export the family literal so callers don't double-import from
// catalog-tab when they only need the installed view's vocabulary.
export { CATALOG_FAMILIES as INSTALLED_TAB_FAMILIES }

export default InstalledTab

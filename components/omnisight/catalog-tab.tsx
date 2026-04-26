"use client"

/**
 * BS.6.1 — Platforms catalog tab shell.
 * BS.6.3 — extended with selection state + slide-out-left/slide-in-right
 *          to host `<CatalogDetailPanel />` inline (without leaving the
 *          tab). When the page wrapper supplies `renderDetail`, card
 *          clicks flip `selectedId` and the grid is replaced with the
 *          panel; the panel's `onClose` callback flips back. Hook order
 *          stays stable (selection state is always mounted) so motion
 *          hooks land on the same nodes regardless of selection.
 *
 * Outer container for the `?tab=catalog` panel inside
 * `app/settings/platforms/page.tsx`. This row owns the toolbar surface
 * the rest of the BS.6 epic builds against:
 *
 *   • family filter chips (mobile / embedded / web / software / custom
 *     + an "all" reset chip) — BS.6.4 will re-skin them with the
 *     polished `<CategoryStrip />` (corner brackets + fui-scan-sweep)
 *     while keeping the same filter state contract so swap is a render
 *     change only.
 *   • search input — substring match against `displayName`, `vendor`,
 *     and `id` (case-insensitive).
 *   • sort dropdown — name asc/desc, vendor asc, recently-updated desc.
 *   • density toggle — compact / comfortable / spacious, persisted
 *     per-(tenant, user) via `useUserStorage` so operators see the same
 *     density across reloads + browser sessions on the same machine.
 *
 * The card grid itself is ship-empty for BS.6.1: the toolbar applies
 * filter / search / sort transforms to the entries prop, computes a
 * post-filter count + density-driven grid layout, and hands the result
 * to `renderCard` (defaulting to a `<CatalogCardPlaceholder />`). BS.6.2
 * lands the real `<CatalogCard />` and BS.6.6 layers in the 8-layer
 * motion library — both consume the same `CatalogEntry` shape exported
 * from this module so we don't double-define the contract.
 *
 * Frozen here for BS.6.2-6.7 to consume:
 *   • `CatalogEntry` interface — minimal shape covering the fields the
 *     toolbar needs to filter / sort / render placeholder cards. Vendor-
 *     specific extensions land in `metadata` JSONB per the BS ADR §3
 *     three-source model and surface unchanged through this module.
 *   • `CATALOG_FAMILIES` const tuple + `CatalogFamily` literal union.
 *   • `CATALOG_DENSITIES` + `CatalogDensity`.
 *   • `CATALOG_SORTS` + `CatalogSortKey`.
 *   • `CATALOG_DENSITY_STORAGE_KEY` — single source-of-truth for the
 *     `useUserStorage` key so BS.6.6 + BS.6.8 tests don't hardcode it.
 *   • `filterAndSortEntries()` — pure helper; BS.6.8 unit tests can
 *     lock the filter / sort behaviour without RTL.
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. All filter / search / sort / density
 * lives in component-local React state (`useState` + `useUserStorage`).
 * `useUserStorage` writes to per-(tenant, user) prefixed `localStorage`
 * keys; cross-tab sync flows through the `storage` event handler that
 * `lib/storage.ts` already mounts. Browser-only — uvicorn `--workers N`
 * model does not apply (answer #1: each tab derives the same view from
 * the same Next.js build artifact + the same persisted preference).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Density writes call `setItem` synchronously and React state updates
 * via `useSyncExternalStore` on the next tick — no network round-trip,
 * no cross-worker race. Filter / search / sort are pure derivations of
 * state and the entries prop; no API calls inside this component (the
 * actual catalog fetch lands in BS.6.5/BS.7's `useCatalog()` hook).
 */

import {
  type ChangeEvent,
  type ReactNode,
  useCallback,
  useId,
  useMemo,
  useState,
} from "react"
import {
  ArrowDownAZ,
  ArrowUpAZ,
  Building2,
  Clock,
  LayoutGrid,
  Rows3,
  Rows4,
  Search,
  X,
} from "lucide-react"

import { useEffectiveMotionLevel } from "@/hooks/use-zero-g"
import { useUserStorage } from "@/lib/storage"

// ─────────────────────────────────────────────────────────────────────
// Public types — frozen so BS.6.2..BS.6.7 + BS.6.8 share one contract.
// ─────────────────────────────────────────────────────────────────────

/** Catalog entry families per BS ADR §7.1 `catalog_entries.family`. The
 *  literal-union form keeps switch-cases exhaustive at compile time
 *  while allowing entries with unknown families (subscription feeds,
 *  legacy seeds) to render under the "custom" bucket without being
 *  silently dropped — `coerceFamily()` handles the fallback. */
export const CATALOG_FAMILIES = [
  "mobile",
  "embedded",
  "web",
  "software",
  "custom",
] as const
export type CatalogFamily = (typeof CATALOG_FAMILIES)[number]

/** Lifecycle states the catalog card surfaces. The five states map 1:1
 *  to BS.6.2's visual contract; BS.6.1 only needs them to filter +
 *  render placeholders. `available` is the catalog default. */
export const CATALOG_INSTALL_STATES = [
  "available",
  "installed",
  "installing",
  "update-available",
  "failed",
] as const
export type CatalogInstallState = (typeof CATALOG_INSTALL_STATES)[number]

export interface CatalogEntry {
  /** Stable unique id (matches PG `catalog_entries.id`). */
  id: string
  /** Operator-facing display name. */
  displayName: string
  /** Vendor / publisher (NXP, Google, Apple, Yocto Project, …). */
  vendor: string
  /** Family bucket — drives filter chips. */
  family: CatalogFamily
  /** Version tag rendered in the card. Optional because operator-built
   *  custom entries may omit it during draft-edit. */
  version?: string
  /** Install lifecycle state — drives BS.6.2 card variant. Defaults to
   *  `available` when omitted (BS.7 install pipeline writes the rest). */
  installState?: CatalogInstallState
  /** Short description / tagline; rendered by the BS.6.2 card. */
  description?: string
  /** Last-updated ISO timestamp (per `catalog_entries.updated_at`). Used
   *  by the "recently updated" sort. */
  updatedAt?: string
  /** Catalog source — `shipped` / `operator` / `override`. Surfaced as
   *  a chip on the card; BS.6.1 only needs it for telemetry. */
  source?: "shipped" | "operator" | "override"
  /** Open-ended vendor-specific metadata (BS ADR §3.4 forward-compat
   *  rule — JSONB column always allows unknown keys). */
  metadata?: Record<string, unknown>
}

/** Density buckets — matches the spec's `compact / comfortable /
 *  spacious` triplet. Persisted as the raw string in `localStorage`. */
export const CATALOG_DENSITIES = ["compact", "comfortable", "spacious"] as const
export type CatalogDensity = (typeof CATALOG_DENSITIES)[number]
export const CATALOG_DEFAULT_DENSITY: CatalogDensity = "comfortable"

/** Sort keys exposed in the toolbar dropdown. Recently-updated puts
 *  entries with no `updatedAt` last so freshly-seeded operator drafts
 *  don't pollute the top of the list. */
export const CATALOG_SORTS = [
  "name-asc",
  "name-desc",
  "vendor-asc",
  "recent-desc",
] as const
export type CatalogSortKey = (typeof CATALOG_SORTS)[number]
export const CATALOG_DEFAULT_SORT: CatalogSortKey = "name-asc"

/** Storage key (un-prefixed) used by `useUserStorage`. The hook adds
 *  the `omnisight:{tenantId}:{userId}:` namespace so this string is
 *  the user-visible suffix only. */
export const CATALOG_DENSITY_STORAGE_KEY = "settings:platforms:catalog:density"

// ─────────────────────────────────────────────────────────────────────
// Filter / sort helpers — exported so BS.6.8 can lock behaviour
// without RTL.
// ─────────────────────────────────────────────────────────────────────

/** Coerce an arbitrary `family` string into a known bucket. Unknown
 *  families collapse to "custom" so subscription-feed entries with
 *  novel taxonomies stay visible (per BS ADR §3.4 forward-compat). */
export function coerceFamily(value: string | undefined | null): CatalogFamily {
  if (value && (CATALOG_FAMILIES as readonly string[]).includes(value)) {
    return value as CatalogFamily
  }
  return "custom"
}

/** Coerce a persisted-string density to a known bucket. Used when
 *  reading from `localStorage` — corrupted / legacy values fall back
 *  to the default rather than throwing or rendering an empty grid. */
export function coerceDensity(
  value: string | null | undefined,
): CatalogDensity {
  if (value && (CATALOG_DENSITIES as readonly string[]).includes(value)) {
    return value as CatalogDensity
  }
  return CATALOG_DEFAULT_DENSITY
}

export function coerceSort(value: string | null | undefined): CatalogSortKey {
  if (value && (CATALOG_SORTS as readonly string[]).includes(value)) {
    return value as CatalogSortKey
  }
  return CATALOG_DEFAULT_SORT
}

interface FilterAndSortInput {
  entries: ReadonlyArray<CatalogEntry>
  family: CatalogFamily | "all"
  search: string
  sort: CatalogSortKey
}

/** Pure filter + sort pipeline. Returns a fresh array (no mutation of
 *  the input) so React rendering is referentially stable and tests can
 *  call it on the same input twice without surprises. */
export function filterAndSortEntries({
  entries,
  family,
  search,
  sort,
}: FilterAndSortInput): CatalogEntry[] {
  const needle = search.trim().toLowerCase()
  const filtered = entries.filter((e) => {
    if (family !== "all" && coerceFamily(e.family) !== family) return false
    if (!needle) return true
    return (
      e.displayName.toLowerCase().includes(needle) ||
      e.vendor.toLowerCase().includes(needle) ||
      e.id.toLowerCase().includes(needle)
    )
  })
  const sorted = [...filtered].sort((a, b) => {
    switch (sort) {
      case "name-asc":
        return a.displayName.localeCompare(b.displayName)
      case "name-desc":
        return b.displayName.localeCompare(a.displayName)
      case "vendor-asc": {
        const v = a.vendor.localeCompare(b.vendor)
        return v !== 0 ? v : a.displayName.localeCompare(b.displayName)
      }
      case "recent-desc": {
        // Entries without `updatedAt` sink to the bottom (treat missing
        // timestamp as "infinitely old") so freshly-seeded drafts with
        // no audit trail don't unfairly leap the list.
        const at = a.updatedAt ? Date.parse(a.updatedAt) : 0
        const bt = b.updatedAt ? Date.parse(b.updatedAt) : 0
        if (bt !== at) return bt - at
        return a.displayName.localeCompare(b.displayName)
      }
      default:
        return 0
    }
  })
  return sorted
}

// ─────────────────────────────────────────────────────────────────────
// Density → grid mapping. Tailwind `grid-cols-*` literals must be
// statically present so the JIT picks them up; we keep the mapping in
// a const lookup rather than building class names at runtime.
// ─────────────────────────────────────────────────────────────────────

const DENSITY_GRID: Record<CatalogDensity, string> = {
  compact: "grid-cols-1 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5",
  comfortable: "grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4",
  spacious: "grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-2 xl:grid-cols-3",
}

const DENSITY_CARD_PADDING: Record<CatalogDensity, string> = {
  compact: "p-2 text-[11px]",
  comfortable: "p-3 text-xs",
  spacious: "p-4 text-sm",
}

const DENSITY_LABEL: Record<CatalogDensity, string> = {
  compact: "Compact",
  comfortable: "Comfortable",
  spacious: "Spacious",
}

const DENSITY_ICON: Record<CatalogDensity, typeof LayoutGrid> = {
  compact: LayoutGrid,
  comfortable: Rows4,
  spacious: Rows3,
}

const FAMILY_LABEL: Record<CatalogFamily, string> = {
  mobile: "Mobile",
  embedded: "Embedded",
  web: "Web",
  software: "Software",
  custom: "Custom",
}

/** Family chip palette — picks the matching OmniSight accent each
 *  bucket already uses elsewhere in the dashboard. BS.6.4 will re-skin
 *  these inside the polished `<CategoryStrip />`; the chip pills here
 *  are the accessible fallback used until that lands. */
const FAMILY_ACCENT: Record<CatalogFamily, string> = {
  mobile: "border-emerald-500/60 text-emerald-300",
  embedded: "border-amber-500/60 text-amber-300",
  web: "border-sky-500/60 text-sky-300",
  software: "border-violet-500/60 text-violet-300",
  custom: "border-rose-500/60 text-rose-300",
}

const SORT_LABEL: Record<CatalogSortKey, string> = {
  "name-asc": "Name (A→Z)",
  "name-desc": "Name (Z→A)",
  "vendor-asc": "Vendor (A→Z)",
  "recent-desc": "Recently updated",
}

const SORT_ICON: Record<CatalogSortKey, typeof ArrowDownAZ> = {
  "name-asc": ArrowDownAZ,
  "name-desc": ArrowUpAZ,
  "vendor-asc": Building2,
  "recent-desc": Clock,
}

// ─────────────────────────────────────────────────────────────────────
// Public component props.
// ─────────────────────────────────────────────────────────────────────

export interface CatalogTabRenderContext {
  entry: CatalogEntry
  density: CatalogDensity
  /** The Tailwind padding/text class derived from the density — used by
   *  the placeholder card and by BS.6.2's `<CatalogCard />` so density
   *  affects every card variant uniformly. */
  cardPaddingClass: string
  /** Card click handler — present when BS.6.3's detail panel is wired
   *  via `renderDetail`, omitted otherwise. The page wrapper plumbs
   *  this into `<CatalogCard onSelect={...} />` so clicking a card
   *  flips selection state in `<CatalogTab />` and slides the panel
   *  in. Optional so BS.6.1/6.2 preview hosts (no detail panel) leave
   *  the card non-interactive without writing extra glue. */
  onSelect?: () => void
}

export interface CatalogTabProps {
  /** Catalog entries fed by BS.6.5's `useCatalog()` hook (or by a
   *  caller-owned mock during BS.6.1's standalone preview). Defaults to
   *  empty so the toolbar still mounts cleanly during BS.6.1 testing. */
  entries?: ReadonlyArray<CatalogEntry>
  /** Optional override for the card renderer. BS.6.2 will inject its
   *  real `<CatalogCard />`; the default is a placeholder skeleton so
   *  the toolbar can be exercised before the card lands. */
  renderCard?: (ctx: CatalogTabRenderContext) => ReactNode
  /** Optional override for the detail panel. BS.6.3 lands
   *  `<CatalogDetailPanel />` here via this prop so when the page
   *  wrapper provides it, clicking a card slides the grid out and
   *  swaps in the panel. When omitted (BS.6.1/6.2 preview), card
   *  clicks are no-ops and the grid stays mounted. The `onClose`
   *  callback dismisses the panel and re-mounts the grid. */
  renderDetail?: (ctx: CatalogTabDetailRenderContext) => ReactNode
  /** Override the empty-state node (e.g. for first-run / loading
   *  shimmers). Defaults to a small "no matches" message. */
  emptyState?: ReactNode
  className?: string
}

export interface CatalogTabDetailRenderContext {
  entry: CatalogEntry
  onClose: () => void
}

// ─────────────────────────────────────────────────────────────────────
// CatalogTab — main export.
// ─────────────────────────────────────────────────────────────────────

const EMPTY_ENTRIES: ReadonlyArray<CatalogEntry> = []

export function CatalogTab({
  entries = EMPTY_ENTRIES,
  renderCard,
  renderDetail,
  emptyState,
  className,
}: CatalogTabProps) {
  const searchInputId = useId()
  const sortInputId = useId()

  const [familyFilter, setFamilyFilter] = useState<CatalogFamily | "all">("all")
  const [search, setSearch] = useState("")
  const [sort, setSort] = useState<CatalogSortKey>(CATALOG_DEFAULT_SORT)

  // BS.6.3 — selection state. Hook is mounted unconditionally so hook
  // order stays stable; when `renderDetail` is omitted (BS.6.1/6.2
  // preview), card clicks are wired to a no-op so the selection state
  // never flips — the grid stays mounted.
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const motionLevel = useEffectiveMotionLevel()
  const reducedMotion = motionLevel === "off" || motionLevel === "subtle"

  const detailEnabled = Boolean(renderDetail)
  const handleSelectEntry = useCallback(
    (entry: CatalogEntry) => {
      if (!detailEnabled) return
      setSelectedId(entry.id)
    },
    [detailEnabled],
  )
  const handleCloseDetail = useCallback(() => setSelectedId(null), [])

  // Density is the only piece of state we persist across reloads.
  // `useUserStorage` returns a `[value, setter]` keyed by the
  // (tenant, user) tuple; we pass-through the raw string and coerce on
  // read so corrupted / legacy values fall back to comfortable.
  const [persistedDensity, persistDensity] = useUserStorage(
    CATALOG_DENSITY_STORAGE_KEY,
  )
  const density = useMemo(() => coerceDensity(persistedDensity), [persistedDensity])
  const onSelectDensity = useCallback(
    (next: CatalogDensity) => {
      if (next === density) return
      persistDensity(next)
    },
    [density, persistDensity],
  )

  const onSearchChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setSearch(e.target.value),
    [],
  )
  const onClearSearch = useCallback(() => setSearch(""), [])
  const onSortChange = useCallback(
    (e: ChangeEvent<HTMLSelectElement>) =>
      setSort(coerceSort(e.target.value)),
    [],
  )

  const visible = useMemo(
    () => filterAndSortEntries({ entries, family: familyFilter, search, sort }),
    [entries, familyFilter, search, sort],
  )

  const totalCount = entries.length
  const visibleCount = visible.length
  const cardPaddingClass = DENSITY_CARD_PADDING[density]
  const gridClass = DENSITY_GRID[density]

  const renderEntryCard =
    renderCard ??
    ((ctx: CatalogTabRenderContext) => (
      <CatalogCardPlaceholder
        entry={ctx.entry}
        density={ctx.density}
        cardPaddingClass={ctx.cardPaddingClass}
      />
    ))

  // BS.6.3 — resolve the currently-selected entry against the live
  // entries list. We resolve by id (not by holding the entry object)
  // so when the parent re-fetches entries with fresh metadata
  // (progress%, audit timeline, etc.) the panel re-renders against
  // the latest snapshot without needing to re-pin the selection.
  const selectedEntry = useMemo(
    () =>
      selectedId == null
        ? null
        : entries.find((e) => e.id === selectedId) ?? null,
    [entries, selectedId],
  )
  const detailOpen = Boolean(selectedEntry && renderDetail)

  // Slide animation classes. `tw-animate-css` (already imported in
  // app/globals.css) ships `animate-in slide-in-from-left-8` and
  // `animate-out slide-out-to-left-8`. Reduced-motion users get an
  // instant cross-fade to honour BS ADR §6.
  const gridAnimClass = reducedMotion
    ? "animate-in fade-in-0 duration-150"
    : "animate-in slide-in-from-left-8 fade-in-0 duration-300"

  return (
    <div
      data-testid="catalog-tab"
      data-catalog-density={density}
      data-catalog-family={familyFilter}
      data-catalog-sort={sort}
      data-catalog-visible={visibleCount}
      data-catalog-total={totalCount}
      data-catalog-detail-open={detailOpen ? "true" : "false"}
      data-catalog-selected-id={selectedEntry?.id ?? ""}
      data-catalog-motion-level={motionLevel}
      className={["flex flex-col gap-4", className ?? ""]
        .filter(Boolean)
        .join(" ")}
    >
      {/* ── Toolbar ─────────────────────────────────────────────── */}
      <div
        data-testid="catalog-tab-toolbar"
        className="flex flex-col gap-3 rounded-md border border-[var(--border)] bg-[var(--card)]/40 p-3 md:flex-row md:items-center md:justify-between"
      >
        {/* Left: family chips */}
        <div
          data-testid="catalog-tab-family-chips"
          role="group"
          aria-label="Filter by family"
          className="flex flex-wrap items-center gap-1"
        >
          <FamilyChip
            id="all"
            label="All"
            active={familyFilter === "all"}
            accentClass="border-[var(--neural-blue)]/60 text-[var(--neural-blue)]"
            onSelect={() => setFamilyFilter("all")}
          />
          {CATALOG_FAMILIES.map((fam) => (
            <FamilyChip
              key={fam}
              id={fam}
              label={FAMILY_LABEL[fam]}
              active={familyFilter === fam}
              accentClass={FAMILY_ACCENT[fam]}
              onSelect={() => setFamilyFilter(fam)}
            />
          ))}
        </div>

        {/* Right: search + sort + density */}
        <div className="flex flex-wrap items-center gap-2 md:flex-nowrap">
          {/* Search */}
          <div className="relative">
            <label htmlFor={searchInputId} className="sr-only">
              Search catalog entries
            </label>
            <Search
              size={12}
              aria-hidden
              className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[var(--muted-foreground)]"
            />
            <input
              id={searchInputId}
              data-testid="catalog-tab-search-input"
              type="search"
              value={search}
              onChange={onSearchChange}
              placeholder="Search name / vendor / id"
              className="h-8 w-48 rounded-md border border-[var(--border)] bg-[var(--background)] pl-7 pr-7 font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:border-[var(--neural-blue)] focus:outline-none"
            />
            {search && (
              <button
                type="button"
                data-testid="catalog-tab-search-clear"
                aria-label="Clear search"
                onClick={onClearSearch}
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
              >
                <X size={12} />
              </button>
            )}
          </div>

          {/* Sort */}
          <div className="flex items-center gap-1">
            <label htmlFor={sortInputId} className="sr-only">
              Sort entries
            </label>
            {(() => {
              const SortIcon = SORT_ICON[sort]
              return (
                <SortIcon
                  size={12}
                  aria-hidden
                  className="text-[var(--muted-foreground)]"
                />
              )
            })()}
            <select
              id={sortInputId}
              data-testid="catalog-tab-sort-select"
              value={sort}
              onChange={onSortChange}
              className="h-8 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 font-mono text-xs text-[var(--foreground)] focus:border-[var(--neural-blue)] focus:outline-none"
            >
              {CATALOG_SORTS.map((key) => (
                <option key={key} value={key}>
                  {SORT_LABEL[key]}
                </option>
              ))}
            </select>
          </div>

          {/* Density toggle */}
          <div
            role="group"
            aria-label="Card density"
            data-testid="catalog-tab-density-group"
            className="inline-flex items-center overflow-hidden rounded-md border border-[var(--border)]"
          >
            {CATALOG_DENSITIES.map((d) => {
              const Icon = DENSITY_ICON[d]
              const active = d === density
              return (
                <button
                  key={d}
                  type="button"
                  data-testid={`catalog-tab-density-${d}`}
                  data-active={active}
                  aria-pressed={active}
                  aria-label={`Density: ${DENSITY_LABEL[d]}`}
                  title={DENSITY_LABEL[d]}
                  onClick={() => onSelectDensity(d)}
                  className={[
                    "inline-flex h-8 w-8 items-center justify-center transition-colors",
                    active
                      ? "bg-[var(--neural-blue)]/15 text-[var(--neural-blue)]"
                      : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
                  ].join(" ")}
                >
                  <Icon size={14} />
                </button>
              )
            })}
          </div>
        </div>
      </div>

      {/* ── Result summary line ─────────────────────────────────── */}
      <div
        data-testid="catalog-tab-summary"
        className="flex items-center justify-between font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]"
      >
        <span data-testid="catalog-tab-result-count">
          {visibleCount} / {totalCount} entries
        </span>
        <span>density · {DENSITY_LABEL[density]}</span>
      </div>

      {/* ── Body: detail panel (when an entry is selected) OR card grid */}
      {detailOpen && selectedEntry && renderDetail ? (
        <div
          data-testid="catalog-tab-detail-slot"
          data-entry-id={selectedEntry.id}
          // The panel itself owns its slide-in-right + fade animation
          // (see `<CatalogDetailPanel />` BS.6.3). The slot only needs
          // a stable wrapper so React keeps the panel re-mounted when
          // the selected id changes — keying by id forces a remount on
          // selection swap so the slide replays each time.
          key={selectedEntry.id}
        >
          {renderDetail({ entry: selectedEntry, onClose: handleCloseDetail })}
        </div>
      ) : visibleCount === 0 ? (
        <div
          data-testid="catalog-tab-empty"
          className={[
            "flex min-h-[140px] items-center justify-center rounded-md border border-dashed border-[var(--border)] bg-[var(--card)]/30 p-6 font-mono text-xs text-[var(--muted-foreground)]",
            gridAnimClass,
          ].join(" ")}
        >
          {emptyState ?? (totalCount === 0
            ? "No catalog entries yet — BS.6.5 + BS.7 will plumb live data."
            : "No entries match the current filters.")}
        </div>
      ) : (
        <div
          data-testid="catalog-tab-grid"
          data-grid-density={density}
          className={["grid", gridClass, gridAnimClass].join(" ")}
        >
          {visible.map((entry) => (
            <div
              key={entry.id}
              data-testid={`catalog-tab-card-slot-${entry.id}`}
              data-entry-id={entry.id}
              data-entry-family={coerceFamily(entry.family)}
            >
              {renderEntryCard({
                entry,
                density,
                cardPaddingClass,
                onSelect: detailEnabled
                  ? () => handleSelectEntry(entry)
                  : undefined,
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Sub-components — file-private. The placeholder is intentionally
// minimal so it can't be mistaken for the production card BS.6.2 ships.
// ─────────────────────────────────────────────────────────────────────

interface FamilyChipProps {
  id: CatalogFamily | "all"
  label: string
  active: boolean
  accentClass: string
  onSelect: () => void
}

function FamilyChip({ id, label, active, accentClass, onSelect }: FamilyChipProps) {
  return (
    <button
      type="button"
      data-testid={`catalog-tab-family-chip-${id}`}
      data-active={active}
      aria-pressed={active}
      onClick={onSelect}
      className={[
        "inline-flex h-7 items-center gap-1 rounded-full border px-2.5 font-mono text-[10px] uppercase tracking-wider transition-colors",
        active
          ? `${accentClass} bg-[var(--card)]`
          : "border-[var(--border)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
      ].join(" ")}
    >
      {label}
    </button>
  )
}

interface CatalogCardPlaceholderProps {
  entry: CatalogEntry
  density: CatalogDensity
  cardPaddingClass: string
}

/** Skeleton card rendered until BS.6.2's `<CatalogCard />` lands. Keeps
 *  enough surface (name + vendor + version + family + state) for
 *  operators to read the catalog while the polished card is in flight,
 *  and lets BS.6.1 deploy immediately rather than waiting on BS.6.2. */
function CatalogCardPlaceholder({
  entry,
  cardPaddingClass,
}: CatalogCardPlaceholderProps) {
  const fam = coerceFamily(entry.family)
  const state = entry.installState ?? "available"
  return (
    <div
      data-testid={`catalog-tab-card-placeholder-${entry.id}`}
      data-entry-state={state}
      className={[
        "flex h-full flex-col justify-between rounded-md border border-[var(--border)] bg-[var(--card)] font-mono text-[var(--foreground)]",
        cardPaddingClass,
      ].join(" ")}
    >
      <div>
        <div className="truncate font-orbitron tracking-wide">
          {entry.displayName}
        </div>
        <div className="mt-0.5 truncate text-[10px] text-[var(--muted-foreground)]">
          {entry.vendor}
          {entry.version ? ` · v${entry.version}` : ""}
        </div>
      </div>
      <div className="mt-2 flex items-center justify-between">
        <span
          className={[
            "inline-flex items-center rounded-full border px-1.5 py-0.5 text-[9px] uppercase tracking-wider",
            FAMILY_ACCENT[fam],
          ].join(" ")}
        >
          {FAMILY_LABEL[fam]}
        </span>
        <span className="text-[9px] uppercase tracking-wider text-[var(--muted-foreground)]">
          {state}
        </span>
      </div>
    </div>
  )
}

export default CatalogTab

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
 * BS.6.5 — windowed virtualization via `@tanstack/react-virtual`'s
 *          `useVirtualizer`. When the visible-after-filter list grows
 *          past `CATALOG_VIRTUALIZATION_THRESHOLD` rows the grid swaps
 *          to a scoped scroll container (`max-h-[70vh] overflow-auto`)
 *          and only the rows whose `translateY` falls inside the
 *          container's viewport (+ overscan) get rendered into the DOM.
 *          The outer testid `catalog-tab-grid` is preserved for both
 *          the virtualized and the static path so existing selectors
 *          keep resolving; the per-card `catalog-tab-card-slot-{id}`
 *          testid is also preserved (just nested an extra row level).
 *          A `disableVirtualization` opt-out prop keeps BS.6.8 tests
 *          and any preview / Storybook host able to force the static
 *          DOM when jsdom's missing layout engine would otherwise hide
 *          every card.
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
 *     server-of-record via the J4 `user_preferences` API (BS.11.4
 *     `useUserDensityPreference` hook) so operators see the same
 *     density across reloads, browser sessions, AND devices. Same-tab
 *     writes refresh sibling consumers via the
 *     `omnisight:density-pref-changed` event bus; cross-device sync
 *     flows through `preferences.updated` SSE emitted by the backend
 *     router on PUT.
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
 *   • `CATALOG_DENSITY_STORAGE_KEY` — historic localStorage key
 *     (kept exported for backward-compat with any caller still
 *     reading the legacy entry). BS.11.4 moved the SoT to the J4
 *     `user_preferences` row keyed `catalog_density`; the lib
 *     `lib/density-preferences.ts` owns that key as
 *     `DENSITY_PREFERENCE_KEY`.
 *   • `filterAndSortEntries()` — pure helper; BS.6.8 unit tests can
 *     lock the filter / sort behaviour without RTL.
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. All filter / search / sort / density
 * lives in component-local React state. BS.11.4 moved the density
 * source-of-record to the J4 `user_preferences` API
 * (`useUserDensityPreference` hook); cross-tab / cross-device sync
 * flows through `preferences.updated` SSE emitted by the backend
 * router on PUT, plus the same-tab `omnisight:density-pref-changed`
 * event bus for instant in-tab refresh. Browser-only — uvicorn
 * `--workers N` model does not apply (answer #1: each tab derives
 * the same view from the same Next.js build artifact + the same
 * persisted PG row).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * Density writes call `setUserPreference` (a single PG UPSERT
 * wrapped in `setDensityPreference`) and apply optimistically to
 * local React state before the await completes. On API rejection
 * the hook restores the prior value — no race window where a
 * sibling consumer sees a value the server later rejected. Filter
 * / search / sort are pure derivations of state and the entries
 * prop; no API calls inside this component (the actual catalog
 * fetch lands in BS.6.5/BS.7's `useCatalog()` hook).
 */

import {
  type ChangeEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
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
import { useVirtualizer } from "@tanstack/react-virtual"

import { CategoryStrip } from "@/components/omnisight/category-strip"
import {
  useEffectiveMotionLevel,
  useScrollParallax,
} from "@/hooks/use-zero-g"
import { useUserDensityPreference } from "@/hooks/use-user-density-preference"

// ─────────────────────────────────────────────────────────────────────
// Public types — frozen so BS.6.2..BS.6.7 + BS.6.8 share one contract.
// ─────────────────────────────────────────────────────────────────────

/** Catalog entry families per BS ADR §7.1 `catalog_entries.family`. The
 *  literal-union form keeps switch-cases exhaustive at compile time
 *  while allowing entries with unknown families (subscription feeds,
 *  legacy seeds) to render under the "custom" bucket without being
 *  silently dropped — `coerceFamily()` handles the fallback.
 *
 *  Defined in `lib/catalog-families.ts` (a leaf module with no other
 *  imports) so `category-strip.tsx` can read the constant without
 *  participating in the catalog-tab ↔ category-strip ESM evaluation
 *  cycle. Re-exported here to preserve the historic
 *  `import { CATALOG_FAMILIES } from "@/components/omnisight/catalog-tab"`
 *  surface. */
export { CATALOG_FAMILIES, type CatalogFamily } from "@/lib/catalog-families"
import { CATALOG_FAMILIES, type CatalogFamily } from "@/lib/catalog-families"

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

/** Historic `useUserStorage` key used before BS.11.4 migrated the
 *  density source-of-record to the J4 `user_preferences` API. Kept
 *  exported so any external caller still reading the legacy
 *  localStorage entry resolves the same suffix. New code should use
 *  `DENSITY_PREFERENCE_KEY` from `lib/density-preferences.ts`. */
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

/** BS.11.3 — coerce an arbitrary install-state string to a known bucket
 *  inside the catalog-tab module. Mirrors `coerceInstallState` from
 *  `catalog-card.tsx` but kept local so the aria-live announcer can run
 *  without pulling a circular dependency on the card module. */
function coerceCatalogInstallState(
  value: string | null | undefined,
): CatalogInstallState {
  if (
    value &&
    (CATALOG_INSTALL_STATES as readonly string[]).includes(value)
  ) {
    return value as CatalogInstallState
  }
  return "available"
}

/** BS.11.3 — render a screen-reader announcement for a single
 *  catalog-entry install-state transition. Returns `null` when the
 *  transition does not need to be announced (no change, or the operator
 *  walked back to the benign `available` baseline). Exported so unit
 *  tests can lock every branch and so consumers in BS.10 / BS.7 can
 *  reuse the phrasing if they want a separate live region. Pure — no
 *  React, no listener, safe under SSR. */
export function buildCatalogStateAnnouncement(
  entry: CatalogEntry,
  prev: CatalogInstallState | undefined,
  next: CatalogInstallState,
): string | null {
  if (prev === next) return null
  if (next === "available") return null
  switch (next) {
    case "installing": {
      if (prev === "failed") return `${entry.displayName} install retry started`
      if (prev === "update-available") return `${entry.displayName} update started`
      return `${entry.displayName} install started`
    }
    case "installed":
      return `${entry.displayName} installed successfully`
    case "failed": {
      const reason = entry.metadata?.failureReason
      if (typeof reason === "string" && reason.length > 0) {
        return `${entry.displayName} install failed — ${reason}`
      }
      return `${entry.displayName} install failed`
    }
    case "update-available": {
      const nv = entry.metadata?.nextVersion
      if (typeof nv === "string" && nv.length > 0) {
        return `Update available for ${entry.displayName} — version ${nv}`
      }
      return `Update available for ${entry.displayName}`
    }
    default:
      return null
  }
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

// ─────────────────────────────────────────────────────────────────────
// BS.6.5 — virtualization tuning. The catalog grid switches to a
// row-windowed layout once the post-filter visible count crosses the
// threshold; below that, the static `display: grid` layout still wins
// (zero overhead, simplest DOM, kindest to BS.6.8 RTL queries).
// ─────────────────────────────────────────────────────────────────────

/** Visible-row count at which we engage `useVirtualizer`. Tuned so the
 *  static path stays cheap for short catalogs (operator demos, shipped-
 *  only feed) and the windowed path kicks in once a real subscription
 *  feed lands hundreds of entries. The number is a row count — at the
 *  widest breakpoint each row holds 3..5 cards depending on density,
 *  so this is roughly "more than 10..16 cards". */
export const CATALOG_VIRTUALIZATION_THRESHOLD = 4

/** Rows to keep mounted above + below the visible viewport so quick
 *  scrolls don't flash a one-frame gap. Four rows is the sweet spot
 *  measured in BS.3 motion benchmarks; higher overscan negates the
 *  savings, lower causes hover-scroll flicker. */
export const CATALOG_VIRTUALIZATION_OVERSCAN = 4

/** Row-height seed (px) used until `measureElement` reports the real
 *  height. BS.6.2 cards measure ~96/156/220px in compact / comfortable /
 *  spacious — using the right seed minimises the post-mount jump. */
const VIRTUAL_ROW_HEIGHT_ESTIMATE: Record<CatalogDensity, number> = {
  compact: 96,
  comfortable: 156,
  spacious: 220,
}

/** Tailwind `gap-{n}` mapping in pixels — has to mirror `DENSITY_GRID`
 *  so the virtual row's bottom-padding matches the static grid's row
 *  gap and the visual spacing stays identical across both paths. */
const VIRTUAL_ROW_GAP: Record<CatalogDensity, number> = {
  compact: 8,
  comfortable: 12,
  spacious: 16,
}

/** Tailwind v4 default breakpoints — column count breakpoints below.
 *  Hard-coded mirror of `DENSITY_GRID` literals so a viewport-width
 *  measurement maps to the column count the CSS grid would render. */
const TAILWIND_BREAKPOINT_PX = {
  sm: 640,
  lg: 1024,
  xl: 1280,
} as const

const COLUMNS_BY_DENSITY: Record<
  CatalogDensity,
  { base: number; sm: number; lg: number; xl: number }
> = {
  compact: { base: 1, sm: 3, lg: 4, xl: 5 },
  comfortable: { base: 1, sm: 2, lg: 3, xl: 4 },
  spacious: { base: 1, sm: 2, lg: 2, xl: 3 },
}

/** Map a viewport width (px) to the column count the CSS grid would
 *  render at that width for the given density. Pure helper exported
 *  for BS.6.8 unit tests so the row→card slicing contract is locked
 *  without a real DOM. */
export function columnsForViewport(
  density: CatalogDensity,
  viewportWidth: number,
): number {
  const map = COLUMNS_BY_DENSITY[density]
  if (viewportWidth >= TAILWIND_BREAKPOINT_PX.xl) return map.xl
  if (viewportWidth >= TAILWIND_BREAKPOINT_PX.lg) return map.lg
  if (viewportWidth >= TAILWIND_BREAKPOINT_PX.sm) return map.sm
  return map.base
}

/** React hook — tracks the live column count for the active density.
 *  Defaults to 1 on the first render so SSR + client first-render agree
 *  (no hydration mismatch); a `useEffect` mounted resize listener then
 *  flips it to the real width-derived value. The 1-column initial pass
 *  also keeps the virtualizer's `count` deterministic during hydration. */
/** BS.11.2 — defensive `CSS.escape()` shim. The browser-native helper
 *  is widely supported but not guaranteed under every jsdom version;
 *  we fall back to a tiny escaper that is safe for catalog entry ids
 *  (the only characters we have to worry about are the standard
 *  `slug.dot/colon/dash` set used by `<CatalogEntry>.id`). */
function cssEscape(value: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(value)
  }
  return value.replace(/[^a-zA-Z0-9_-]/g, (ch) => `\\${ch}`)
}

function useResponsiveColumnCount(density: CatalogDensity): number {
  const [columns, setColumns] = useState<number>(1)
  useEffect(() => {
    if (typeof window === "undefined") return
    const update = () => {
      setColumns(columnsForViewport(density, window.innerWidth))
    }
    update()
    window.addEventListener("resize", update)
    return () => window.removeEventListener("resize", update)
  }, [density])
  return columns
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
  /** BS.6.6 — stable index passed to `<CatalogCard />` so its idle-drift
   *  Layer-1 `useFloatingCard` variant (a/b/c/d) cycles deterministically
   *  with the visible card position. The catalog tab assigns this from
   *  the visible entries' index in both the static and virtualized
   *  paths so adjacent cards always land on different keyframe phases.
   *  Card-level fallback (entry-id hash) still applies if a custom
   *  renderCard ignores this field. */
  floatVariantIndex: number
  /** Card click handler — present when BS.6.3's detail panel is wired
   *  via `renderDetail`, omitted otherwise. The page wrapper plumbs
   *  this into `<CatalogCard onSelect={...} />` so clicking a card
   *  flips selection state in `<CatalogTab />` and slides the panel
   *  in. Optional so BS.6.1/6.2 preview hosts (no detail panel) leave
   *  the card non-interactive without writing extra glue. */
  onSelect?: () => void
  /** BS.11.2 — roving tabindex value the page wrapper must forward
   *  to `<CatalogCard tabIndex={...} />`. The grid maintains a single
   *  tab stop (the active card gets `0`, every other card gets `-1`)
   *  so Tab into the grid lands on one card and arrow keys move
   *  focus inside the grid. Always a number — even on preview hosts
   *  with a single visible card we emit `0` so the contract is
   *  uniform. */
  tabIndex: number
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
  /** Force the static `display: grid` layout regardless of the visible-
   *  row count. BS.6.8 unit tests pass `true` to bypass the windowed
   *  path so jsdom's missing layout engine doesn't hide every card;
   *  preview / Storybook hosts may also want the static DOM for visual
   *  regression. Defaults to `false`: virtualization engages whenever
   *  the visible row count crosses `CATALOG_VIRTUALIZATION_THRESHOLD`. */
  disableVirtualization?: boolean
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
  disableVirtualization = false,
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
  // BS.6.6 — group-breathe (Layer 5) is normal+ only per BS.3.6
  // LAYER_AVAILABILITY. Gating in JS rather than CSS keeps the
  // animation off the GPU layer at subtle / off levels (no `transform`
  // property change → no compositor work).
  const groupBreatheEnabled =
    motionLevel === "normal" || motionLevel === "dramatic"
  // BS.6.6 — scroll parallax (Layer 2) drives the whole catalog-tab
  // section to drift as the page scrolls. Speed kept low so the tab
  // stays anchored while still reading as motion. Hook self-disables
  // at level === "off". Note: the inner grid scroll container has its
  // own `overflow-auto` so virtualizer's translateY (driven by the
  // container scroll, not window scroll) is independent of this layer.
  const { ref: parallaxRef, style: parallaxStyle } =
    useScrollParallax<HTMLDivElement>({ speed: 0.05, maxOffsetPx: 20 })

  const detailEnabled = Boolean(renderDetail)
  // BS.11.2 — roving tabindex anchor. `null` means "no card has been
  // explicitly focused yet" → the first visible card receives the tab
  // stop so Tab into the grid still lands somewhere sane. After arrow-
  // key navigation the active id is pinned here. After detail close we
  // restore it from `pendingFocusRef` so focus returns to the operator's
  // last-clicked card (matches the focus-management ARIA pattern).
  const [focusedEntryId, setFocusedEntryId] = useState<string | null>(null)
  const pendingFocusRef = useRef<string | null>(null)

  // BS.11.3 — screen-reader live region for install-state transitions.
  // `previousStatesRef` snapshots the install-state of every entry the
  // last time we rendered, so a state flip on any entry (e.g.
  // available → installing → installed via the BS.7 install pipeline)
  // produces an aria-live polite announcement without forcing the
  // operator to chase visual chrome. The first render seeds the map
  // (no announcement) so we don't burst a transcript of every catalog
  // entry's current state on tab mount.
  const previousStatesRef = useRef<Map<string, CatalogInstallState> | null>(null)
  const [stateAnnouncement, setStateAnnouncement] = useState<string>("")
  const handleSelectEntry = useCallback(
    (entry: CatalogEntry) => {
      if (!detailEnabled) return
      // Remember which card to restore focus to when the detail panel
      // is dismissed — operators expect Esc to return them to the same
      // card they clicked, not the start of the grid.
      pendingFocusRef.current = entry.id
      setFocusedEntryId(entry.id)
      setSelectedId(entry.id)
    },
    [detailEnabled],
  )
  const handleCloseDetail = useCallback(() => setSelectedId(null), [])

  // BS.11.4 — density is persisted server-of-record via the J4
  // `user_preferences` API (key `catalog_density`). Same-tab writes
  // refresh sibling consumers via the
  // `omnisight:density-pref-changed` event bus; cross-tab /
  // cross-device sync flows through `preferences.updated` SSE
  // emitted by the backend router on PUT. Until the first fetch
  // resolves the hook returns `CATALOG_DEFAULT_DENSITY`
  // (comfortable) so the toolbar never flashes an empty state.
  const { density, setDensity } = useUserDensityPreference()
  const onSelectDensity = useCallback(
    (next: CatalogDensity) => {
      if (next === density) return
      // The J4 PUT happens inside `setDensity`; we intentionally
      // do not surface the rejection here — the hook restores the
      // prior value on failure and a future toast surface (BS.11.6
      // perf budget row keeps the catalog tab presentational) can
      // listen via the same event bus to prompt a retry.
      void setDensity(next)
    },
    [density, setDensity],
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

  // BS.11.3 — diff install-state across renders + emit the latest
  // announcement. Runs after commit so React owns ordering. Multiple
  // simultaneous transitions (rare — usually only one entry flips per
  // render) collapse to the most-recent `entries` order; if a louder
  // schedule is needed later we can queue them, but in practice the
  // BS.7 install pipeline only flips one entry at a time per SSE tick.
  useEffect(() => {
    const prevMap = previousStatesRef.current
    const nextMap = new Map<string, CatalogInstallState>()
    let lastMessage: string | null = null
    for (const e of entries) {
      const next = coerceCatalogInstallState(e.installState)
      nextMap.set(e.id, next)
      if (prevMap) {
        const prev = prevMap.get(e.id)
        const msg = buildCatalogStateAnnouncement(e, prev, next)
        if (msg) lastMessage = msg
      }
    }
    previousStatesRef.current = nextMap
    if (lastMessage) setStateAnnouncement(lastMessage)
  }, [entries])

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

  // BS.6.5 — windowed virtualization. Hooks are mounted unconditionally
  // (column-count tracker, scroll-element ref, virtualizer) so React
  // hook order stays stable when the visible row count crosses the
  // threshold mid-session. The virtualizer is fed `count: 0` whenever
  // virtualization is disabled (caller opt-out, SSR / pre-mount, detail
  // panel open, no visible entries) so the underlying scheduler does
  // no work. The `hasMounted` flag keeps SSR + the first client render
  // on the static path; once `useEffect` fires we swap to the windowed
  // path. That matches server / client HTML during hydration so React
  // never warns about a mismatch and avoids the "0 cards rendered while
  // the virtualizer is measuring" flash that a naive always-on virtual
  // path would cause.
  const scrollContainerRef = useRef<HTMLDivElement | null>(null)
  const columnCount = useResponsiveColumnCount(density)
  const [hasMounted, setHasMounted] = useState(false)
  useEffect(() => {
    setHasMounted(true)
  }, [])
  const virtualizationActive =
    hasMounted &&
    !disableVirtualization &&
    !detailOpen &&
    visibleCount > 0 &&
    Math.ceil(visibleCount / Math.max(1, columnCount)) >=
      CATALOG_VIRTUALIZATION_THRESHOLD
  const virtualRowCount = virtualizationActive
    ? Math.ceil(visibleCount / Math.max(1, columnCount))
    : 0
  const rowEstimate = VIRTUAL_ROW_HEIGHT_ESTIMATE[density]
  const rowGap = VIRTUAL_ROW_GAP[density]
  // React Compiler flags `useVirtualizer` as an incompatible library
  // because the hook returns ad-hoc closures (`measureElement`,
  // `getVirtualItems`) that cannot be auto-memoised. The disable is
  // scoped to this single hook call — Compiler just skips memoising
  // values returned by it, which is the documented expected behaviour
  // for TanStack Virtual under React 19.
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: virtualRowCount,
    getScrollElement: () => scrollContainerRef.current,
    estimateSize: () => rowEstimate + rowGap,
    overscan: CATALOG_VIRTUALIZATION_OVERSCAN,
  })
  const virtualRows = virtualizationActive
    ? rowVirtualizer.getVirtualItems()
    : []
  const virtualTotalSize = virtualizationActive
    ? rowVirtualizer.getTotalSize()
    : 0

  // BS.11.2 — keyboard navigation. Resolve the "active" tab-stop id:
  // if the operator has explicitly focused / selected a card, that id
  // wins; otherwise the first visible card is the tab anchor so Tab
  // into the grid still lands somewhere sane. When the active id no
  // longer matches a visible entry (filter / search shrunk the list),
  // fall back to the first visible entry so the tab stop never lands
  // on `tabindex=-1` for every card.
  const visibleIds = useMemo(() => visible.map((e) => e.id), [visible])
  const activeFocusId = useMemo(() => {
    if (focusedEntryId && visibleIds.includes(focusedEntryId)) {
      return focusedEntryId
    }
    return visibleIds[0] ?? null
  }, [focusedEntryId, visibleIds])

  // After the detail panel closes we restore focus to the previously
  // selected card (matches the modal-dismiss focus-restoration ARIA
  // pattern). `pendingFocusRef` is set when the operator opens detail
  // and consumed once the grid re-mounts.
  useEffect(() => {
    if (detailOpen) return
    const targetId = pendingFocusRef.current
    if (!targetId) return
    if (!visibleIds.includes(targetId)) {
      pendingFocusRef.current = null
      return
    }
    // Wait one frame so the static / virtualized grid has committed
    // its DOM before we query for the slot's focusable child.
    const raf = requestAnimationFrame(() => {
      focusCardById(targetId)
      pendingFocusRef.current = null
    })
    return () => cancelAnimationFrame(raf)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailOpen, visibleIds])

  // Focus the focusable card root inside the slot wrapper. The card's
  // outer-most motion shell carries `role="button"` + `tabindex` (set
  // via the renderCard contract below) so we delegate to it rather
  // than focusing the slot — the card is the element with click +
  // Enter/Space affordance the operator expects to see focused.
  const focusCardById = useCallback((id: string) => {
    if (typeof document === "undefined") return
    const slot = document.querySelector<HTMLElement>(
      `[data-keynav-card-slot="true"][data-entry-id="${cssEscape(id)}"]`,
    )
    if (!slot) return
    // Prefer the card root (role=button); fall back to the slot itself
    // so jsdom + non-CatalogCard renderCards still receive focus.
    const card = slot.querySelector<HTMLElement>(
      "[role='button']",
    )
    ;(card ?? slot).focus()
  }, [])

  // Grid-level keyboard handler. Only intercepts arrow / Home / End /
  // Escape; everything else bubbles so the card's own Enter/Space →
  // onSelect path stays untouched.
  const handleGridKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.defaultPrevented) return
      const target = event.target as HTMLElement | null
      if (!target) return
      const slot = target.closest?.(
        "[data-keynav-card-slot='true']",
      ) as HTMLElement | null
      if (!slot) return
      const id = slot.getAttribute("data-entry-id")
      if (!id) return
      const idx = visibleIds.indexOf(id)
      if (idx < 0) return
      const cols = Math.max(1, columnCount)
      let nextIdx = -1
      switch (event.key) {
        case "ArrowRight":
          nextIdx = Math.min(idx + 1, visibleIds.length - 1)
          break
        case "ArrowLeft":
          nextIdx = Math.max(idx - 1, 0)
          break
        case "ArrowDown":
          nextIdx = Math.min(idx + cols, visibleIds.length - 1)
          break
        case "ArrowUp":
          nextIdx = Math.max(idx - cols, 0)
          break
        case "Home":
          nextIdx = 0
          break
        case "End":
          nextIdx = visibleIds.length - 1
          break
        default:
          return
      }
      if (nextIdx === idx || nextIdx < 0) return
      event.preventDefault()
      const nextId = visibleIds[nextIdx]
      setFocusedEntryId(nextId)
      // Defer the .focus() call to the next frame so React commits the
      // new roving tabindex (`tabindex=0` moves to the next slot,
      // previous slot drops to `-1`) before the focus() lands.
      requestAnimationFrame(() => focusCardById(nextId))
    },
    [columnCount, focusCardById, visibleIds],
  )

  return (
    <div
      ref={parallaxRef}
      data-testid="catalog-tab"
      data-catalog-density={density}
      data-catalog-family={familyFilter}
      data-catalog-sort={sort}
      data-catalog-visible={visibleCount}
      data-catalog-total={totalCount}
      data-catalog-detail-open={detailOpen ? "true" : "false"}
      data-catalog-selected-id={selectedEntry?.id ?? ""}
      data-catalog-motion-level={motionLevel}
      data-motion-parallax={parallaxStyle.transform ? "on" : "off"}
      data-motion-group-breathe={groupBreatheEnabled ? "on" : "off"}
      data-catalog-active-focus-id={activeFocusId ?? ""}
      data-catalog-announcement={stateAnnouncement}
      style={parallaxStyle}
      className={["flex flex-col gap-4", className ?? ""]
        .filter(Boolean)
        .join(" ")}
    >
      {/* ── BS.11.3 — Screen-reader live region. Visually hidden via
       *      `sr-only` (Tailwind utility, hides without removing from
       *      the a11y tree). `aria-live="polite"` so install-state
       *      transitions announced by `<CatalogTab />` interrupt the
       *      reader at the next safe boundary instead of stomping the
       *      operator's current speech. `aria-atomic="true"` so the
       *      reader announces the full message when the text changes
       *      (otherwise some readers only read the diff and may swallow
       *      a state flip from "Installed" → "Install failed" on the
       *      same entry id). */}
      <div
        data-testid="catalog-tab-announcer"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="sr-only"
      >
        {stateAnnouncement}
      </div>
      {/* ── Toolbar ─────────────────────────────────────────────── */}
      <div
        data-testid="catalog-tab-toolbar"
        className="flex flex-col gap-3 rounded-md border border-[var(--border)] bg-[var(--card)]/40 p-3 md:flex-row md:items-center md:justify-between"
      >
        {/* Left: family chips — BS.6.4 polished `<CategoryStrip />`.
         *  Replaces BS.6.1's inline `<FamilyChip />` pills with corner-
         *  brackets-full-tinted + localised scan-sweep treatment per
         *  family. The `rootTestId` / `chipTestIdPrefix` overrides keep
         *  BS.6.1's testid contract (`catalog-tab-family-chips` +
         *  `catalog-tab-family-chip-{id}`) so any test or deep-link
         *  written against BS.6.1 still resolves. Filter state stays
         *  owned by CatalogTab (the chip strip is presentation only). */}
        <CategoryStrip
          family={familyFilter}
          onSelect={(next) => setFamilyFilter(next)}
          rootTestId="catalog-tab-family-chips"
          chipTestIdPrefix="catalog-tab-family-chip"
        />

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
      ) : virtualizationActive ? (
        // BS.6.5 — windowed grid. The outer wrapper is the scroll
        // container the `useVirtualizer` reads viewport bounds from
        // (`max-h-[70vh] overflow-auto` keeps the chrome anchored — page
        // breadcrumb + hero + tab nav stay above, only the cards
        // scroll). Each rendered row absolutely-positions itself at
        // `translateY(virtualRow.start)` inside an inner spacer whose
        // height matches the total measured size so the scrollbar
        // tracks the full list. Cards are sliced into the row inline so
        // `<CatalogCard />` motion / hover behaviour is unchanged.
        // BS.6.6 — `.group-breathe` (Layer 5) is applied to the inner
        // spacer rather than the outer scroll container so the breath
        // happens inside the scroll viewport (no scrollbar jitter from
        // a scaling overflow:auto wrapper). Gated to normal+ levels via
        // `groupBreatheEnabled`.
        <div
          ref={scrollContainerRef}
          data-testid="catalog-tab-grid"
          data-grid-density={density}
          data-grid-virtualized="true"
          data-grid-column-count={columnCount}
          data-grid-row-count={virtualRowCount}
          data-grid-rendered-rows={virtualRows.length}
          data-grid-overscan={CATALOG_VIRTUALIZATION_OVERSCAN}
          data-grid-group-breathe={groupBreatheEnabled ? "on" : "off"}
          role="grid"
          aria-label="Catalog entries"
          aria-rowcount={virtualRowCount}
          aria-colcount={columnCount}
          onKeyDown={handleGridKeyDown}
          className={[
            "max-h-[70vh] overflow-auto",
            gridAnimClass,
          ].join(" ")}
        >
          <div
            data-testid="catalog-tab-virtual-spacer"
            style={{
              height: `${virtualTotalSize}px`,
              position: "relative",
              width: "100%",
            }}
            className={groupBreatheEnabled ? "group-breathe" : undefined}
          >
            {virtualRows.map((virtualRow) => {
              const rowStart = virtualRow.index * Math.max(1, columnCount)
              const slice = visible.slice(
                rowStart,
                rowStart + Math.max(1, columnCount),
              )
              return (
                <div
                  key={virtualRow.key}
                  ref={rowVirtualizer.measureElement}
                  data-index={virtualRow.index}
                  data-testid={`catalog-tab-virtual-row-${virtualRow.index}`}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${virtualRow.start}px)`,
                    paddingBottom: `${rowGap}px`,
                  }}
                  className={["grid", gridClass].join(" ")}
                >
                  {slice.map((entry, columnIndex) => {
                    const isActiveFocus = entry.id === activeFocusId
                    return (
                      <div
                        key={entry.id}
                        data-testid={`catalog-tab-card-slot-${entry.id}`}
                        data-entry-id={entry.id}
                        data-entry-family={coerceFamily(entry.family)}
                        data-keynav-card-slot="true"
                        data-keynav-active={isActiveFocus ? "true" : "false"}
                        role="gridcell"
                      >
                        {renderEntryCard({
                          entry,
                          density,
                          cardPaddingClass,
                          floatVariantIndex: rowStart + columnIndex,
                          tabIndex: isActiveFocus ? 0 : -1,
                          onSelect: detailEnabled
                            ? () => handleSelectEntry(entry)
                            : undefined,
                        })}
                      </div>
                    )
                  })}
                </div>
              )
            })}
          </div>
        </div>
      ) : (
        // BS.6.6 — static grid path also carries `.group-breathe` (Layer
        // 5) when motion level is normal+. Because there is no inner
        // spacer here, we apply the class directly to the grid root —
        // safe because the static path's grid has no virtualizer
        // translateY transform competing for the `transform` property.
        <div
          data-testid="catalog-tab-grid"
          data-grid-density={density}
          data-grid-virtualized="false"
          data-grid-column-count={columnCount}
          data-grid-group-breathe={groupBreatheEnabled ? "on" : "off"}
          role="grid"
          aria-label="Catalog entries"
          aria-rowcount={Math.ceil(visibleCount / Math.max(1, columnCount))}
          aria-colcount={columnCount}
          onKeyDown={handleGridKeyDown}
          className={[
            "grid",
            gridClass,
            gridAnimClass,
            groupBreatheEnabled ? "group-breathe" : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {visible.map((entry, index) => {
            const isActiveFocus = entry.id === activeFocusId
            return (
              <div
                key={entry.id}
                data-testid={`catalog-tab-card-slot-${entry.id}`}
                data-entry-id={entry.id}
                data-entry-family={coerceFamily(entry.family)}
                data-keynav-card-slot="true"
                data-keynav-active={isActiveFocus ? "true" : "false"}
                role="gridcell"
              >
                {renderEntryCard({
                  entry,
                  density,
                  cardPaddingClass,
                  floatVariantIndex: index,
                  tabIndex: isActiveFocus ? 0 : -1,
                  onSelect: detailEnabled
                    ? () => handleSelectEntry(entry)
                    : undefined,
                })}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Sub-components — file-private. The placeholder is intentionally
// minimal so it can't be mistaken for the production card BS.6.2 ships.
// (BS.6.4 retired the inline `<FamilyChip />` here in favour of the
// polished `<CategoryStrip />` — the per-family palette + corner-
// brackets-full-tinted + scan-sweep dressing now live there.)
// ─────────────────────────────────────────────────────────────────────

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

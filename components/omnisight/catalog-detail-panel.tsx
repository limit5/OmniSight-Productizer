"use client"

/**
 * BS.6.3 — Catalog detail panel.
 *
 * Inline-expand replacement for the catalog grid: when an operator
 * clicks a `<CatalogCard />`, the grid slides out to the left and this
 * panel slides in from the right. The panel keeps the operator inside
 * the same `?tab=catalog` surface (no full-page nav, no modal overlay)
 * so context — toolbar, hero panel, breadcrumb — stays visible.
 *
 * Layout (md+):
 *   ┌──────────────────────────────────────────────────────────────┐
 *   │ ◄ back · {family} chip · {state} chip · vendor · v{version}  │
 *   ├─────────────┬────────────────────────────────────────────────┤
 *   │             │  Header (display name, description)            │
 *   │  ENERGY     │  Dependencies (graph row, depends_on TEXT[])   │
 *   │  ORB        │  Used by workspaces (reverse refs)             │
 *   │  (SVG)      │  Activity audit (timeline)                     │
 *   │             │  Footer CTA (state-appropriate primary action) │
 *   └─────────────┴────────────────────────────────────────────────┘
 *
 * Energy orb (left):
 *   Concentric SVG rings + an animated core. Core colour + halo
 *   intensity track the install state (palette mirrors BS.6.2's card
 *   variants so the orb feels like a "zoomed-in" version of the card
 *   icon). For installing state the orb spins faster and the percent
 *   readout sits in the centre; for failed it pulses with the
 *   `force-turbo-armed` heartbeat. Subtle / off motion levels paint the
 *   orb statically — no listeners, no animation.
 *
 * Dependencies (right column, top):
 *   Each `depends_on[]` entry surfaces as a chip; if `metadata.depends_on_meta`
 *   is present and includes a per-dep `state`, the chip carries a small
 *   status dot (emerald / amber / rose). Operators get a one-glance read
 *   of "is the prerequisite installed?". Empty `depends_on[]` collapses
 *   the section with a "no dependencies" muted line.
 *
 * Used by workspaces (right column, middle):
 *   Reverse references — the operator-facing answer to "who is going to
 *   break if I uninstall this?". Sourced from `metadata.used_by_workspaces`
 *   (ADR §7.1 forward-compat key, written by BS.7.5+ install pipeline
 *   when a workspace activates a vertical). Each row shows the workspace
 *   name + product line + a deep-link to the workspace surface.
 *
 * Activity audit (right column, bottom):
 *   Timeline of events (created / installed / updated / failed) for this
 *   entry pulled from `metadata.activity[]` (BS.7.6 will plumb the
 *   real subset of `audit_log` rows scoped to this entry). Renders the
 *   most-recent N events; older events fold under a "+M more in audit
 *   log" link that BS.7.6 will deep-link to the audit surface.
 *
 * Out of scope for this row (deferred):
 *   • BS.6.5 — virtualisation; this panel only renders one entry at a
 *     time so it has no virtualisation concern of its own.
 *   • BS.6.6 — BS.3 8-layer motion; the panel deliberately avoids the
 *     tilt + glass-reflect wrappers BS.5.4 used so the slide-in stays
 *     legible on a freshly-revealed surface (those layers will land
 *     here in BS.6.6 once BS.6.5 stabilises height/scroll).
 *
 * BS.6.7 — disabled-state tooltip (this row owns)
 * ───────────────────────────────────────────────
 * Until BS.7 lands the install pipeline, the panel's primary CTA
 * (Install / Update / Retry / View log) is rendered without a handler
 * from the page wrapper. To communicate why the affordance is parked,
 * each disabled button is wrapped with `<PendingInstallTooltip />`
 * (shared with `<CatalogCard />`) showing
 * "Install pipeline 即將上線". The wrapper becomes a no-op passthrough
 * once a handler is wired so BS.7 just plumbs `onInstall` through and
 * the tooltip + tab-stop wrapper vanishes automatically.
 *   • BS.7.6 — replaces `metadata.activity[]` reads with a live
 *     `audit_log` slice + replaces the "+M more" link with a real deep
 *     link to the audit surface.
 *
 * Slide animation (the contract this row owns):
 *   The panel is always rendered with `data-state="open"` when the
 *   parent (`<CatalogTab />`) has a selected entry. Tailwind's
 *   `tw-animate-css` plugin (already imported in `app/globals.css`)
 *   provides the `animate-in slide-in-from-right-8` + `animate-out
 *   slide-out-to-left-8` utility classes. The grid side uses the
 *   reciprocal `slide-out-to-left` on close and `slide-in-from-left` on
 *   re-mount; the swap is owned by `<CatalogTab />` so it stays in
 *   one place. Reduced-motion users (per `useEffectiveMotionLevel()`)
 *   see an instant cross-fade — no horizontal translation — to honour
 *   the BS ADR §6 reduced-motion rule.
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. All visuals derive from props +
 * immutable look-up tables (`STATE_PALETTE`, `STATE_ORB_RING_SPEC`,
 * `ACTIVITY_KIND_META`, `RELATIVE_TIME_BUCKETS`). Pure render — no
 * `useState` / `useEffect` / `useRef` outside the optional motion
 * level read. SSR-safe (deterministic, no `Math.random`, no
 * `Date.now` that isn't bracketed by `formatRelativeTime`'s explicit
 * `now` arg). Browser-only render — uvicorn `--workers N` model does
 * not apply (answer #1: each browser tab derives the same view from
 * the same Next.js build artifact).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * N/A — pure presentation. Activity events flow through the `entry`
 * prop; BS.7.6's audit-log feed will just re-render the panel with a
 * fresher `entry.metadata.activity[]`. No API calls, no cross-worker
 * race at this layer.
 */

import {
  type ReactNode,
  useCallback,
  useMemo,
  useState,
} from "react"
import {
  AlertOctagon,
  ArrowLeft,
  ArrowUpCircle,
  CheckCircle2,
  ChevronRight,
  Clock,
  ClipboardList,
  Download,
  ExternalLink,
  Layers,
  Loader2,
  RefreshCw,
  ScrollText,
  Sparkles,
  Users,
} from "lucide-react"

import {
  buildInstallProgressGradient,
  CATALOG_INSTALL_PENDING_TOOLTIP,
  clampInstallProgress,
  coerceInstallState,
  PendingInstallTooltip,
} from "@/components/omnisight/catalog-card"
import {
  type CatalogEntry,
  type CatalogFamily,
  type CatalogInstallState,
  coerceFamily,
} from "@/components/omnisight/catalog-tab"
import { useEffectiveMotionLevel } from "@/hooks/use-zero-g"

// ─────────────────────────────────────────────────────────────────────
// Public types — exported so BS.6.8 tests + BS.7.5/7.6 install pipeline
// share a single source of truth for the panel prop shape.
// ─────────────────────────────────────────────────────────────────────

/** Activity kinds the panel surfaces with a per-kind icon + colour.
 *  Extends are forward-compat: anything outside this list collapses to
 *  `info` (per BS ADR §3.4 forward-compat rule). */
export const CATALOG_ACTIVITY_KINDS = [
  "created",
  "installed",
  "updated",
  "failed",
  "uninstalled",
  "info",
] as const
export type CatalogActivityKind = (typeof CATALOG_ACTIVITY_KINDS)[number]

export interface CatalogDetailDependency {
  /** Catalog entry id this row depends on. */
  id: string
  /** Operator-facing name — defaults to `id` when omitted. */
  displayName?: string
  /** Lifecycle state of the dependency, drives the status dot colour.
   *  Optional because BS.7.5 may surface deps before their state is
   *  resolved; unknown state renders as a neutral chip. */
  state?: CatalogInstallState
}

export interface CatalogDetailUsedByWorkspace {
  /** Stable workspace id (route segment used by the deep-link). */
  id: string
  /** Operator-facing workspace name (e.g. "Web Studio · pro-shop"). */
  name: string
  /** Product line bucket — drives the small chip accent. */
  productLine?: "web" | "mobile" | "software" | "other"
  /** Optional deep-link override; defaults to `/workspace/{id}`. */
  href?: string
}

export interface CatalogDetailActivityEvent {
  /** Stable id (audit_log row id when BS.7.6 plumbs the real feed). */
  id: string
  /** Activity kind — drives icon + colour; unknown collapses to info. */
  kind: CatalogActivityKind | string
  /** ISO timestamp of when the event happened. */
  timestamp: string
  /** Short operator-facing message. */
  message: string
  /** Optional actor (operator email / sidecar id / system). */
  actor?: string
}

export interface CatalogDetailPanelProps {
  entry: CatalogEntry
  /** Back action — closes the panel and re-mounts the catalog grid.
   *  `<CatalogTab />` owns this state, the panel just fires the event. */
  onBack: () => void
  /** Optional install handler — kept opt-in so BS.6.7 can wire it. */
  onInstall?: (entry: CatalogEntry) => void
  /** Retry handler — for `failed` state only. */
  onRetry?: (entry: CatalogEntry) => void
  /** "View log" handler — for `failed` state only. */
  onViewLog?: (entry: CatalogEntry) => void
  /** Click handler for a dependency chip; lets the parent jump the
   *  selection to the dep entry without re-fetching the catalog. */
  onSelectDependency?: (depId: string) => void
  /** Click handler for a "used by" workspace row; defaults to a
   *  `Link` to `/workspace/{id}` via the panel's internal `<Link>` tag,
   *  but tests + alternate hosts can intercept here. */
  onSelectWorkspace?: (workspace: CatalogDetailUsedByWorkspace) => void
  /** Override `Date.now()` for stable `formatRelativeTime` output in
   *  tests + Storybook. Defaults to `() => Date.now()`. */
  now?: () => number
  /** Cap on rendered activity events. Older events fold under a
   *  "+ N more in audit log" muted line. Defaults to 6 so the panel
   *  doesn't grow unbounded for entries with thousands of audit rows. */
  activityLimit?: number
  className?: string
}

// ─────────────────────────────────────────────────────────────────────
// Pure helpers — exported so BS.6.8 tests can lock contract directly.
// ─────────────────────────────────────────────────────────────────────

/** Lift an arbitrary activity-kind string to a known bucket. */
export function coerceActivityKind(value: unknown): CatalogActivityKind {
  if (typeof value !== "string") return "info"
  if ((CATALOG_ACTIVITY_KINDS as readonly string[]).includes(value)) {
    return value as CatalogActivityKind
  }
  return "info"
}

/** Pull dependencies out of an entry. Reads the `depends_on` array on
 *  the entry first (canonical PG `catalog_entries.depends_on TEXT[]`),
 *  then optionally enriches with `metadata.depends_on_meta`. Returns a
 *  fresh array — caller can sort / filter without worrying about
 *  mutation. */
export function extractDependencies(
  entry: CatalogEntry,
): CatalogDetailDependency[] {
  const dependsOn = entry.metadata?.depends_on
  const ids: string[] = Array.isArray(dependsOn)
    ? dependsOn.filter((v): v is string => typeof v === "string" && v.length > 0)
    : []
  const meta = entry.metadata?.depends_on_meta
  const metaMap: Record<string, { displayName?: string; state?: string }> =
    meta && typeof meta === "object" && !Array.isArray(meta)
      ? (meta as Record<string, { displayName?: string; state?: string }>)
      : {}
  return ids.map((id) => {
    const m = metaMap[id]
    const stateRaw = m && typeof m.state === "string" ? m.state : undefined
    return {
      id,
      displayName: m && typeof m.displayName === "string" ? m.displayName : undefined,
      state: stateRaw ? coerceInstallState(stateRaw) : undefined,
    }
  })
}

/** Pull "used by" workspaces. Reads `metadata.used_by_workspaces`
 *  (BS.7.5+ writes this). Anything that doesn't have a string `id` +
 *  string `name` is silently dropped — operator-friendly default that
 *  protects the panel from a buggy publisher injecting bad rows. */
export function extractUsedByWorkspaces(
  entry: CatalogEntry,
): CatalogDetailUsedByWorkspace[] {
  const raw = entry.metadata?.used_by_workspaces
  if (!Array.isArray(raw)) return []
  const out: CatalogDetailUsedByWorkspace[] = []
  for (const row of raw) {
    if (!row || typeof row !== "object") continue
    const r = row as Record<string, unknown>
    const id = typeof r.id === "string" ? r.id : null
    const name = typeof r.name === "string" ? r.name : null
    if (!id || !name) continue
    const productLine =
      r.productLine === "web" ||
      r.productLine === "mobile" ||
      r.productLine === "software"
        ? r.productLine
        : "other"
    const href = typeof r.href === "string" ? r.href : undefined
    out.push({ id, name, productLine, href })
  }
  return out
}

/** Pull activity events. Sorts most-recent-first by timestamp
 *  (lexicographic ISO comparison is correct for `Date.parse`-able
 *  strings). Drops rows missing `id` / `timestamp` / `message`. */
export function extractActivityEvents(
  entry: CatalogEntry,
): CatalogDetailActivityEvent[] {
  const raw = entry.metadata?.activity
  if (!Array.isArray(raw)) return []
  const out: CatalogDetailActivityEvent[] = []
  for (const row of raw) {
    if (!row || typeof row !== "object") continue
    const r = row as Record<string, unknown>
    const id = typeof r.id === "string" ? r.id : null
    const timestamp = typeof r.timestamp === "string" ? r.timestamp : null
    const message = typeof r.message === "string" ? r.message : null
    if (!id || !timestamp || !message) continue
    out.push({
      id,
      kind: coerceActivityKind(r.kind),
      timestamp,
      message,
      actor: typeof r.actor === "string" ? r.actor : undefined,
    })
  }
  out.sort((a, b) => {
    const at = Date.parse(a.timestamp)
    const bt = Date.parse(b.timestamp)
    const aok = Number.isFinite(at)
    const bok = Number.isFinite(bt)
    if (aok && bok) return bt - at
    if (aok) return -1
    if (bok) return 1
    return 0
  })
  return out
}

interface RelativeTimeBucket {
  /** Threshold in milliseconds — when `delta < threshold` the bucket
   *  fires. Last bucket has Infinity threshold so it always matches. */
  threshold: number
  format: (deltaMs: number) => string
}

const RELATIVE_TIME_BUCKETS: ReadonlyArray<RelativeTimeBucket> = [
  { threshold: 60_000, format: () => "just now" },
  { threshold: 60 * 60_000, format: (d) => `${Math.round(d / 60_000)}m ago` },
  { threshold: 24 * 60 * 60_000, format: (d) => `${Math.round(d / (60 * 60_000))}h ago` },
  { threshold: 7 * 24 * 60 * 60_000, format: (d) => `${Math.round(d / (24 * 60 * 60_000))}d ago` },
  { threshold: 30 * 24 * 60 * 60_000, format: (d) => `${Math.round(d / (7 * 24 * 60 * 60_000))}w ago` },
  { threshold: 365 * 24 * 60 * 60_000, format: (d) => `${Math.round(d / (30 * 24 * 60 * 60_000))}mo ago` },
  { threshold: Number.POSITIVE_INFINITY, format: (d) => `${Math.round(d / (365 * 24 * 60 * 60_000))}y ago` },
]

/** Format an ISO timestamp as a short relative-time label (e.g. "5m
 *  ago"). Future timestamps render as "in N…" so the panel doesn't
 *  display obviously-broken negative deltas during clock skew. Returns
 *  `"—"` for unparseable input rather than throwing. */
export function formatRelativeTime(
  isoTimestamp: string,
  nowMs: number = Date.now(),
): string {
  const t = Date.parse(isoTimestamp)
  if (!Number.isFinite(t)) return "—"
  const delta = nowMs - t
  if (delta < 0) {
    // Future — shouldn't happen in a well-behaved audit feed, but
    // rendering "in 2h" is friendlier than a confusing "-2h ago".
    const future = -delta
    for (const bucket of RELATIVE_TIME_BUCKETS) {
      if (future < bucket.threshold) {
        if (bucket.format.length === 0) return "in a moment"
        const label = bucket.format(future)
        // Replace " ago" suffix with " from now".
        return label.replace(/ ago$/, " from now").replace(/^just now$/, "in a moment")
      }
    }
  }
  for (const bucket of RELATIVE_TIME_BUCKETS) {
    if (delta < bucket.threshold) return bucket.format(delta)
  }
  return "—"
}

interface EnergyOrbState {
  /** Hex/CSS-var colour for the orb core. */
  coreColor: string
  /** Hex/CSS-var colour for the orb halo glow. */
  haloColor: string
  /** Drop-shadow filter string for the orb svg. */
  glowFilter: string
  /** Tailwind class layered on the orb root for the per-state animation
   *  (force-turbo-armed for failed, pulse-purple for update-available,
   *  empty for available / installed). */
  rootAnimationClass: string
  /** Optional inline label rendered inside the orb (installing shows
   *  the live percent; everything else surfaces the state label). */
  centerLabel: string
  /** Whether the inner ring spins (installing gets a fast spin to match
   *  the card's conic-gradient progress; everything else uses the slow
   *  `orbital-rotate` keyframe). */
  innerRingFast: boolean
}

/** Compute the energy-orb visual state from an install state +
 *  progress. Pure helper exported so BS.6.8 can lock the colour table
 *  + the installing-vs-spinning branch without parsing CSS. */
export function buildEnergyOrbState(
  state: CatalogInstallState,
  progress: number,
): EnergyOrbState {
  switch (state) {
    case "installed":
      return {
        coreColor: "var(--validation-emerald)",
        haloColor: "rgba(16,185,129,0.5)",
        glowFilter:
          "drop-shadow(0 0 10px rgba(16,185,129,0.6)) drop-shadow(0 0 20px rgba(16,185,129,0.35))",
        rootAnimationClass: "",
        centerLabel: "OK",
        innerRingFast: false,
      }
    case "installing":
      return {
        coreColor: "var(--neural-blue)",
        haloColor: "rgba(56,189,248,0.55)",
        glowFilter:
          "drop-shadow(0 0 12px rgba(56,189,248,0.7)) drop-shadow(0 0 22px rgba(56,189,248,0.35))",
        rootAnimationClass: "",
        centerLabel: `${clampInstallProgress(progress).toFixed(0)}%`,
        innerRingFast: true,
      }
    case "update-available":
      return {
        coreColor: "var(--artifact-purple)",
        haloColor: "rgba(168,85,247,0.5)",
        glowFilter:
          "drop-shadow(0 0 10px rgba(168,85,247,0.6)) drop-shadow(0 0 20px rgba(168,85,247,0.35))",
        rootAnimationClass: "pulse-purple",
        centerLabel: "Update",
        innerRingFast: false,
      }
    case "failed":
      return {
        coreColor: "var(--critical-red)",
        haloColor: "rgba(239,68,68,0.55)",
        glowFilter:
          "drop-shadow(0 0 12px rgba(239,68,68,0.7)) drop-shadow(0 0 22px rgba(239,68,68,0.35))",
        rootAnimationClass: "force-turbo-armed",
        centerLabel: "Fail",
        innerRingFast: false,
      }
    case "available":
    default:
      return {
        coreColor: "var(--neural-blue)",
        haloColor: "rgba(56,189,248,0.4)",
        glowFilter:
          "drop-shadow(0 0 8px rgba(56,189,248,0.45)) drop-shadow(0 0 18px rgba(56,189,248,0.2))",
        rootAnimationClass: "",
        centerLabel: "Ready",
        innerRingFast: false,
      }
  }
}

// ─────────────────────────────────────────────────────────────────────
// State / family / activity look-up tables. Tailwind class literals
// must be statically present for the JIT.
// ─────────────────────────────────────────────────────────────────────

interface StateMeta {
  label: string
  chipClass: string
  borderClass: string
}

const STATE_META: Record<CatalogInstallState, StateMeta> = {
  available: {
    label: "Available",
    chipClass:
      "border-[var(--neural-blue)]/45 bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]",
    borderClass: "border-[var(--neural-blue)]/40",
  },
  installed: {
    label: "Installed",
    chipClass:
      "border-[var(--validation-emerald)]/55 bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)]",
    borderClass: "border-[var(--validation-emerald)]/55",
  },
  installing: {
    label: "Installing",
    chipClass:
      "border-[var(--neural-blue)]/55 bg-[var(--neural-blue)]/15 text-[var(--neural-blue)]",
    borderClass: "border-[var(--neural-blue)]/60",
  },
  "update-available": {
    label: "Update available",
    chipClass:
      "border-[var(--artifact-purple)]/55 bg-[var(--artifact-purple)]/15 text-[var(--artifact-purple)]",
    borderClass: "border-[var(--artifact-purple)]/45",
  },
  failed: {
    label: "Failed",
    chipClass:
      "border-[var(--critical-red)]/55 bg-[var(--critical-red)]/15 text-[var(--critical-red)]",
    borderClass: "border-[var(--critical-red)]/65",
  },
}

const FAMILY_LABEL: Record<CatalogFamily, string> = {
  mobile: "Mobile",
  embedded: "Embedded",
  web: "Web",
  software: "Software",
  custom: "Custom",
}

const FAMILY_ACCENT: Record<CatalogFamily, string> = {
  mobile: "border-emerald-500/55 text-emerald-300",
  embedded: "border-amber-500/55 text-amber-300",
  web: "border-sky-500/55 text-sky-300",
  software: "border-violet-500/55 text-violet-300",
  custom: "border-rose-500/55 text-rose-300",
}

const PRODUCT_LINE_ACCENT: Record<
  NonNullable<CatalogDetailUsedByWorkspace["productLine"]>,
  string
> = {
  web: "border-sky-500/55 text-sky-300",
  mobile: "border-emerald-500/55 text-emerald-300",
  software: "border-violet-500/55 text-violet-300",
  other: "border-[var(--border)] text-[var(--muted-foreground)]",
}

interface ActivityKindMeta {
  label: string
  icon: typeof CheckCircle2
  iconClass: string
  ariaTone: string
}

const ACTIVITY_KIND_META: Record<CatalogActivityKind, ActivityKindMeta> = {
  created: {
    label: "Created",
    icon: Sparkles,
    iconClass: "text-[var(--neural-blue)]",
    ariaTone: "info",
  },
  installed: {
    label: "Installed",
    icon: CheckCircle2,
    iconClass: "text-[var(--validation-emerald)]",
    ariaTone: "success",
  },
  updated: {
    label: "Updated",
    icon: ArrowUpCircle,
    iconClass: "text-[var(--artifact-purple)]",
    ariaTone: "info",
  },
  failed: {
    label: "Failed",
    icon: AlertOctagon,
    iconClass: "text-[var(--critical-red)]",
    ariaTone: "error",
  },
  uninstalled: {
    label: "Uninstalled",
    icon: ScrollText,
    iconClass: "text-[var(--muted-foreground)]",
    ariaTone: "info",
  },
  info: {
    label: "Event",
    icon: ClipboardList,
    iconClass: "text-[var(--muted-foreground)]",
    ariaTone: "info",
  },
}

const DEPENDENCY_DOT_FILL: Record<CatalogInstallState, string> = {
  available: "bg-[var(--neural-blue)]/60",
  installed: "bg-[var(--validation-emerald)]",
  installing: "bg-[var(--neural-blue)]",
  "update-available": "bg-[var(--artifact-purple)]",
  failed: "bg-[var(--critical-red)]",
}

// ─────────────────────────────────────────────────────────────────────
// CatalogDetailPanel — main export.
// ─────────────────────────────────────────────────────────────────────

export function CatalogDetailPanel({
  entry,
  onBack,
  onInstall,
  onRetry,
  onViewLog,
  onSelectDependency,
  onSelectWorkspace,
  now,
  activityLimit = 6,
  className,
}: CatalogDetailPanelProps) {
  const motionLevel = useEffectiveMotionLevel()
  const reducedMotion = motionLevel === "off" || motionLevel === "subtle"

  const state = coerceInstallState(entry.installState)
  const family = coerceFamily(entry.family)
  const stateMeta = STATE_META[state]

  const progress = useMemo(
    () => clampInstallProgress(entry.metadata?.progressPercent),
    [entry.metadata?.progressPercent],
  )

  const dependencies = useMemo(() => extractDependencies(entry), [entry])
  const usedByWorkspaces = useMemo(() => extractUsedByWorkspaces(entry), [entry])
  const activity = useMemo(() => extractActivityEvents(entry), [entry])

  const visibleActivity = useMemo(
    () => activity.slice(0, Math.max(0, activityLimit)),
    [activity, activityLimit],
  )
  const hiddenActivityCount = Math.max(0, activity.length - visibleActivity.length)

  const orb = useMemo(
    () => buildEnergyOrbState(state, progress),
    [state, progress],
  )

  // `useState` lazy initialiser — fires once per mount so React's
  // purity rule is happy (no impure call during render). The relative-
  // time labels stay pinned to mount time, which matches operator
  // intuition: you opened the panel, the "5m ago" label refers to that
  // moment. Tests + Storybook override via the `now` prop for stable
  // snapshots without re-render thrash.
  const [mountNowMs] = useState(() => (now ? now() : Date.now()))
  const nowMs = now ? now() : mountNowMs

  const handleBack = useCallback(() => onBack(), [onBack])

  // The slide-in classes come from `tw-animate-css` (already imported in
  // app/globals.css). Reduced-motion users see a fade only — same time
  // budget but no horizontal translation, per BS ADR §6.
  const slideInClass = reducedMotion
    ? "animate-in fade-in-0 duration-200"
    : "animate-in slide-in-from-right-8 fade-in-0 duration-300"

  return (
    <article
      data-testid="catalog-detail-panel"
      data-entry-id={entry.id}
      data-entry-state={state}
      data-entry-family={family}
      data-motion-level={motionLevel}
      data-reduced-motion={reducedMotion ? "true" : "false"}
      data-deps-count={dependencies.length}
      data-usedby-count={usedByWorkspaces.length}
      data-activity-total={activity.length}
      data-activity-visible={visibleActivity.length}
      aria-label={`${entry.displayName} — ${stateMeta.label}`}
      className={[
        "relative flex flex-col rounded-md border bg-[var(--card)] p-4 md:p-5",
        stateMeta.borderClass,
        slideInClass,
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {/* ── Top bar: back · family · state · vendor · version ─────── */}
      <header
        data-testid="catalog-detail-panel-toolbar"
        className="mb-3 flex flex-wrap items-center gap-2 border-b border-[var(--border)]/60 pb-3"
      >
        <button
          type="button"
          data-testid="catalog-detail-panel-back"
          onClick={handleBack}
          aria-label="Back to catalog grid"
          className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-2 py-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)] hover:border-[var(--neural-blue)] hover:text-[var(--foreground)]"
        >
          <ArrowLeft size={12} aria-hidden />
          back
        </button>
        <span
          data-testid="catalog-detail-panel-family-chip"
          data-family={family}
          className={[
            "inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
            FAMILY_ACCENT[family],
          ].join(" ")}
        >
          {FAMILY_LABEL[family]}
        </span>
        <span
          data-testid="catalog-detail-panel-state-chip"
          data-state-chip={state}
          className={[
            "inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
            stateMeta.chipClass,
          ].join(" ")}
        >
          {stateMeta.label}
        </span>
        <span
          data-testid="catalog-detail-panel-vendor"
          className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]"
        >
          {entry.vendor}
        </span>
        {entry.version && (
          <span
            data-testid="catalog-detail-panel-version"
            className="font-mono text-[10px] text-[var(--muted-foreground)]"
          >
            v{entry.version}
          </span>
        )}
      </header>

      {/* ── Two-column body: orb (left) + sections (right) ────────── */}
      <div className="grid grid-cols-1 gap-5 md:grid-cols-[160px_1fr] md:items-start">
        {/* ── Left: energy orb ──────────────────────────────────── */}
        <EnergyOrb
          state={state}
          progress={progress}
          orb={orb}
          reducedMotion={reducedMotion}
        />

        {/* ── Right: stacked sections ──────────────────────────── */}
        <div className="flex min-w-0 flex-col gap-4">
          {/* Header: name + description */}
          <section data-testid="catalog-detail-panel-header">
            <h2
              data-testid="catalog-detail-panel-name"
              className="font-orbitron text-lg tracking-wide text-[var(--foreground)]"
            >
              {entry.displayName}
            </h2>
            {entry.description && (
              <p
                data-testid="catalog-detail-panel-description"
                className="mt-1 text-xs text-[var(--muted-foreground)]"
              >
                {entry.description}
              </p>
            )}
            {/* Update-available next-version sub-line */}
            {state === "update-available" &&
              typeof entry.metadata?.nextVersion === "string" && (
                <p
                  data-testid="catalog-detail-panel-update-version"
                  className="mt-1 font-mono text-[11px] text-[var(--artifact-purple)]"
                >
                  → v{entry.metadata.nextVersion}
                </p>
              )}
            {/* Failed-state failure reason */}
            {state === "failed" &&
              typeof entry.metadata?.failureReason === "string" && (
                <p
                  data-testid="catalog-detail-panel-error-message"
                  className="mt-1 font-mono text-[11px] text-[var(--critical-red)]/85"
                >
                  {entry.metadata.failureReason}
                </p>
              )}
          </section>

          {/* Dependencies */}
          <DependenciesSection
            dependencies={dependencies}
            onSelect={onSelectDependency}
          />

          {/* Used by workspaces */}
          <UsedByWorkspacesSection
            workspaces={usedByWorkspaces}
            onSelect={onSelectWorkspace}
          />

          {/* Activity audit */}
          <ActivitySection
            visible={visibleActivity}
            hiddenCount={hiddenActivityCount}
            nowMs={nowMs}
          />

          {/* Footer CTA */}
          <DetailFooterAction
            entry={entry}
            state={state}
            onInstall={onInstall}
            onRetry={onRetry}
            onViewLog={onViewLog}
          />
        </div>
      </div>
    </article>
  )
}

// ─────────────────────────────────────────────────────────────────────
// EnergyOrb — three concentric rings + animated core.
// ─────────────────────────────────────────────────────────────────────

interface EnergyOrbProps {
  state: CatalogInstallState
  progress: number
  orb: EnergyOrbState
  reducedMotion: boolean
}

function EnergyOrb({ state, progress, orb, reducedMotion }: EnergyOrbProps) {
  // For installing state, layer a conic-gradient progress ring on top
  // of the core so the orb tells the same visual story as the card's
  // 2-px conic ring. Reduced-motion users get a static disc + the
  // percent label only — no ring spin, no halo pulse.
  const innerRingClass =
    reducedMotion || state !== "installing"
      ? ""
      : orb.innerRingFast
        ? "ring-spin"
        : "orbital-rotate"
  const middleRingClass = reducedMotion ? "" : "ring-spin-reverse"
  const outerRingClass = reducedMotion ? "" : "orbital-rotate"
  const rootAnimClass = reducedMotion ? "" : orb.rootAnimationClass

  const conicStyle =
    state === "installing"
      ? { background: buildInstallProgressGradient(progress) }
      : undefined

  return (
    <div
      data-testid="catalog-detail-panel-energy-orb"
      data-orb-state={state}
      data-orb-progress={progress.toFixed(2)}
      data-orb-inner-fast={orb.innerRingFast ? "true" : "false"}
      aria-label={`Energy orb — ${state}`}
      className={[
        "relative mx-auto flex aspect-square w-[160px] items-center justify-center md:w-full",
        rootAnimClass,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <svg
        viewBox="-100 -100 200 200"
        width="100%"
        height="100%"
        role="img"
        aria-hidden
        data-testid="catalog-detail-panel-energy-orb-svg"
        style={{ filter: orb.glowFilter }}
      >
        {/* Outermost faint guide ring. */}
        <g className={outerRingClass} style={{ transformOrigin: "center" }}>
          <circle
            cx="0"
            cy="0"
            r="92"
            fill="none"
            stroke={orb.haloColor}
            strokeWidth="1"
            strokeDasharray="4 6"
            data-testid="catalog-detail-panel-energy-orb-ring-outer"
          />
        </g>
        {/* Middle reverse-spin ring with tick marks. */}
        <g className={middleRingClass} style={{ transformOrigin: "center" }}>
          <circle
            cx="0"
            cy="0"
            r="68"
            fill="none"
            stroke={orb.coreColor}
            strokeOpacity="0.55"
            strokeWidth="1.2"
            strokeDasharray="2 8"
            data-testid="catalog-detail-panel-energy-orb-ring-middle"
          />
        </g>
        {/* Inner spin ring (matches card's progress sweep direction). */}
        <g className={innerRingClass} style={{ transformOrigin: "center" }}>
          <circle
            cx="0"
            cy="0"
            r="46"
            fill="none"
            stroke={orb.coreColor}
            strokeOpacity="0.85"
            strokeWidth="2"
            strokeDasharray="5 4"
            data-testid="catalog-detail-panel-energy-orb-ring-inner"
          />
        </g>
        {/* Core disc — solid for non-installing, conic ring overlaid on
         *  installing via the absolute div below so it can use CSS
         *  `background: conic-gradient(...)`. */}
        <circle
          cx="0"
          cy="0"
          r="28"
          fill={orb.coreColor}
          fillOpacity="0.18"
          stroke={orb.coreColor}
          strokeWidth="1.5"
          data-testid="catalog-detail-panel-energy-orb-core"
        />
      </svg>
      {/* Conic ring overlay (installing only). Sized to match the inner
       *  SVG ring at r=46 → 92px diameter. */}
      {conicStyle && (
        <div
          data-testid="catalog-detail-panel-energy-orb-progress-ring"
          aria-hidden
          className="pointer-events-none absolute h-[58%] w-[58%] rounded-full p-[2px]"
          style={conicStyle}
        >
          <div className="h-full w-full rounded-full bg-[var(--card)]" />
        </div>
      )}
      {/* Centred label (state-specific). */}
      <div
        data-testid="catalog-detail-panel-energy-orb-label"
        className="pointer-events-none absolute font-orbitron text-xs tracking-wider"
        style={{ color: orb.coreColor }}
      >
        {orb.centerLabel}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// DependenciesSection — chip strip with status dots.
// ─────────────────────────────────────────────────────────────────────

interface DependenciesSectionProps {
  dependencies: ReadonlyArray<CatalogDetailDependency>
  onSelect?: (depId: string) => void
}

function DependenciesSection({
  dependencies,
  onSelect,
}: DependenciesSectionProps) {
  return (
    <section
      data-testid="catalog-detail-panel-dependencies"
      data-deps-count={dependencies.length}
      aria-labelledby="catalog-detail-panel-deps-heading"
    >
      <SectionHeading
        id="catalog-detail-panel-deps-heading"
        icon={Layers}
        label="Dependencies"
        count={dependencies.length}
      />
      {dependencies.length === 0 ? (
        <p
          data-testid="catalog-detail-panel-deps-empty"
          className="mt-2 font-mono text-[11px] text-[var(--muted-foreground)]"
        >
          No dependencies.
        </p>
      ) : (
        <ul
          data-testid="catalog-detail-panel-deps-list"
          className="mt-2 flex flex-wrap items-center gap-1.5"
        >
          {dependencies.map((dep) => {
            const interactive = Boolean(onSelect)
            const Tag = interactive ? "button" : "span"
            return (
              <li
                key={dep.id}
                data-testid={`catalog-detail-panel-dep-${dep.id}`}
                data-dep-state={dep.state ?? "unknown"}
              >
                <Tag
                  type={interactive ? ("button" as const) : undefined}
                  onClick={
                    interactive
                      ? () => {
                          if (onSelect) onSelect(dep.id)
                        }
                      : undefined
                  }
                  className={[
                    "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-mono text-[10px]",
                    dep.state
                      ? "border-[var(--border)] text-[var(--foreground)]"
                      : "border-dashed border-[var(--border)] text-[var(--muted-foreground)]",
                    interactive
                      ? "cursor-pointer hover:border-[var(--neural-blue)] hover:text-[var(--neural-blue)]"
                      : "",
                  ].join(" ")}
                >
                  {dep.state && (
                    <span
                      data-testid={`catalog-detail-panel-dep-${dep.id}-dot`}
                      aria-hidden
                      className={[
                        "inline-block h-1.5 w-1.5 rounded-full",
                        DEPENDENCY_DOT_FILL[dep.state],
                      ].join(" ")}
                    />
                  )}
                  <span className="truncate">
                    {dep.displayName ?? dep.id}
                  </span>
                </Tag>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────────
// UsedByWorkspacesSection — reverse refs.
// ─────────────────────────────────────────────────────────────────────

interface UsedByWorkspacesSectionProps {
  workspaces: ReadonlyArray<CatalogDetailUsedByWorkspace>
  onSelect?: (workspace: CatalogDetailUsedByWorkspace) => void
}

function UsedByWorkspacesSection({
  workspaces,
  onSelect,
}: UsedByWorkspacesSectionProps) {
  return (
    <section
      data-testid="catalog-detail-panel-usedby"
      data-usedby-count={workspaces.length}
      aria-labelledby="catalog-detail-panel-usedby-heading"
    >
      <SectionHeading
        id="catalog-detail-panel-usedby-heading"
        icon={Users}
        label="Used by workspaces"
        count={workspaces.length}
      />
      {workspaces.length === 0 ? (
        <p
          data-testid="catalog-detail-panel-usedby-empty"
          className="mt-2 font-mono text-[11px] text-[var(--muted-foreground)]"
        >
          No workspaces reference this entry yet.
        </p>
      ) : (
        <ul
          data-testid="catalog-detail-panel-usedby-list"
          className="mt-2 flex flex-col gap-1"
        >
          {workspaces.map((w) => {
            const accent =
              PRODUCT_LINE_ACCENT[w.productLine ?? "other"] ??
              PRODUCT_LINE_ACCENT.other
            return (
              <li
                key={w.id}
                data-testid={`catalog-detail-panel-usedby-${w.id}`}
                data-product-line={w.productLine ?? "other"}
              >
                <button
                  type="button"
                  onClick={() => {
                    if (onSelect) onSelect(w)
                  }}
                  disabled={!onSelect}
                  className={[
                    "group flex w-full items-center justify-between gap-2 rounded border px-2 py-1.5 text-left",
                    "border-[var(--border)] bg-[var(--background)]/30",
                    onSelect
                      ? "hover:border-[var(--neural-blue)]/60 hover:bg-[var(--background)]/60"
                      : "cursor-not-allowed opacity-60",
                  ].join(" ")}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span
                      data-testid={`catalog-detail-panel-usedby-${w.id}-accent`}
                      className={[
                        "inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider",
                        accent,
                      ].join(" ")}
                    >
                      {w.productLine ?? "other"}
                    </span>
                    <span className="truncate text-xs text-[var(--foreground)]">
                      {w.name}
                    </span>
                  </span>
                  <span className="inline-flex shrink-0 items-center gap-0.5 font-mono text-[10px] text-[var(--muted-foreground)] group-hover:text-[var(--neural-blue)]">
                    open
                    <ChevronRight size={10} aria-hidden />
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────────
// ActivitySection — timeline of audit events.
// ─────────────────────────────────────────────────────────────────────

interface ActivitySectionProps {
  visible: ReadonlyArray<CatalogDetailActivityEvent>
  hiddenCount: number
  nowMs: number
}

function ActivitySection({
  visible,
  hiddenCount,
  nowMs,
}: ActivitySectionProps) {
  return (
    <section
      data-testid="catalog-detail-panel-activity"
      data-activity-visible={visible.length}
      data-activity-hidden={hiddenCount}
      aria-labelledby="catalog-detail-panel-activity-heading"
    >
      <SectionHeading
        id="catalog-detail-panel-activity-heading"
        icon={Clock}
        label="Activity"
        count={visible.length + hiddenCount}
      />
      {visible.length === 0 ? (
        <p
          data-testid="catalog-detail-panel-activity-empty"
          className="mt-2 font-mono text-[11px] text-[var(--muted-foreground)]"
        >
          No recorded activity yet.
        </p>
      ) : (
        <ol
          data-testid="catalog-detail-panel-activity-list"
          className="mt-2 flex flex-col gap-1.5 border-l border-[var(--border)]/60 pl-3"
        >
          {visible.map((evt) => {
            const kind = coerceActivityKind(evt.kind)
            const meta = ACTIVITY_KIND_META[kind]
            const Icon = meta.icon
            return (
              <li
                key={evt.id}
                data-testid={`catalog-detail-panel-activity-event-${evt.id}`}
                data-activity-kind={kind}
                className="relative flex items-start gap-2"
              >
                <span
                  aria-hidden
                  className="absolute -left-[15px] top-1 inline-flex h-2 w-2 items-center justify-center rounded-full bg-[var(--card)] ring-2 ring-[var(--border)]"
                />
                <Icon
                  size={12}
                  aria-hidden
                  data-testid={`catalog-detail-panel-activity-event-${evt.id}-icon`}
                  className={["mt-0.5 shrink-0", meta.iconClass].join(" ")}
                />
                <div className="flex min-w-0 flex-col">
                  <div className="flex items-baseline gap-1.5">
                    <span
                      data-testid={`catalog-detail-panel-activity-event-${evt.id}-label`}
                      className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]"
                    >
                      {meta.label}
                    </span>
                    <time
                      dateTime={evt.timestamp}
                      data-testid={`catalog-detail-panel-activity-event-${evt.id}-time`}
                      className="font-mono text-[10px] text-[var(--muted-foreground)]"
                    >
                      {formatRelativeTime(evt.timestamp, nowMs)}
                    </time>
                  </div>
                  <p
                    data-testid={`catalog-detail-panel-activity-event-${evt.id}-message`}
                    className="text-xs text-[var(--foreground)]"
                  >
                    {evt.message}
                  </p>
                  {evt.actor && (
                    <p
                      data-testid={`catalog-detail-panel-activity-event-${evt.id}-actor`}
                      className="font-mono text-[10px] text-[var(--muted-foreground)]"
                    >
                      by {evt.actor}
                    </p>
                  )}
                </div>
              </li>
            )
          })}
          {hiddenCount > 0 && (
            <li
              data-testid="catalog-detail-panel-activity-more"
              className="font-mono text-[10px] text-[var(--muted-foreground)]"
            >
              + {hiddenCount} more in audit log
            </li>
          )}
        </ol>
      )}
    </section>
  )
}

// ─────────────────────────────────────────────────────────────────────
// SectionHeading — small icon + label + count badge. Shared by the
// three right-column sections.
// ─────────────────────────────────────────────────────────────────────

interface SectionHeadingProps {
  id: string
  icon: typeof CheckCircle2
  label: string
  count: number
}

function SectionHeading({ id, icon: Icon, label, count }: SectionHeadingProps) {
  return (
    <h3
      id={id}
      className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-[var(--muted-foreground)]"
    >
      <Icon size={11} aria-hidden />
      <span>{label}</span>
      <span
        data-testid={`${id}-count`}
        className="rounded-full border border-[var(--border)] px-1.5 font-mono text-[9px] text-[var(--foreground)]"
      >
        {count}
      </span>
    </h3>
  )
}

// ─────────────────────────────────────────────────────────────────────
// DetailFooterAction — primary CTA. Mirrors `<CatalogCard />`'s footer
// but laid out for the panel scale (larger button, full label).
// ─────────────────────────────────────────────────────────────────────

interface DetailFooterActionProps {
  entry: CatalogEntry
  state: CatalogInstallState
  onInstall?: (entry: CatalogEntry) => void
  onRetry?: (entry: CatalogEntry) => void
  onViewLog?: (entry: CatalogEntry) => void
}

function DetailFooterAction({
  entry,
  state,
  onInstall,
  onRetry,
  onViewLog,
}: DetailFooterActionProps) {
  // BS.6.7 — track which CTAs are still parked behind BS.7 (no
  // handler) so the wrapper tooltip surfaces "Install pipeline 即將
  // 上線" on hover / focus. Once BS.7 wires the handlers these flags
  // become false and `<PendingInstallTooltip>` collapses to a no-op
  // passthrough.
  const installPending = !onInstall
  const retryPending = !onRetry
  const viewLogPending = !onViewLog

  let cta: ReactNode
  switch (state) {
    case "available":
      cta = (
        <PendingInstallTooltip
          pending={installPending}
          testId="catalog-detail-panel-action-install-pending-tooltip"
        >
          <button
            type="button"
            data-testid="catalog-detail-panel-action-install"
            aria-label={`Install ${entry.displayName}`}
            title={installPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
            disabled={installPending}
            onClick={() => {
              if (onInstall) onInstall(entry)
            }}
            className="inline-flex items-center gap-1.5 rounded border border-[var(--neural-blue)]/55 bg-[var(--neural-blue)]/10 px-3 py-1.5 font-mono text-[11px] text-[var(--neural-blue)] hover:border-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/20 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-[var(--neural-blue)]/10"
          >
            <Download size={12} aria-hidden />
            Install
          </button>
        </PendingInstallTooltip>
      )
      break
    case "installed":
      cta = (
        <span
          data-testid="catalog-detail-panel-action-installed-badge"
          className="inline-flex items-center gap-1.5 font-mono text-[11px] text-[var(--validation-emerald)]"
        >
          <CheckCircle2 size={12} aria-hidden />
          Installed
        </span>
      )
      break
    case "installing":
      cta = (
        <span
          data-testid="catalog-detail-panel-action-installing-label"
          className="inline-flex items-center gap-1.5 font-mono text-[11px] text-[var(--neural-blue)]"
        >
          <Loader2 size={12} className="ring-spin" aria-hidden />
          Installing…
        </span>
      )
      break
    case "update-available":
      cta = (
        <PendingInstallTooltip
          pending={installPending}
          testId="catalog-detail-panel-action-update-pending-tooltip"
        >
          <button
            type="button"
            data-testid="catalog-detail-panel-action-update"
            aria-label={`Update ${entry.displayName}`}
            title={installPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
            disabled={installPending}
            onClick={() => {
              if (onInstall) onInstall(entry)
            }}
            className="inline-flex items-center gap-1.5 rounded border border-[var(--artifact-purple)]/55 bg-[var(--artifact-purple)]/10 px-3 py-1.5 font-mono text-[11px] text-[var(--artifact-purple)] hover:border-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)]/20 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-[var(--artifact-purple)]/10"
          >
            <ArrowUpCircle size={12} aria-hidden />
            Update
          </button>
        </PendingInstallTooltip>
      )
      break
    case "failed":
      cta = (
        <span className="inline-flex items-center gap-2">
          <PendingInstallTooltip
            pending={retryPending}
            testId="catalog-detail-panel-action-retry-pending-tooltip"
          >
            <button
              type="button"
              data-testid="catalog-detail-panel-action-retry"
              aria-label={`Retry installing ${entry.displayName}`}
              title={retryPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
              disabled={retryPending}
              onClick={() => {
                if (onRetry) onRetry(entry)
              }}
              className="inline-flex items-center gap-1.5 rounded border border-[var(--critical-red)]/55 bg-[var(--critical-red)]/10 px-3 py-1.5 font-mono text-[11px] text-[var(--critical-red)] hover:border-[var(--critical-red)] hover:bg-[var(--critical-red)]/20 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-[var(--critical-red)]/10"
            >
              <RefreshCw size={12} aria-hidden />
              Retry
            </button>
          </PendingInstallTooltip>
          <PendingInstallTooltip
            pending={viewLogPending}
            testId="catalog-detail-panel-action-view-log-pending-tooltip"
          >
            <button
              type="button"
              data-testid="catalog-detail-panel-action-view-log"
              aria-label={`View install log for ${entry.displayName}`}
              title={viewLogPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
              disabled={viewLogPending}
              onClick={() => {
                if (onViewLog) onViewLog(entry)
              }}
              className="inline-flex items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 font-mono text-[11px] text-[var(--muted-foreground)] hover:border-[var(--neural-blue)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-60"
            >
              <ExternalLink size={12} aria-hidden />
              View log
            </button>
          </PendingInstallTooltip>
        </span>
      )
      break
    default: {
      // TS exhaustiveness guard — adding a new install state without
      // wiring the panel's footer becomes a compile-time error.
      const _exhaustive: never = state
      cta = null as ReactNode & typeof _exhaustive
    }
  }

  return (
    <footer
      data-testid="catalog-detail-panel-footer"
      data-footer-state={state}
      className="mt-2 flex items-center justify-end border-t border-[var(--border)]/60 pt-3"
    >
      {cta}
    </footer>
  )
}

export default CatalogDetailPanel

"use client"

/**
 * BS.6.4 — Polished family chip strip.
 *
 * Replaces BS.6.1's inline `<FamilyChip />` block inside `<CatalogTab />`
 * with a richer visual treatment that matches the BS spec:
 *
 *   • 6 chips: `All` + the 5 catalog families (Mobile / Embedded / Web
 *     / Software / Custom). Order matches BS.6.1's `CATALOG_FAMILIES`
 *     const tuple so deep-linkers and BS.6.8 tests share one source of
 *     truth.
 *   • Each family carries its own OmniSight palette accent — emerald
 *     for Mobile, amber for Embedded, neural-blue for Web, artifact-
 *     purple for Software, rose for Custom. `All` collapses to the
 *     dashboard's default neural-blue. Inactive chips render as muted
 *     pills with the accent border at low opacity; hover lifts the
 *     accent slightly so operators can preview which colour family
 *     they're about to commit to.
 *   • **Active chip dressing** (the contract this row owns):
 *       1. `corner-brackets-full-tinted` — four short L-brackets at
 *          the chip corners painted in the family accent via the
 *          `--cb-color` custom property. Mirrors the existing
 *          `.corner-brackets-full` tactical-HUD pattern but tinted so
 *          each family reads its own palette.
 *       2. `category-strip-scan-sweep` — a thin neural-line bar that
 *          sweeps top→bottom inside the chip on a 2.4s loop, tinted
 *          with `--scan-sweep-color`. This is the localised cousin of
 *          `.fui-scan-sweep` (the viewport-wide setup-required overlay);
 *          the new utility is `position: absolute` + `inset: 0` +
 *          `border-radius: inherit` so it clips to the chip's pill
 *          silhouette and does not touch the document. Reduced-motion
 *          users (OS `prefers-reduced-motion: reduce`) see the static
 *          chip with brackets only — the CSS halts the animation +
 *          hides the sweep.
 *       3. Solid family-accent text + 15% accent background, plus a
 *          stronger ring around the chip so the active state reads at
 *          a glance even without the brackets / sweep (e.g. printer-
 *          friendly accessibility scenario).
 *
 * The chip strip stays state-light: filter selection lives in
 * `<CatalogTab />` via the `family` prop + `onSelect` callback. This
 * row owns visuals only — no useState, no useUserStorage, no SSE.
 *
 * Out of scope for this row (deferred to later BS.6.x):
 *   • BS.6.5 — virtualisation; the chip strip has 6 fixed chips, no
 *     virtualisation concern of its own.
 *   • BS.6.6 — BS.3 8-layer motion; the chip strip stays motion-light
 *     (only the per-chip scan sweep) so it doesn't compete with the
 *     card-grid cursor magnetic tilt that lands in BS.6.6.
 *   • BS.6.8 — unit tests live in their own row; this row exposes
 *     `data-testid="category-strip"` + `data-testid="category-strip-chip-${id}"`
 *     hooks for BS.6.8 to query against. Inside `<CatalogTab />` the
 *     prefix is overridden to `catalog-tab-family-chips` /
 *     `catalog-tab-family-chip-${id}` so the BS.6.1 testid contract
 *     carries through unchanged.
 *
 * Module-global state audit (per implement_phase_step.md Step 1)
 * ─────────────────────────────────────────────────────────────
 * No module-level mutable state. All visuals derive from props plus
 * the immutable `FAMILY_PALETTE` lookup table. Pure render, no
 * `useState` / `useEffect` / `useRef` / `Math.random` / `Date.now`.
 * SSR-deterministic. Browser-only — uvicorn `--workers N` does not
 * apply (answer #1: each browser tab derives the same view from the
 * same Next.js build artifact).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * N/A — pure presentation. Active family flows in via the `family`
 * prop; selection round-trips through `onSelect` to the parent. No
 * API calls, no cross-worker race at this layer.
 */

import {
  type CSSProperties,
  type ReactNode,
} from "react"
import {
  Cpu,
  Globe,
  LayoutGrid,
  Smartphone,
  Sparkles,
  TerminalSquare,
  type LucideIcon,
} from "lucide-react"

import {
  CATALOG_FAMILIES,
  type CatalogFamily,
} from "@/lib/catalog-families"

// ─────────────────────────────────────────────────────────────────────
// Public types — exported so BS.6.8 tests share one contract.
// ─────────────────────────────────────────────────────────────────────

export type CategoryStripFamilyId = CatalogFamily | "all"

/** Order rendered by `<CategoryStrip />` — `all` first as the reset
 *  chip, then the 5 catalog families in BS.6.1's frozen order. */
export const CATEGORY_STRIP_FAMILIES: ReadonlyArray<CategoryStripFamilyId> = [
  "all",
  ...CATALOG_FAMILIES,
]

interface FamilyPaletteEntry {
  /** Visible label rendered inside the chip. */
  label: string
  /** Lucide icon paired with the chip — gives operators a non-text
   *  cue for the family alongside the colour accent. */
  icon: LucideIcon
  /** Tailwind text class applied when the chip is active. */
  textActiveClass: string
  /** Tailwind text class applied when the chip is inactive (muted). */
  textInactiveClass: string
  /** Tailwind border class for the chip pill. */
  borderClass: string
  /** Tailwind background class for the active chip's accent fill. */
  bgActiveClass: string
  /** Tailwind ring class for the active chip's surrounding glow. */
  ringActiveClass: string
  /** Raw CSS colour fed into the `--cb-color` custom property so the
   *  tinted corner-brackets paint in the family's accent. Same value
   *  is reused for `--scan-sweep-color` (with reduced opacity). */
  hudColor: string
  /** RGBA tint for the localised scan-sweep so the bar reads as the
   *  family colour instead of the default neural cyan. Kept separate
   *  from `hudColor` so brackets stay opaque while the sweep stays
   *  translucent. */
  sweepColor: string
}

/** Family-by-family palette. Mobile / Embedded / Web / Software /
 *  Custom each carry their own OmniSight palette accent; `all` falls
 *  back to neural-blue. The Tailwind class literals are static so the
 *  JIT picks them up — no runtime concatenation, no missed classes. */
const FAMILY_PALETTE: Record<CategoryStripFamilyId, FamilyPaletteEntry> = {
  all: {
    label: "All",
    icon: LayoutGrid,
    textActiveClass: "text-[var(--neural-blue)]",
    textInactiveClass: "text-[var(--muted-foreground)]",
    borderClass: "border-[var(--neural-blue)]/55",
    bgActiveClass: "bg-[var(--neural-blue)]/15",
    ringActiveClass: "ring-1 ring-[var(--neural-blue)]/40",
    hudColor: "#38bdf8",
    sweepColor: "rgba(56, 189, 248, 0.38)",
  },
  mobile: {
    label: "Mobile",
    icon: Smartphone,
    textActiveClass: "text-emerald-300",
    textInactiveClass: "text-[var(--muted-foreground)]",
    borderClass: "border-emerald-500/55",
    bgActiveClass: "bg-emerald-500/15",
    ringActiveClass: "ring-1 ring-emerald-500/45",
    hudColor: "#10b981",
    sweepColor: "rgba(16, 185, 129, 0.38)",
  },
  embedded: {
    label: "Embedded",
    icon: Cpu,
    textActiveClass: "text-amber-300",
    textInactiveClass: "text-[var(--muted-foreground)]",
    borderClass: "border-amber-500/55",
    bgActiveClass: "bg-amber-500/15",
    ringActiveClass: "ring-1 ring-amber-500/45",
    hudColor: "#f59e0b",
    sweepColor: "rgba(245, 158, 11, 0.38)",
  },
  web: {
    label: "Web",
    icon: Globe,
    textActiveClass: "text-sky-300",
    textInactiveClass: "text-[var(--muted-foreground)]",
    borderClass: "border-sky-500/55",
    bgActiveClass: "bg-sky-500/15",
    ringActiveClass: "ring-1 ring-sky-500/45",
    hudColor: "#0ea5e9",
    sweepColor: "rgba(14, 165, 233, 0.38)",
  },
  software: {
    label: "Software",
    icon: TerminalSquare,
    textActiveClass: "text-violet-300",
    textInactiveClass: "text-[var(--muted-foreground)]",
    borderClass: "border-violet-500/55",
    bgActiveClass: "bg-violet-500/15",
    ringActiveClass: "ring-1 ring-violet-500/45",
    hudColor: "#a855f7",
    sweepColor: "rgba(168, 85, 247, 0.38)",
  },
  custom: {
    label: "Custom",
    icon: Sparkles,
    textActiveClass: "text-rose-300",
    textInactiveClass: "text-[var(--muted-foreground)]",
    borderClass: "border-rose-500/55",
    bgActiveClass: "bg-rose-500/15",
    ringActiveClass: "ring-1 ring-rose-500/45",
    hudColor: "#f43f5e",
    sweepColor: "rgba(244, 63, 94, 0.38)",
  },
}

/** Exported so BS.6.8 tests can introspect the per-family palette
 *  without re-deriving Tailwind classes. */
export function getCategoryStripPalette(
  id: CategoryStripFamilyId,
): FamilyPaletteEntry {
  return FAMILY_PALETTE[id]
}

// ─────────────────────────────────────────────────────────────────────
// Public component props.
// ─────────────────────────────────────────────────────────────────────

export interface CategoryStripProps {
  /** Currently-active filter. Drives which chip dresses up with corner
   *  brackets + scan-sweep. */
  family: CategoryStripFamilyId
  /** Click handler — receives the next family id (or `"all"` for the
   *  reset chip). The parent owns the actual state. */
  onSelect: (next: CategoryStripFamilyId) => void
  /** Optional per-family counts. When supplied the chip carries a small
   *  numeric badge so operators can see how many entries each family
   *  holds before committing to a filter. Missing keys default to no
   *  badge so callers can ship counts incrementally. */
  counts?: Partial<Record<CategoryStripFamilyId, number>>
  className?: string
  /** Override for the root `data-testid`. Defaults to
   *  `"category-strip"`; `<CatalogTab />` passes
   *  `"catalog-tab-family-chips"` so BS.6.1's testid contract carries
   *  through unchanged. */
  rootTestId?: string
  /** Override for the per-chip `data-testid` prefix. Defaults to
   *  `"category-strip-chip"`; `<CatalogTab />` passes
   *  `"catalog-tab-family-chip"` so BS.6.1's testid contract carries
   *  through unchanged. */
  chipTestIdPrefix?: string
  /** Optional ARIA label override. Defaults to "Filter by family"
   *  matching the BS.6.1 inline chip block contract. */
  ariaLabel?: string
}

// ─────────────────────────────────────────────────────────────────────
// CategoryStrip — main export.
// ─────────────────────────────────────────────────────────────────────

export function CategoryStrip({
  family,
  onSelect,
  counts,
  className,
  rootTestId = "category-strip",
  chipTestIdPrefix = "category-strip-chip",
  ariaLabel = "Filter by family",
}: CategoryStripProps) {
  return (
    <div
      data-testid={rootTestId}
      data-active-family={family}
      role="group"
      aria-label={ariaLabel}
      className={[
        "flex flex-wrap items-center gap-1",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {CATEGORY_STRIP_FAMILIES.map((id) => (
        <CategoryChip
          key={id}
          id={id}
          active={family === id}
          count={counts?.[id]}
          onSelect={() => onSelect(id)}
          testId={`${chipTestIdPrefix}-${id}`}
        />
      ))}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// CategoryChip — file-private. Active dressing renders the
// corner-brackets + scan-sweep overlays; inactive renders a clean pill.
// ─────────────────────────────────────────────────────────────────────

interface CategoryChipProps {
  id: CategoryStripFamilyId
  active: boolean
  count: number | undefined
  onSelect: () => void
  testId: string
}

function CategoryChip({ id, active, count, onSelect, testId }: CategoryChipProps) {
  const palette = FAMILY_PALETTE[id]
  const Icon = palette.icon

  // Custom-property carriers fed to the corner-brackets + scan-sweep
  // overlays. Per-family colours flow as inline style so the same CSS
  // utility (`.corner-brackets-full-tinted`, `.category-strip-scan-sweep`)
  // can be reused across families without exploding the class graph.
  const chipStyle: CSSProperties = {
    // CSS custom properties — TS doesn't model them in CSSProperties,
    // so we cast through a Record. The values are typed strings, no
    // injection surface (no user input flows into the inline style).
    ...(active
      ? ({
          ["--cb-color" as string]: palette.hudColor,
          ["--scan-sweep-color" as string]: palette.sweepColor,
        } as Record<string, string>)
      : {}),
  }

  // Class graph:
  //   • base — pill geometry, font, transitions.
  //   • inactive — neutral border + muted text + hover lift.
  //   • active — accent border (full opacity) + accent text + accent
  //     bg + ring + corner-brackets-full-tinted (renders 4 L-shaped
  //     brackets at the chip corners painted from `--cb-color`).
  const className = [
    "relative inline-flex h-7 select-none items-center gap-1 rounded-full border px-2.5 font-mono text-[10px] uppercase tracking-wider transition-colors",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--background)]",
    active
      ? [
          palette.borderClass.replace("/55", "/80"),
          palette.bgActiveClass,
          palette.textActiveClass,
          palette.ringActiveClass,
          "corner-brackets-full-tinted",
        ].join(" ")
      : [
          "border-[var(--border)]",
          palette.textInactiveClass,
          "hover:border-[var(--neural-blue)]/40",
          "hover:text-[var(--foreground)]",
        ].join(" "),
  ].join(" ")

  return (
    <button
      type="button"
      aria-pressed={active}
      aria-label={`Filter: ${palette.label}`}
      data-testid={testId}
      data-active={active}
      data-family={id}
      onClick={onSelect}
      className={className}
      style={chipStyle}
    >
      {/* Localised scan-sweep — pointer-events:none, position:absolute,
       *  border-radius:inherit so it clips to the pill. Mounted only
       *  when the chip is active; the `@media (prefers-reduced-motion:
       *  reduce)` rule in globals.css halts the sweep + hides it for
       *  vestibular-affected operators. */}
      {active && (
        <span
          aria-hidden
          data-testid={`${testId}-scan-sweep`}
          className="category-strip-scan-sweep"
        />
      )}
      {/* Foreground content — wrap in a relative span so it renders
       *  above the absolutely-positioned sweep / corner-brackets
       *  pseudo-elements. */}
      <span className="relative z-[1] inline-flex items-center gap-1">
        <Icon size={12} aria-hidden />
        <span>{palette.label}</span>
        {typeof count === "number" && count >= 0 ? (
          <ChipBadge active={active} count={count} testId={`${testId}-count`} />
        ) : null}
      </span>
    </button>
  )
}

interface ChipBadgeProps {
  active: boolean
  count: number
  testId: string
}

function ChipBadge({ active, count, testId }: ChipBadgeProps): ReactNode {
  return (
    <span
      data-testid={testId}
      className={[
        "ml-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full px-1 font-mono text-[9px] tabular-nums",
        active
          ? "bg-[var(--background)]/40 text-current"
          : "bg-[var(--card)] text-[var(--muted-foreground)]",
      ].join(" ")}
    >
      {count}
    </span>
  )
}

export default CategoryStrip

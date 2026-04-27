"use client"

/**
 * BS.6.2 — Catalog card, 5 install-state visuals.
 * BS.6.6 — extended with the BS.3 zero-gravity motion library. The
 *          existing visual root is wrapped by four motion layers
 *          (outer-to-inner: cursor magnetic tilt → idle floating drift
 *          → glass reflection → cursor-distance glow), a Layer-3
 *          orbital-rotate decoration on the `available` state Sparkles
 *          icon (dramatic only), and Layer-8 spring-press feedback on
 *          every footer call-to-action button. Each layer self-gates
 *          via `useEffectiveMotionLevel()` so reduced-motion / battery-
 *          critical / `motion: off` users see no listeners attached
 *          and no GPU layer cost. Hook order is stable across all five
 *          install states because the motion hooks live on the outer
 *          shell — the per-state visual variant (installing's conic
 *          gradient wrapper vs the plain card root) lives inside the
 *          motion layers and does not perturb the ref/listener graph.
 *
 * The polished card body that the catalog tab renders for each
 * `CatalogEntry`. Replaces BS.6.1's `<CatalogCardPlaceholder />` so
 * operators see a visually distinct treatment per install lifecycle:
 *
 *   • available        — dim cyan core + neutral border + faint inner
 *                        glow (entry is ready to install, no urgency).
 *   • installed        — emerald accent border + ✓ check icon + solid
 *                        emerald state chip (entry is live + healthy).
 *   • installing       — conic-gradient progress border + hazard-stripe
 *                        overlay + spinning ring icon (`.ring-spin`) +
 *                        live `{progress}%` readout. The conic angle
 *                        scales with `installProgressPercent` (or the
 *                        entry's `metadata.progressPercent`).
 *   • update-available — purple chip + `.pulse-purple` breathing pulse
 *                        on the chip + accent border (operator should
 *                        notice but no urgency).
 *   • failed           — critical-red border + `.force-turbo-armed`
 *                        aggressive heartbeat + retry / view-log slot
 *                        labels (operator must intervene).
 *
 * The card is **wired up to BS.6.1** via the `renderCard` prop on
 * `<CatalogTab />`: the page wrapper passes
 *   `(ctx) => <CatalogCard entry={ctx.entry} density={ctx.density}
 *                         cardPaddingClass={ctx.cardPaddingClass} />`
 * and BS.6.1's placeholder vanishes. The contract on the prop tuple
 * (`entry / density / cardPaddingClass`) matches the
 * `CatalogTabRenderContext` shape so wiring is one-liner change.
 *
 * Optional callbacks let later rows wire behaviour without touching
 * this file: `onSelect` (BS.6.3 detail panel expand), `onInstall` /
 * `onRetry` / `onViewLog` (BS.7 install pipeline + retry/log).
 * The footer always renders state-appropriate call-to-action **labels**
 * — buttons stay disabled when the corresponding handler is not wired
 * (no `onInstall` / `onRetry` / `onViewLog` from the parent), so
 * operators see the shape of the surface but can't trigger actions
 * before the install pipeline is wired (avoids a "click that does
 * nothing" UX hole).
 *
 * BS.6.7 — disabled-state tooltip
 * ────────────────────────────────
 * Until BS.7 lands the real install pipeline (`POST /installer/jobs`
 * → PEP gateway → sidecar), every footer button is rendered without a
 * handler from the page wrapper. To communicate why the affordance
 * looks "ready but inert", hovering / focusing a disabled button
 * surfaces a Radix tooltip with the exact text "Install pipeline 即將
 * 上線". The tooltip wrapper is a `<span tabIndex=0>` (because a
 * `<button disabled>` does not fire pointer events the Radix primitive
 * needs), and a native `title` attribute on the button itself is the
 * non-JS / SSR / screen-reader fallback. Once a parent wires a real
 * handler the wrapper is a no-op passthrough so no extra DOM is
 * introduced on the wired path. The exact tooltip text is exported as
 * `CATALOG_INSTALL_PENDING_TOOLTIP` so BS.6.8 / BS.7.1 tests can lock
 * it without scraping DOM strings.
 *
 * Out of scope for this row (deferred to later BS.6.x):
 *   • BS.6.3 — detail panel slide-out (this row only fires `onSelect`).
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. All visuals derive from props +
 * immutable look-up tables (`STATE_PALETTE`, `STATE_ICON`, `FAMILY_LABEL`,
 * `FAMILY_ACCENT`). The motion hooks added by BS.6.6
 * (`useCursorMagneticTilt` / `useFloatingCard` / `useGlassReflection` /
 * `useCursorDistanceGlow` / `useSpringPress`) are per-component-instance:
 * each owns its own `ref` + `useEffect` cleanup, so unmounting a card
 * (virtualizer scrolls it out, density toggle remounts the grid)
 * detaches every listener and clears the CSS variables it wrote.
 * Float-variant assignment is a deterministic hash of `entry.id` so
 * SSR + the first client render agree, and adjacent cards land on
 * different keyframe phases without a shared counter. Browser-only
 * render — uvicorn `--workers N` model does not apply (answer #1:
 * each browser tab derives the same view from the same Next.js build
 * artifact).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * N/A — pure presentation. Install progress flows through the `entry`
 * prop (or `installProgressPercent` override); BS.7's SSE wiring will
 * just re-render the card with a fresh percent. No API calls, no
 * cross-worker race at this layer. The motion hooks read
 * `useEffectiveMotionLevel()` synchronously off the same context the
 * rest of the page consumes, so a level downgrade (battery rule fires,
 * user toggles `motion: off`) propagates on the next React tick with
 * no cross-tab race.
 */

import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  useCallback,
  useMemo,
} from "react"
import {
  AlertOctagon,
  ArrowUpCircle,
  CheckCircle2,
  Download,
  Loader2,
  RefreshCw,
  ScrollText,
  Sparkles,
} from "lucide-react"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  type CatalogDensity,
  type CatalogEntry,
  type CatalogFamily,
  type CatalogInstallState,
  coerceFamily,
} from "@/components/omnisight/catalog-tab"
import {
  type FloatVariant,
  type MotionLevel,
  useCursorDistanceGlow,
  useCursorMagneticTilt,
  useEffectiveMotionLevel,
  useFloatingCard,
  useGlassReflection,
  useSpringPress,
} from "@/hooks/use-zero-g"

// ─────────────────────────────────────────────────────────────────────
// BS.6.7 — disabled-state tooltip text. Exported so BS.6.8 unit tests
// + BS.7.1 e2e checks can lock the exact message rather than scraping
// the DOM. Once BS.7 wires `onInstall`, the wrapper component becomes
// a transparent passthrough (no tooltip mounted, no extra DOM).
// ─────────────────────────────────────────────────────────────────────

export const CATALOG_INSTALL_PENDING_TOOLTIP = "Install pipeline 即將上線"

// ─────────────────────────────────────────────────────────────────────
// Public types — exported so BS.6.8 tests + BS.6.6 motion wrapper +
// BS.7 install pipeline share one source of truth for the card prop
// shape.
// ─────────────────────────────────────────────────────────────────────

export interface CatalogCardProps {
  entry: CatalogEntry
  density: CatalogDensity
  /** Tailwind class that the catalog tab derives from `density`
   *  (`p-2 text-[11px]` / `p-3 text-xs` / `p-4 text-sm`). Reused so
   *  every card variant scales uniformly with the density toggle. */
  cardPaddingClass: string
  /** Override the install progress (0..100) used by the installing
   *  state. Defaults to `entry.metadata.progressPercent` clamped via
   *  `clampInstallProgress()`. BS.7 install pipeline passes the live
   *  SSE value through this prop so the conic ring tracks downloads. */
  installProgressPercent?: number
  /** Click handler for the card body — BS.6.3 detail panel hook will
   *  pass `(entry) => openDetail(entry)`. Defaults to no-op so the
   *  card is still safe to render in BS.6.2 standalone preview. */
  onSelect?: (entry: CatalogEntry) => void
  /** Install action handler — kept opt-in. BS.6.7 wires the disabled
   *  tooltip + later BS.7 swaps in the real PEP-gated invocation. */
  onInstall?: (entry: CatalogEntry) => void
  /** Retry handler for `failed` state. Wired by BS.7.6. */
  onRetry?: (entry: CatalogEntry) => void
  /** "View log" handler for `failed` state. Wired by BS.7.6 via the
   *  log-tail modal. */
  onViewLog?: (entry: CatalogEntry) => void
  /** Override the BS.6.6 idle-drift float variant (a/b/c/d). When
   *  omitted the card derives a deterministic variant from `entry.id`
   *  so adjacent cards in the grid land on different keyframe phases
   *  without sharing a counter. The catalog tab can pass an explicit
   *  index to keep variant cycling stable across re-orders (sort key
   *  changes, search-driven shrink/regrow). */
  floatVariantIndex?: number
  className?: string
}

// ─────────────────────────────────────────────────────────────────────
// Pure helpers — exported so BS.6.8 tests can lock contract directly.
// ─────────────────────────────────────────────────────────────────────

/** Clamp an arbitrary value into the 0..100 install-progress band.
 *  Non-finite, missing, or negative values collapse to 0 so the conic
 *  gradient never draws a partial arc with garbage angles. Values
 *  above 100 saturate at 100 (BS.7 SSE may briefly publish 101 during
 *  post-extract finalisation; we don't want a wrap-around). */
export function clampInstallProgress(value: unknown): number {
  const num =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number(value)
        : NaN
  if (!Number.isFinite(num)) return 0
  if (num <= 0) return 0
  if (num >= 100) return 100
  return num
}

/** Lift the install state to its canonical bucket. Anything that is
 *  not one of the five lifecycle values defaults to `available` so a
 *  forward-compat install-state from a future catalog feed (e.g.
 *  `queued`) renders as a benign card instead of a blank panel. */
export function coerceInstallState(
  value: string | null | undefined,
): CatalogInstallState {
  switch (value) {
    case "available":
    case "installed":
    case "installing":
    case "update-available":
    case "failed":
      return value
    default:
      return "available"
  }
}

/** BS.6.6 — pick an idle-drift keyframe variant (a/b/c/d) based on a
 *  numeric seed. Pure cycle so identical seeds map to the same variant
 *  on SSR + client (no hydration mismatch) and adjacent indices land on
 *  different phases (no synced wave across the grid). Exported so BS.6.8
 *  tests can lock the cycle without inspecting CSS. */
export const CATALOG_CARD_FLOAT_VARIANTS = ["a", "b", "c", "d"] as const

export function pickCatalogCardFloatVariant(
  seed: number | string,
): FloatVariant {
  let n: number
  if (typeof seed === "number") {
    n = Number.isFinite(seed) ? Math.trunc(seed) : 0
  } else {
    // FNV-1a-ish accumulator over the id string. Cheap (no allocation,
    // no hash function dependency), and deterministic — same id always
    // returns the same variant so SSR HTML matches the client render.
    let acc = 2166136261
    for (let i = 0; i < seed.length; i++) {
      acc ^= seed.charCodeAt(i)
      acc = (acc * 16777619) >>> 0
    }
    n = acc
  }
  const idx = Math.abs(n) % CATALOG_CARD_FLOAT_VARIANTS.length
  return CATALOG_CARD_FLOAT_VARIANTS[idx]
}

// ─────────────────────────────────────────────────────────────────────
// State → visual lookup tables. Tailwind class literals must be
// statically present for the JIT to pick them up; we therefore keep
// the mapping exhaustive across `CatalogInstallState` rather than
// composing class names at runtime.
// ─────────────────────────────────────────────────────────────────────

interface StatePalette {
  /** Label rendered in the state chip (footer-right). */
  label: string
  /** Tailwind class for the state chip itself (border + text). */
  chipClass: string
  /** Tailwind class for the card border + accent halo. */
  borderClass: string
  /** Optional Tailwind class layered on top of the card root for the
   *  state-defining animation (force-turbo-armed for failed,
   *  pulse-purple for update-available, ring-spin doesn't go here —
   *  it lives on the icon). Empty string when the state has no root
   *  animation. */
  rootAnimationClass: string
  /** Footer call-to-action label (always rendered, button stays
   *  disabled per BS.6.7 spec). */
  footerLabel: string
  /** Aria-friendly status text used by the card's `aria-label`. */
  statusText: string
}

const STATE_PALETTE: Record<CatalogInstallState, StatePalette> = {
  available: {
    label: "Available",
    chipClass:
      "border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]",
    borderClass:
      "border-[var(--neural-blue)]/30 hover:border-[var(--neural-blue)]/60",
    rootAnimationClass: "",
    footerLabel: "Install",
    statusText: "Available to install",
  },
  installed: {
    label: "Installed",
    chipClass:
      "border-[var(--validation-emerald)]/50 bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)]",
    borderClass: "border-[var(--validation-emerald)]/55",
    rootAnimationClass: "",
    footerLabel: "Installed",
    statusText: "Installed and healthy",
  },
  installing: {
    label: "Installing",
    chipClass:
      "border-[var(--neural-blue)]/55 bg-[var(--neural-blue)]/15 text-[var(--neural-blue)]",
    // Border is owned by the conic-gradient outer wrapper; inner card
    // keeps a transparent border so the gradient shows through.
    borderClass: "border-transparent",
    rootAnimationClass: "",
    footerLabel: "Installing",
    statusText: "Installing — download in progress",
  },
  "update-available": {
    label: "Update available",
    chipClass:
      "border-[var(--artifact-purple)]/55 bg-[var(--artifact-purple)]/15 text-[var(--artifact-purple)] pulse-purple",
    borderClass: "border-[var(--artifact-purple)]/45",
    rootAnimationClass: "",
    footerLabel: "Update",
    statusText: "Update available",
  },
  failed: {
    label: "Failed",
    chipClass:
      "border-[var(--critical-red)]/55 bg-[var(--critical-red)]/15 text-[var(--critical-red)]",
    borderClass: "border-[var(--critical-red)]/65",
    rootAnimationClass: "force-turbo-armed",
    footerLabel: "Retry",
    statusText: "Install failed — retry required",
  },
}

const STATE_ICON: Record<CatalogInstallState, typeof CheckCircle2> = {
  available: Sparkles,
  installed: CheckCircle2,
  installing: Loader2,
  "update-available": ArrowUpCircle,
  failed: AlertOctagon,
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

// ─────────────────────────────────────────────────────────────────────
// BS.6.7 — `<PendingInstallTooltip />` wraps a footer call-to-action
// button while it has no handler wired (no `onInstall` / `onRetry` /
// `onViewLog`). Hovering or focusing the wrapper surfaces the
// "Install pipeline 即將上線" message via Radix Tooltip; once a
// handler is wired the component is a transparent passthrough so no
// extra DOM is added on the wired path. Exported so the catalog
// detail panel (BS.6.3) can reuse the same affordance for its larger
// CTA buttons without duplicating the helper.
// ─────────────────────────────────────────────────────────────────────

interface PendingInstallTooltipProps {
  /** When `true`, the wrapped button has no handler yet — render the
   *  tooltip so operators understand the affordance is intentional but
   *  parked behind BS.7. When `false`, render children as-is (no
   *  TooltipProvider mounted, no wrapper span, no tab stop, no extra
   *  DOM) so the wired path is zero-overhead. */
  pending: boolean
  /** Optional testid for the wrapper span. The tooltip content also
   *  picks up `${testId}-content` so BS.6.8 / BS.7.1 tests can locate
   *  the popover without inspecting Radix portal children. */
  testId?: string
  children: ReactNode
}

export function PendingInstallTooltip({
  pending,
  testId,
  children,
}: PendingInstallTooltipProps) {
  if (!pending) return <>{children}</>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid={testId}
          data-pending-install-tooltip="true"
          tabIndex={0}
          aria-label={CATALOG_INSTALL_PENDING_TOOLTIP}
          className="inline-flex rounded outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-blue)]/60"
        >
          {children}
        </span>
      </TooltipTrigger>
      <TooltipContent
        data-testid={testId ? `${testId}-content` : undefined}
        side="top"
        sideOffset={4}
        className="border border-[var(--border)] bg-[var(--card)] font-mono text-[10px] tracking-wide text-[var(--foreground)]"
      >
        {CATALOG_INSTALL_PENDING_TOOLTIP}
      </TooltipContent>
    </Tooltip>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Conic-gradient progress border helper. The installing state wraps
// the card in a 2-px padding shell whose background paints the
// conic-gradient. The inner card paints over the centre, leaving the
// 2-px ring visible as the progress arc. Kept as a pure helper so
// BS.6.8 tests can verify the gradient stops without parsing CSS.
// ─────────────────────────────────────────────────────────────────────

export function buildInstallProgressGradient(progress: number): string {
  const clamped = clampInstallProgress(progress)
  // `from -90deg` puts the 0% mark at 12 o'clock so the arc grows
  // clockwise from the top — matches the orbital-rotate sweep
  // direction the rest of `/settings/platforms` uses.
  return (
    `conic-gradient(from -90deg, ` +
    `var(--neural-blue) 0%, ` +
    `var(--neural-blue) ${clamped}%, ` +
    `rgba(56, 189, 248, 0.12) ${clamped}%, ` +
    `rgba(56, 189, 248, 0.12) 100%)`
  )
}

// ─────────────────────────────────────────────────────────────────────
// CatalogCard — main export.
// ─────────────────────────────────────────────────────────────────────

export function CatalogCard({
  entry,
  density,
  cardPaddingClass,
  installProgressPercent,
  onSelect,
  onInstall,
  onRetry,
  onViewLog,
  floatVariantIndex,
  className,
}: CatalogCardProps) {
  const state = coerceInstallState(entry.installState)
  const family = coerceFamily(entry.family)
  const palette = STATE_PALETTE[state]
  const StateIcon = STATE_ICON[state]

  // Resolve install progress: prop override wins, then metadata, then 0.
  // Memoised because the conic-gradient string is otherwise a fresh
  // allocation on every parent re-render even when nothing changed.
  const progress = useMemo(() => {
    if (typeof installProgressPercent === "number") {
      return clampInstallProgress(installProgressPercent)
    }
    return clampInstallProgress(entry.metadata?.progressPercent)
  }, [installProgressPercent, entry.metadata?.progressPercent])

  // The "next version" label for update-available; surfaced from
  // metadata when present, falls back to the generic chip text.
  const nextVersion =
    state === "update-available" && typeof entry.metadata?.nextVersion === "string"
      ? entry.metadata.nextVersion
      : undefined

  // Failed state error message for the operator. Surface the metadata
  // hint when present so operators don't have to open the log just to
  // see "shasum mismatch" / "network unreachable".
  const failureReason =
    state === "failed" && typeof entry.metadata?.failureReason === "string"
      ? entry.metadata.failureReason
      : undefined

  const handleCardClick = useCallback(
    (_event: ReactMouseEvent<HTMLElement>) => {
      if (onSelect) onSelect(entry)
    },
    [onSelect, entry],
  )

  const handleKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (!onSelect) return
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault()
        onSelect(entry)
      }
    },
    [onSelect, entry],
  )

  // ─── BS.6.6 — motion layers ─────────────────────────────────────
  // Outer-to-inner stack mirrors `<MotionPreview />` (BS.3.6) so each
  // hook owns its own ref + listener and the layers compose without
  // hook-order surprises:
  //   tilt  → wraps everything, applies perspective + rotateX/Y
  //           (Layer 6, normal + dramatic only).
  //   float → idle drift keyframe (Layer 1, all non-off levels).
  //   reflect → glass `::after` reflection (Layer 7, dramatic only).
  //   glow  → cursor-distance box-shadow (Layer 4, all non-off levels).
  // Each hook self-gates via `useEffectiveMotionLevel()`, so users
  // with `prefers-reduced-motion: reduce` / battery critical /
  // `motion: off` see no animation, no listeners, no GPU layer cost.
  // We surface the effective level + per-layer on/off on the outermost
  // wrapper so BS.6.8 tests can verify the resolver chain is plumbed
  // without inspecting computed CSS.
  const motionLevel = useEffectiveMotionLevel()
  // Variant cycles a/b/c/d — defaults to a deterministic hash of the
  // entry id so adjacent cards land on different keyframe phases
  // without a shared counter. Caller may override with an explicit
  // index (e.g. visible-row index) when stable cycling is required.
  const floatVariant = useMemo(
    () =>
      typeof floatVariantIndex === "number"
        ? CATALOG_CARD_FLOAT_VARIANTS[
            Math.abs(Math.trunc(floatVariantIndex)) %
              CATALOG_CARD_FLOAT_VARIANTS.length
          ]
        : pickCatalogCardFloatVariant(entry.id),
    [floatVariantIndex, entry.id],
  )
  const { ref: tiltRef, style: tiltStyle } =
    useCursorMagneticTilt<HTMLDivElement>({ maxTiltDeg: 6 })
  const { className: floatClassName, style: floatStyle } = useFloatingCard(
    floatVariant,
  )
  const { ref: reflectRef, className: reflectClassName } =
    useGlassReflection<HTMLDivElement>()
  const { ref: glowRef, className: glowClassName } =
    useCursorDistanceGlow<HTMLDivElement>({ maxDistancePx: 240 })

  // Body content shared by all states — name / vendor / version /
  // description / family chip / state chip / footer.
  const body = (
    <CardBody
      entry={entry}
      family={family}
      state={state}
      palette={palette}
      StateIcon={StateIcon}
      density={density}
      cardPaddingClass={cardPaddingClass}
      progress={progress}
      nextVersion={nextVersion}
      failureReason={failureReason}
      motionLevel={motionLevel}
      onInstall={onInstall}
      onRetry={onRetry}
      onViewLog={onViewLog}
    />
  )

  const interactive = Boolean(onSelect)
  // Click + keyboard handlers live on the outer-most motion wrapper so
  // the entire card area (including the motion-driven outer perimeter)
  // is the click target. `data-testid={catalog-card-{id}}` + entry/state
  // data-* + aria-label also sit here so BS.6.8 / BS.5 deep-link tests
  // continue to find the card by the same testid contract introduced in
  // BS.6.2 — only the wrapping element's role is now "motion shell"
  // instead of "visual shell". The visual root inside keeps every
  // existing visual class graph (border, padding, conic-gradient
  // shell for installing) and gets `data-card-visual` so tests can
  // distinguish the inner from the outer when needed.
  const interactiveProps = interactive
    ? {
        role: "button" as const,
        tabIndex: 0,
        onClick: handleCardClick,
        onKeyDown: handleKeyDown,
      }
    : {}

  // The installing state still wraps in a conic-gradient shell so the
  // 2-px ring continues to render the live progress arc. Other states
  // skip the inner wrapper to avoid a redundant DOM node.
  let visual: ReactNode
  if (state === "installing") {
    const gradientStyle: CSSProperties = {
      background: buildInstallProgressGradient(progress),
    }
    visual = (
      <div
        data-card-visual="installing"
        className={[
          "group relative h-full rounded-md p-[2px]",
          "transition-shadow duration-200",
        ].join(" ")}
        style={gradientStyle}
      >
        {/* Hazard stripe overlay sits above the conic ring but below
         *  the card body so the stripes feel like they belong to the
         *  border, not the content. `pointer-events-none` keeps it
         *  from swallowing card clicks. */}
        <div
          data-testid="catalog-card-hazard-overlay"
          aria-hidden
          className="force-turbo-hazard-overlay pointer-events-none absolute inset-0 rounded-md opacity-30"
        />
        <div
          data-testid="catalog-card-progress-ring"
          aria-hidden
          className="pointer-events-none absolute inset-0 rounded-md"
        />
        {body}
      </div>
    )
  } else {
    visual = (
      <div
        data-card-visual={state}
        className={[
          "group relative flex h-full flex-col rounded-md border bg-[var(--card)]",
          "transition-colors duration-200",
          palette.borderClass,
          palette.rootAnimationClass,
          cardPaddingClass,
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {body}
      </div>
    )
  }

  // Outer-most wrapper carries the click target + tilt transform. The
  // `rounded-md` is repeated on every motion layer so cursor-distance
  // glow / glass reflection clip to the card's pill shape rather than
  // the wrapper's bounding rectangle (would otherwise show a square
  // halo around a rounded card).
  return (
    <div
      ref={tiltRef}
      data-testid={`catalog-card-${entry.id}`}
      data-entry-id={entry.id}
      data-entry-family={family}
      data-state={state}
      data-motion-level={motionLevel}
      data-motion-float-variant={floatVariant}
      data-motion-float={floatClassName ? "on" : "off"}
      data-motion-tilt={tiltStyle.transform ? "on" : "off"}
      data-motion-reflect={reflectClassName ? "on" : "off"}
      data-motion-glow={glowClassName ? "on" : "off"}
      {...(state === "installing"
        ? { "data-progress": progress.toFixed(2) }
        : {})}
      aria-label={`${entry.displayName} — ${palette.statusText}`}
      style={tiltStyle}
      className={["relative h-full rounded-md", className ?? ""]
        .filter(Boolean)
        .join(" ")}
      {...interactiveProps}
    >
      <div
        data-testid="catalog-card-motion-float"
        style={floatStyle}
        className={["h-full rounded-md", floatClassName].filter(Boolean).join(" ")}
      >
        <div
          ref={reflectRef}
          data-testid="catalog-card-motion-reflect"
          className={["h-full rounded-md", reflectClassName]
            .filter(Boolean)
            .join(" ")}
        >
          <div
            ref={glowRef}
            data-testid="catalog-card-motion-glow"
            className={["h-full rounded-md", glowClassName]
              .filter(Boolean)
              .join(" ")}
          >
            {visual}
          </div>
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// CardBody — shared inner layout. Split out so the installing state
// can wrap it in its conic-gradient shell without duplicating layout
// markup.
// ─────────────────────────────────────────────────────────────────────

interface CardBodyProps {
  entry: CatalogEntry
  family: CatalogFamily
  state: CatalogInstallState
  palette: StatePalette
  StateIcon: typeof CheckCircle2
  density: CatalogDensity
  cardPaddingClass: string
  progress: number
  nextVersion: string | undefined
  failureReason: string | undefined
  /** Effective BS.3 motion level — drives Layer-3 orbital-rotate
   *  decoration on the available state's Sparkles icon (dramatic only,
   *  per ADR §5.7). All other visuals are level-agnostic; reduced-motion
   *  / battery rule already handled by the per-layer hooks. */
  motionLevel: MotionLevel
  onInstall?: (entry: CatalogEntry) => void
  onRetry?: (entry: CatalogEntry) => void
  onViewLog?: (entry: CatalogEntry) => void
}

function CardBody({
  entry,
  family,
  state,
  palette,
  StateIcon,
  density,
  cardPaddingClass,
  progress,
  nextVersion,
  failureReason,
  motionLevel,
  onInstall,
  onRetry,
  onViewLog,
}: CardBodyProps) {
  // For installing we render the body inside the conic-gradient shell,
  // so we need to paint the inner background ourselves (the parent's
  // `style.background` is the gradient ring). For every other state
  // the parent already owns the background.
  const isInstalling = state === "installing"
  const innerWrapperClass = isInstalling
    ? [
        "relative z-10 flex h-full flex-col rounded-[5px] border border-transparent bg-[var(--card)]",
        cardPaddingClass,
      ].join(" ")
    : "flex h-full flex-col"

  const showDescription = density !== "compact" && entry.description
  const showFooter = density !== "compact"

  return (
    <div className={innerWrapperClass}>
      {/* ── Header row: state icon + name + state chip ─────────────── */}
      <header className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <StateIcon
            data-testid="catalog-card-state-icon"
            data-state-icon={state}
            data-state-icon-orbital={
              state === "available" && motionLevel === "dramatic" ? "on" : "off"
            }
            size={14}
            aria-hidden
            className={
              state === "installing"
                ? "ring-spin shrink-0 text-[var(--neural-blue)]"
                : state === "installed"
                  ? "shrink-0 text-[var(--validation-emerald)]"
                  : state === "update-available"
                    ? "shrink-0 text-[var(--artifact-purple)]"
                    : state === "failed"
                      ? "shrink-0 text-[var(--critical-red)]"
                      : motionLevel === "dramatic"
                        ? // BS.6.6 Layer 3 — slow orbital rotation on the
                          // available state's Sparkles icon at dramatic
                          // level only. ADR §5.7 reserves orbital-rotate
                          // for the dramatic tier; subtle/normal/off keep
                          // the icon static. The class self-stops via
                          // R25.2's global prefers-reduced-motion fallback
                          // (animation-duration: 0.01ms !important) so OS
                          // reduce-motion users see the icon at rest even
                          // if their motion-pref is dramatic.
                          "orbital-rotate shrink-0 text-[var(--neural-blue)]/70"
                        : "shrink-0 text-[var(--neural-blue)]/70"
            }
          />
          <h3
            data-testid="catalog-card-name"
            className="truncate font-orbitron text-[length:inherit] tracking-wide text-[var(--foreground)]"
            title={entry.displayName}
          >
            {entry.displayName}
          </h3>
        </div>
        <span
          data-testid="catalog-card-state-chip"
          data-state-chip={state}
          className={[
            "shrink-0 rounded-full border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider",
            palette.chipClass,
          ].join(" ")}
        >
          {palette.label}
        </span>
      </header>

      {/* ── Vendor + version line ──────────────────────────────────── */}
      <div className="mt-0.5 flex items-baseline gap-1.5 truncate text-[10px] text-[var(--muted-foreground)]">
        <span data-testid="catalog-card-vendor" className="truncate">
          {entry.vendor}
        </span>
        {entry.version && (
          <span data-testid="catalog-card-version" className="font-mono">
            v{entry.version}
          </span>
        )}
      </div>

      {/* ── Optional description (hidden in compact density) ───────── */}
      {showDescription && (
        <p
          data-testid="catalog-card-description"
          className="mt-1 line-clamp-2 text-[11px] leading-snug text-[var(--muted-foreground)]"
        >
          {entry.description}
        </p>
      )}

      {/* ── Update-available next-version sub-line ─────────────────── */}
      {state === "update-available" && nextVersion && (
        <p
          data-testid="catalog-card-update-version"
          className="mt-1 font-mono text-[10px] text-[var(--artifact-purple)]"
        >
          → v{nextVersion}
        </p>
      )}

      {/* ── Installing live progress readout ───────────────────────── */}
      {state === "installing" && (
        <div
          data-testid="catalog-card-progress-block"
          className="mt-1.5 flex items-center justify-between font-mono text-[10px] text-[var(--neural-blue)]"
        >
          <span data-testid="catalog-card-progress-value">
            {progress.toFixed(0)}%
          </span>
          <span className="text-[var(--muted-foreground)]">downloading…</span>
        </div>
      )}

      {/* ── Failed reason hint (collapsed when no metadata) ───────── */}
      {state === "failed" && failureReason && (
        <p
          data-testid="catalog-card-error-message"
          className="mt-1 line-clamp-2 font-mono text-[10px] leading-snug text-[var(--critical-red)]/85"
        >
          {failureReason}
        </p>
      )}

      {/* ── Spacer keeps footer pinned to the bottom in fixed-height
       *      grid rows (BS.6.5 virtualisation will fix the row height) ── */}
      <div className="grow" />

      {/* ── Footer row: family chip + state CTA ───────────────────── */}
      {showFooter && (
        <footer
          data-testid="catalog-card-footer"
          className="mt-2 flex items-center justify-between gap-2"
        >
          <span
            data-testid="catalog-card-family-chip"
            data-family={family}
            className={[
              "inline-flex items-center rounded-full border px-1.5 py-0.5 text-[9px] uppercase tracking-wider",
              FAMILY_ACCENT[family],
            ].join(" ")}
          >
            {FAMILY_LABEL[family]}
          </span>
          <FooterAction
            entry={entry}
            state={state}
            palette={palette}
            onInstall={onInstall}
            onRetry={onRetry}
            onViewLog={onViewLog}
          />
        </footer>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// FooterAction — per-state CTA cluster. All buttons stay disabled
// until BS.6.7 lands so operators see the affordance without being
// able to fire a half-wired action. The handlers are present so BS.6.7
// can flip `disabled` to a guarded variant without restructuring DOM.
// ─────────────────────────────────────────────────────────────────────

interface FooterActionProps {
  entry: CatalogEntry
  state: CatalogInstallState
  palette: StatePalette
  onInstall?: (entry: CatalogEntry) => void
  onRetry?: (entry: CatalogEntry) => void
  onViewLog?: (entry: CatalogEntry) => void
}

function FooterAction({
  entry,
  state,
  palette,
  onInstall,
  onRetry,
  onViewLog,
}: FooterActionProps) {
  const stop = (e: ReactMouseEvent<HTMLButtonElement>) => e.stopPropagation()

  // BS.6.6 Layer 8 — spring-press click feedback on every footer call-
  // to-action button. Independent of motion level (operator always
  // wants click acknowledgement); the keyframe itself becomes a no-op
  // via the global `prefers-reduced-motion` fallback at globals.css
  // line ~1590, so OS reduce-motion users still get the visual press
  // state without the overshoot. We mount one hook per button slot;
  // hook order is stable per-state branch.
  const installPress = useSpringPress()
  const updatePress = useSpringPress()
  const retryPress = useSpringPress()
  const viewLogPress = useSpringPress()

  // BS.6.7 — every state branch that renders a clickable button wraps
  // it in `<PendingInstallTooltip>` so disabled-by-default buttons
  // surface the "pipeline coming soon" message. The wrapper becomes a
  // no-op passthrough once a real handler lands (BS.7), so the wired
  // path adds zero DOM.
  const installPending = !onInstall
  const retryPending = !onRetry
  const viewLogPending = !onViewLog

  switch (state) {
    case "available":
      return (
        <PendingInstallTooltip
          pending={installPending}
          testId="catalog-card-action-install-pending-tooltip"
        >
          <button
            type="button"
            data-testid="catalog-card-action-install"
            aria-label={`Install ${entry.displayName}`}
            title={installPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
            disabled={installPending}
            onClick={(e) => {
              stop(e)
              if (onInstall) onInstall(entry)
            }}
            className={[
              installPress.className,
              "inline-flex items-center gap-1 rounded border border-[var(--neural-blue)]/40 px-1.5 py-0.5 font-mono text-[10px] text-[var(--neural-blue)] hover:border-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/10 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent",
            ].join(" ")}
            {...installPress.pressProps}
          >
            <Download size={10} />
            {palette.footerLabel}
          </button>
        </PendingInstallTooltip>
      )
    case "installed":
      return (
        <span
          data-testid="catalog-card-action-installed-badge"
          className="inline-flex items-center gap-1 font-mono text-[10px] text-[var(--validation-emerald)]"
        >
          <CheckCircle2 size={10} />
          {palette.footerLabel}
        </span>
      )
    case "installing":
      return (
        <span
          data-testid="catalog-card-action-installing-label"
          className="inline-flex items-center gap-1 font-mono text-[10px] text-[var(--neural-blue)]"
        >
          <Loader2 size={10} className="ring-spin" />
          {palette.footerLabel}…
        </span>
      )
    case "update-available":
      return (
        <PendingInstallTooltip
          pending={installPending}
          testId="catalog-card-action-update-pending-tooltip"
        >
          <button
            type="button"
            data-testid="catalog-card-action-update"
            aria-label={`Update ${entry.displayName}`}
            title={installPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
            disabled={installPending}
            onClick={(e) => {
              stop(e)
              if (onInstall) onInstall(entry)
            }}
            className={[
              updatePress.className,
              "inline-flex items-center gap-1 rounded border border-[var(--artifact-purple)]/55 px-1.5 py-0.5 font-mono text-[10px] text-[var(--artifact-purple)] hover:border-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)]/10 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent",
            ].join(" ")}
            {...updatePress.pressProps}
          >
            <ArrowUpCircle size={10} />
            {palette.footerLabel}
          </button>
        </PendingInstallTooltip>
      )
    case "failed":
      return (
        <span className="inline-flex items-center gap-1">
          <PendingInstallTooltip
            pending={retryPending}
            testId="catalog-card-action-retry-pending-tooltip"
          >
            <button
              type="button"
              data-testid="catalog-card-action-retry"
              aria-label={`Retry installing ${entry.displayName}`}
              title={retryPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
              disabled={retryPending}
              onClick={(e) => {
                stop(e)
                if (onRetry) onRetry(entry)
              }}
              className={[
                retryPress.className,
                "inline-flex items-center gap-1 rounded border border-[var(--critical-red)]/55 px-1.5 py-0.5 font-mono text-[10px] text-[var(--critical-red)] hover:border-[var(--critical-red)] hover:bg-[var(--critical-red)]/10 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent",
              ].join(" ")}
              {...retryPress.pressProps}
            >
              <RefreshCw size={10} />
              {palette.footerLabel}
            </button>
          </PendingInstallTooltip>
          <PendingInstallTooltip
            pending={viewLogPending}
            testId="catalog-card-action-view-log-pending-tooltip"
          >
            <button
              type="button"
              data-testid="catalog-card-action-view-log"
              aria-label={`View install log for ${entry.displayName}`}
              title={viewLogPending ? CATALOG_INSTALL_PENDING_TOOLTIP : undefined}
              disabled={viewLogPending}
              onClick={(e) => {
                stop(e)
                if (onViewLog) onViewLog(entry)
              }}
              className={[
                viewLogPress.className,
                "inline-flex items-center gap-1 rounded border border-[var(--border)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-60",
              ].join(" ")}
              {...viewLogPress.pressProps}
            >
              <ScrollText size={10} />
              log
            </button>
          </PendingInstallTooltip>
        </span>
      )
    default: {
      // TS exhaustiveness guard. If a future state is added to
      // `CatalogInstallState` and not handled here, this branch turns
      // into a compile-time error via `_exhaustive`.
      const _exhaustive: never = state
      return null as ReactNode & typeof _exhaustive
    }
  }
}

export default CatalogCard

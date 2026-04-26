"use client"

/**
 * BS.6.2 — Catalog card, 5 install-state visuals.
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
 * `onRetry` / `onViewLog` (BS.6.7 install button + BS.7 retry/log).
 * The footer always renders state-appropriate call-to-action **labels**
 * — buttons stay disabled until BS.6.7 lands so operators see the
 * shape of the surface but can't trigger actions before the install
 * pipeline is wired (avoids a "click that does nothing" UX hole).
 *
 * Out of scope for this row (deferred to later BS.6.x):
 *   • BS.6.3 — detail panel slide-out (this row only fires `onSelect`).
 *   • BS.6.6 — BS.3 8-layer motion (this row keeps motion to per-state
 *     CSS animations only: `.pulse-purple`, `.force-turbo-armed`,
 *     `.ring-spin`, `.force-turbo-hazard-overlay`. The card root is a
 *     `relative` block so BS.6.6 can wrap with the motion hooks
 *     without restructuring the DOM.)
 *   • BS.6.7 — wires real `onInstall` / disabled-state tooltip text.
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. All visuals derive from props +
 * immutable look-up tables (`STATE_PALETTE`, `STATE_ICON`, `STATE_LABEL`,
 * `STATE_PULSE`, `STATE_BORDER`, `STATE_FOOTER_LABEL`). Pure render
 * function, no `useState` / `useEffect` / `useRef` — this row stays
 * stateless on purpose so BS.6.6 can wrap motion hooks externally
 * without hook-order surprises. SSR-safe (deterministic, no
 * `Math.random` / `Date.now`). Browser-only render — uvicorn
 * `--workers N` model does not apply (answer #1: each browser tab
 * derives the same view from the same Next.js build artifact).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * N/A — pure presentation. Install progress flows through the `entry`
 * prop (or `installProgressPercent` override); BS.7's SSE wiring will
 * just re-render the card with a fresh percent. No API calls, no
 * cross-worker race at this layer.
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
  type CatalogDensity,
  type CatalogEntry,
  type CatalogFamily,
  type CatalogInstallState,
  coerceFamily,
} from "@/components/omnisight/catalog-tab"

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
      onInstall={onInstall}
      onRetry={onRetry}
      onViewLog={onViewLog}
    />
  )

  const interactive = Boolean(onSelect)
  const rootProps = interactive
    ? {
        role: "button" as const,
        tabIndex: 0,
        onClick: handleCardClick,
        onKeyDown: handleKeyDown,
      }
    : {}

  // The installing state wraps in a conic-gradient shell. Other states
  // skip the wrapper to avoid a redundant DOM node.
  if (state === "installing") {
    const gradientStyle: CSSProperties = {
      background: buildInstallProgressGradient(progress),
    }
    return (
      <div
        data-testid={`catalog-card-${entry.id}`}
        data-entry-id={entry.id}
        data-entry-family={family}
        data-state={state}
        data-progress={progress.toFixed(2)}
        aria-label={`${entry.displayName} — ${palette.statusText}`}
        className={[
          "group relative rounded-md p-[2px]",
          "transition-shadow duration-200",
          className ?? "",
        ]
          .filter(Boolean)
          .join(" ")}
        style={gradientStyle}
        {...rootProps}
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
  }

  return (
    <div
      data-testid={`catalog-card-${entry.id}`}
      data-entry-id={entry.id}
      data-entry-family={family}
      data-state={state}
      aria-label={`${entry.displayName} — ${palette.statusText}`}
      className={[
        "group relative flex h-full flex-col rounded-md border bg-[var(--card)]",
        "transition-colors duration-200",
        palette.borderClass,
        palette.rootAnimationClass,
        cardPaddingClass,
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
      {...rootProps}
    >
      {body}
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

  switch (state) {
    case "available":
      return (
        <button
          type="button"
          data-testid="catalog-card-action-install"
          aria-label={`Install ${entry.displayName}`}
          disabled={!onInstall}
          onClick={(e) => {
            stop(e)
            if (onInstall) onInstall(entry)
          }}
          className="inline-flex items-center gap-1 rounded border border-[var(--neural-blue)]/40 px-1.5 py-0.5 font-mono text-[10px] text-[var(--neural-blue)] hover:border-[var(--neural-blue)] hover:bg-[var(--neural-blue)]/10 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent"
        >
          <Download size={10} />
          {palette.footerLabel}
        </button>
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
        <button
          type="button"
          data-testid="catalog-card-action-update"
          aria-label={`Update ${entry.displayName}`}
          disabled={!onInstall}
          onClick={(e) => {
            stop(e)
            if (onInstall) onInstall(entry)
          }}
          className="inline-flex items-center gap-1 rounded border border-[var(--artifact-purple)]/55 px-1.5 py-0.5 font-mono text-[10px] text-[var(--artifact-purple)] hover:border-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)]/10 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent"
        >
          <ArrowUpCircle size={10} />
          {palette.footerLabel}
        </button>
      )
    case "failed":
      return (
        <span className="inline-flex items-center gap-1">
          <button
            type="button"
            data-testid="catalog-card-action-retry"
            aria-label={`Retry installing ${entry.displayName}`}
            disabled={!onRetry}
            onClick={(e) => {
              stop(e)
              if (onRetry) onRetry(entry)
            }}
            className="inline-flex items-center gap-1 rounded border border-[var(--critical-red)]/55 px-1.5 py-0.5 font-mono text-[10px] text-[var(--critical-red)] hover:border-[var(--critical-red)] hover:bg-[var(--critical-red)]/10 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-transparent"
          >
            <RefreshCw size={10} />
            {palette.footerLabel}
          </button>
          <button
            type="button"
            data-testid="catalog-card-action-view-log"
            aria-label={`View install log for ${entry.displayName}`}
            disabled={!onViewLog}
            onClick={(e) => {
              stop(e)
              if (onViewLog) onViewLog(entry)
            }}
            className="inline-flex items-center gap-1 rounded border border-[var(--border)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            <ScrollText size={10} />
            log
          </button>
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

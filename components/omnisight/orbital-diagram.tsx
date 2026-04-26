"use client"

/**
 * BS.5.3 — Platforms orbital diagram.
 *
 * Three concentric SVG rings (capacity 8 / 10 / 12) rotating at
 * different speeds — middle ring rotates in reverse so the eye does not
 * lock on a single sweep. Each dot is bound to one installed-platform
 * entry:
 *
 *   • status colour — emerald (healthy) / amber (installing) / rose (failed)
 *   • hover / focus → React-state-driven tooltip overlay (top of frame)
 *     plus a native `<title>` for browser-default tooltips.
 *   • click → navigate to `/settings/platforms?tab=catalog&entry=<id>`
 *     (overridable via `onEntryClick` for tests + alternate hosts).
 *
 * Empty slots render as faint placeholder dots so the orbital still has
 * visual structure when the operator has installed nothing yet. Entries
 * that overflow the 30-slot capacity surface a "+N more" badge in the
 * bottom-right so no data is silently dropped.
 *
 * Distribution rule: entries are sorted by status priority (failed →
 * installing → healthy) then by name, and the *outer* ring fills first
 * so the most attention-worthy statuses sit on the visually prominent
 * ring.
 *
 * Module-global state audit
 * ─────────────────────────
 * No module-level mutable state. Geometry consts (`ORBITAL_RING_SPECS`,
 * `STATUS_*` look-up tables) are immutable; hover state is per-mount
 * React state. SSR-safe — `distributeEntries()` is deterministic (no
 * `Math.random`, no `Date.now`).
 *
 * Read-after-write timing audit
 * ─────────────────────────────
 * N/A — pure presentation component. The `entries` prop is owned by the
 * caller (BS.6 catalog hook lands later); any timing concerns belong
 * upstream of this layer.
 */

import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useId,
  useMemo,
  useState,
} from "react"
import { useRouter } from "next/navigation"

// ─────────────────────────────────────────────────────────────────────
// Public types — exported so BS.5.5 tests + BS.6 catalog hook share a
// single source of truth.
// ─────────────────────────────────────────────────────────────────────

export type PlatformEntryStatus = "healthy" | "installing" | "failed"

export interface InstalledPlatformEntry {
  /** Stable unique id (used for React key + URL deep-link). */
  id: string
  /** Operator-facing display name. */
  name: string
  /** Lifecycle bucket — drives dot colour + animation. */
  status: PlatformEntryStatus
  /** Catalog category (vertical / sdk / runtime / bsp / …). Optional
   *  because BS.6 may extend this taxonomy later. */
  kind?: "vertical" | "sdk" | "runtime" | "bsp" | string
  /** Optional version tag, surfaced in the tooltip. */
  version?: string
}

export interface OrbitalDiagramProps {
  entries?: ReadonlyArray<InstalledPlatformEntry>
  /** Override the click handler. Falls back to a `router.push` to the
   *  catalog deep-link when omitted. Tests typically pass a `vi.fn()` so
   *  they don't have to mount inside a Next.js router context. */
  onEntryClick?: (entry: InstalledPlatformEntry) => void
  /** Override the URL the default click handler navigates to. */
  catalogHref?: (entry: InstalledPlatformEntry) => string
  /** Test-id prefix — `<PlatformHero />` passes "platform-hero-orbital"
   *  to keep BS.5.2's testid contract stable after the swap. */
  testIdPrefix?: string
  className?: string
  ariaLabel?: string
}

// ─────────────────────────────────────────────────────────────────────
// Geometry — three rings, capacity 8 / 10 / 12 (within the BS.5 spec
// "8-12 dots / 圓"). Inner spins fastest, middle is reversed for the
// counter-rotation effect, outer is slow so the larger ring stays
// readable. Durations match `<HeroOrbitalShell />` (BS.5.2) so swapping
// in feels visually continuous.
// ─────────────────────────────────────────────────────────────────────

interface RingSpec {
  index: 0 | 1 | 2
  radius: number
  capacity: number
  durationS: number
  reverse: boolean
}

export const ORBITAL_RING_SPECS: ReadonlyArray<RingSpec> = [
  { index: 0, radius: 30, capacity: 8, durationS: 18, reverse: false },
  { index: 1, radius: 55, capacity: 10, durationS: 32, reverse: true },
  { index: 2, radius: 85, capacity: 12, durationS: 48, reverse: false },
] as const

export const ORBITAL_TOTAL_CAPACITY: number = ORBITAL_RING_SPECS.reduce(
  (sum, ring) => sum + ring.capacity,
  0,
)

const STATUS_PRIORITY: Record<PlatformEntryStatus, number> = {
  failed: 0,
  installing: 1,
  healthy: 2,
}

const STATUS_FILL: Record<PlatformEntryStatus, string> = {
  healthy: "#34d399", // emerald-400
  installing: "#fbbf24", // amber-400 (FUI "orange" lane)
  failed: "#f43f5e", // rose-500
}

const STATUS_GLOW: Record<PlatformEntryStatus, string> = {
  healthy: "drop-shadow(0 0 3px rgba(52,211,153,0.7))",
  installing: "drop-shadow(0 0 4px rgba(251,191,36,0.85))",
  failed: "drop-shadow(0 0 5px rgba(244,63,94,0.95))",
}

/** Tailwind `animate-pulse` runs at the default 2s; arbitrary-duration
 *  variants give failed dots a more urgent cadence than installing dots
 *  so the operator can triage by motion as well as colour. */
const STATUS_PULSE: Record<PlatformEntryStatus, string> = {
  healthy: "",
  installing: "animate-pulse [animation-duration:1.6s]",
  failed: "animate-pulse [animation-duration:0.9s]",
}

const STATUS_LABEL: Record<PlatformEntryStatus, string> = {
  healthy: "Healthy",
  installing: "Installing",
  failed: "Failed",
}

const STATUS_BADGE_CLASS: Record<PlatformEntryStatus, string> = {
  healthy: "border-emerald-500/60 text-emerald-300",
  installing: "border-amber-500/60 text-amber-300",
  failed: "border-rose-500/60 text-rose-300",
}

const PLACEHOLDER_FILL = "rgba(56,189,248,0.18)" // faint neural-blue

// ─────────────────────────────────────────────────────────────────────
// Distribution.
// ─────────────────────────────────────────────────────────────────────

interface SlotShape {
  ringIndex: number
  slotIndex: number
  cx: number
  cy: number
  entry: InstalledPlatformEntry | null
}

export interface DistributionResult {
  /** Indexed by `ringIndex` so callers / tests can reach a specific
   *  ring's slot list without re-grouping. */
  ringSlots: ReadonlyArray<ReadonlyArray<SlotShape>>
  /** Number of entries that exceeded the 30-slot capacity. */
  overflow: number
  /** Number of entries that landed on a slot. */
  placedCount: number
}

export function distributeEntries(
  entries: ReadonlyArray<InstalledPlatformEntry>,
): DistributionResult {
  const sorted = [...entries].sort((a, b) => {
    const ap = STATUS_PRIORITY[a.status] ?? 9
    const bp = STATUS_PRIORITY[b.status] ?? 9
    if (ap !== bp) return ap - bp
    return a.name.localeCompare(b.name)
  })

  const ringSlots: SlotShape[][] = ORBITAL_RING_SPECS.map(() => [])
  let cursor = 0
  // Outer ring fills first so failed/installing entries sit prominently.
  for (const ring of [...ORBITAL_RING_SPECS].sort((a, b) => b.radius - a.radius)) {
    for (let i = 0; i < ring.capacity; i += 1) {
      // Start at the top (-90°) so slot 0 sits at 12 o'clock.
      const angle = (i / ring.capacity) * Math.PI * 2 - Math.PI / 2
      const cx = Math.cos(angle) * ring.radius
      const cy = Math.sin(angle) * ring.radius
      const entry = cursor < sorted.length ? sorted[cursor] : null
      ringSlots[ring.index].push({
        ringIndex: ring.index,
        slotIndex: i,
        cx,
        cy,
        entry,
      })
      if (entry) cursor += 1
    }
  }

  return {
    ringSlots,
    overflow: Math.max(0, sorted.length - cursor),
    placedCount: cursor,
  }
}

/** Default catalog deep-link — kept exported so tests can lock the URL
 *  shape without touching JSX, and so future BS.6 surfaces (e.g.
 *  notification cards) reuse the same scheme. */
export function defaultCatalogHref(entry: InstalledPlatformEntry): string {
  return `/settings/platforms?tab=catalog&entry=${encodeURIComponent(entry.id)}`
}

// ─────────────────────────────────────────────────────────────────────
// OrbitalDiagram — main export.
// ─────────────────────────────────────────────────────────────────────

export function OrbitalDiagram({
  entries = [],
  onEntryClick,
  catalogHref = defaultCatalogHref,
  testIdPrefix = "orbital-diagram",
  className,
  ariaLabel = "Installed platforms orbital diagram",
}: OrbitalDiagramProps) {
  const router = useRouter()
  const tooltipDomId = useId()
  const [hoveredId, setHoveredId] = useState<string | null>(null)

  const distribution = useMemo(() => distributeEntries(entries), [entries])

  const hoveredSlot = useMemo(() => {
    if (!hoveredId) return null
    for (const ring of distribution.ringSlots) {
      for (const slot of ring) {
        if (slot.entry?.id === hoveredId) return slot
      }
    }
    return null
  }, [distribution, hoveredId])

  const handleActivate = useCallback(
    (entry: InstalledPlatformEntry) => {
      if (onEntryClick) {
        onEntryClick(entry)
        return
      }
      // Defensive guard: tests that don't mock `next/navigation` still
      // get a no-op activation rather than a hard crash.
      if (router && typeof router.push === "function") {
        router.push(catalogHref(entry))
      }
    },
    [catalogHref, onEntryClick, router],
  )

  return (
    <div
      data-testid={testIdPrefix}
      data-orbital-entries={distribution.placedCount}
      data-orbital-overflow={distribution.overflow}
      data-orbital-hovered={hoveredId ?? ""}
      className={["relative h-full w-full", className ?? ""]
        .filter(Boolean)
        .join(" ")}
    >
      <svg
        viewBox="-100 -100 200 200"
        width="100%"
        height="100%"
        role="img"
        aria-label={ariaLabel}
        data-testid={`${testIdPrefix}-svg`}
        className="select-none"
      >
        {/* Cross-hair axes — match `<HeroOrbitalShell />` aesthetics. */}
        <line
          x1="-95"
          x2="95"
          y1="0"
          y2="0"
          stroke="rgba(56,189,248,0.12)"
          strokeWidth="0.5"
        />
        <line
          x1="0"
          x2="0"
          y1="-95"
          y2="95"
          stroke="rgba(56,189,248,0.12)"
          strokeWidth="0.5"
        />

        {/* Centre core — pulses via shared breathing-pulse keyframe. */}
        <circle
          cx="0"
          cy="0"
          r="6"
          fill="var(--neural-blue)"
          opacity="0.85"
          className="breathing-pulse"
          data-testid={`${testIdPrefix}-core`}
        />

        {ORBITAL_RING_SPECS.map((ring) => {
          const slots = distribution.ringSlots[ring.index]
          const filled = slots.filter((s) => s.entry).length
          return (
            <g
              key={ring.radius}
              className="orbital-rotate"
              data-testid={`${testIdPrefix}-ring-${ring.index}`}
              data-ring-index={ring.index}
              data-ring-radius={ring.radius}
              data-ring-capacity={ring.capacity}
              data-ring-filled={filled}
              style={{
                animationDuration: `${ring.durationS}s`,
                animationDirection: ring.reverse ? "reverse" : "normal",
                // Pause rotation while a dot is hovered so the operator
                // can read the tooltip + click without chasing a moving
                // target.
                animationPlayState: hoveredId ? "paused" : "running",
                transformOrigin: "center",
              }}
            >
              {/* Ring track — thin dashed stroke. */}
              <circle
                cx="0"
                cy="0"
                r={ring.radius}
                fill="none"
                stroke="rgba(56,189,248,0.25)"
                strokeWidth="0.6"
                strokeDasharray="2 4"
              />
              {slots.map((slot) =>
                slot.entry ? (
                  <OrbitalEntryDot
                    key={`${ring.index}-${slot.slotIndex}-${slot.entry.id}`}
                    slot={slot}
                    entry={slot.entry}
                    testIdPrefix={testIdPrefix}
                    tooltipId={
                      hoveredId === slot.entry.id ? tooltipDomId : undefined
                    }
                    onHoverChange={setHoveredId}
                    onActivate={handleActivate}
                  />
                ) : (
                  <OrbitalEmptyDot
                    key={`${ring.index}-${slot.slotIndex}-empty`}
                    slot={slot}
                    testIdPrefix={testIdPrefix}
                  />
                ),
              )}
            </g>
          )
        })}
      </svg>

      {hoveredSlot?.entry && (
        <OrbitalTooltip
          id={tooltipDomId}
          entry={hoveredSlot.entry}
          testIdPrefix={testIdPrefix}
        />
      )}

      {distribution.overflow > 0 && (
        <div
          data-testid={`${testIdPrefix}-overflow-badge`}
          className="absolute bottom-1 right-1 rounded-full border border-[var(--neural-blue)]/60 bg-black/60 px-2 py-0.5 font-mono text-[9px] uppercase tracking-wider text-[var(--neural-blue)]"
        >
          +{distribution.overflow} more
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────
// Sub-components.
// ─────────────────────────────────────────────────────────────────────

interface OrbitalEntryDotProps {
  slot: SlotShape
  entry: InstalledPlatformEntry
  testIdPrefix: string
  tooltipId: string | undefined
  onHoverChange: (id: string | null) => void
  onActivate: (entry: InstalledPlatformEntry) => void
}

function OrbitalEntryDot({
  slot,
  entry,
  testIdPrefix,
  tooltipId,
  onHoverChange,
  onActivate,
}: OrbitalEntryDotProps) {
  const fill = STATUS_FILL[entry.status]
  const glow = STATUS_GLOW[entry.status]
  const pulseClass = STATUS_PULSE[entry.status]

  const handleEnter = useCallback(
    () => onHoverChange(entry.id),
    [entry.id, onHoverChange],
  )
  const handleLeave = useCallback(
    () => onHoverChange(null),
    [onHoverChange],
  )
  const handleClick = useCallback(
    () => onActivate(entry),
    [entry, onActivate],
  )
  const handleKey = useCallback(
    (e: ReactKeyboardEvent<SVGGElement>) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault()
        onActivate(entry)
      }
    },
    [entry, onActivate],
  )

  const label = `${entry.name} (${STATUS_LABEL[entry.status]})`

  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={label}
      aria-describedby={tooltipId}
      data-testid={`${testIdPrefix}-dot-${entry.id}`}
      data-entry-id={entry.id}
      data-entry-status={entry.status}
      data-entry-kind={entry.kind ?? "unknown"}
      data-ring-index={slot.ringIndex}
      data-slot-index={slot.slotIndex}
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
      onFocus={handleEnter}
      onBlur={handleLeave}
      onClick={handleClick}
      onKeyDown={handleKey}
      style={{ cursor: "pointer", outline: "none" }}
    >
      {/* Native browser tooltip fallback (instant, no JS state). */}
      <title>{`${entry.name} — ${STATUS_LABEL[entry.status]}`}</title>
      {/* Invisible hit area — keeps the visible dot small while making
       *  click/hover targets large enough to satisfy WCAG 2.5.5. */}
      <circle cx={slot.cx} cy={slot.cy} r="8" fill="transparent" />
      <circle
        cx={slot.cx}
        cy={slot.cy}
        r="3.4"
        fill={fill}
        className={pulseClass || undefined}
        style={{ filter: glow, pointerEvents: "none" }}
      />
    </g>
  )
}

interface OrbitalEmptyDotProps {
  slot: SlotShape
  testIdPrefix: string
}

function OrbitalEmptyDot({ slot, testIdPrefix }: OrbitalEmptyDotProps) {
  return (
    <circle
      cx={slot.cx}
      cy={slot.cy}
      r="1.6"
      fill={PLACEHOLDER_FILL}
      data-testid={`${testIdPrefix}-slot-empty-${slot.ringIndex}-${slot.slotIndex}`}
      data-slot-state="empty"
      data-ring-index={slot.ringIndex}
      data-slot-index={slot.slotIndex}
    />
  )
}

interface OrbitalTooltipProps {
  id: string
  entry: InstalledPlatformEntry
  testIdPrefix: string
}

/** Tooltip is anchored to the top of the orbital frame instead of the
 *  rotating dot itself — chasing a moving cx/cy in pixel space requires
 *  reading the SVG CTM on every animation frame, which is overkill for
 *  a panel that needs to stay readable for 1-2 seconds. The hover
 *  handler pauses the orbit (see `animationPlayState`), and a fixed
 *  tooltip slot keeps positioning predictable for tests. */
function OrbitalTooltip({ id, entry, testIdPrefix }: OrbitalTooltipProps) {
  const style: CSSProperties = {
    left: "50%",
    top: 4,
    transform: "translateX(-50%)",
  }
  return (
    <div
      id={id}
      role="tooltip"
      data-testid={`${testIdPrefix}-tooltip`}
      data-tooltip-entry-id={entry.id}
      style={style}
      className="pointer-events-none absolute z-10 min-w-[140px] max-w-[200px] rounded-md border border-[var(--border)] bg-black/85 p-2 font-mono text-[10px] text-[var(--foreground)] shadow-[0_0_12px_rgba(0,0,0,0.6)] backdrop-blur"
    >
      <div
        data-testid={`${testIdPrefix}-tooltip-name`}
        className="truncate font-orbitron text-[11px] tracking-wider text-[var(--foreground)]"
      >
        {entry.name}
      </div>
      <div className="mt-1 flex items-center gap-1">
        <span
          data-testid={`${testIdPrefix}-tooltip-status`}
          className={[
            "inline-block rounded-full border px-1.5 py-0.5 text-[9px] uppercase tracking-wider",
            STATUS_BADGE_CLASS[entry.status],
          ].join(" ")}
        >
          {STATUS_LABEL[entry.status]}
        </span>
        {entry.kind && (
          <span
            data-testid={`${testIdPrefix}-tooltip-kind`}
            className="text-[9px] uppercase tracking-wider text-[var(--muted-foreground)]"
          >
            {entry.kind}
          </span>
        )}
      </div>
      {entry.version && (
        <div
          data-testid={`${testIdPrefix}-tooltip-version`}
          className="mt-1 text-[9px] text-[var(--muted-foreground)]"
        >
          v{entry.version}
        </div>
      )}
    </div>
  )
}

export default OrbitalDiagram

/**
 * V6 #4 (TODO row 1551 / issue #322) — Multi-device grid view.
 *
 * Fans a single screenshot (or a per-device mapping of screenshots) out
 * across the six V6 #3 `DeviceFrame` presets so the operator can see,
 * at a glance, how the same mobile page renders on every target device:
 *
 *   iPhone 15  |  iPhone SE  |  iPad
 *   Pixel 8    |  Galaxy Fold |  Galaxy Tab
 *
 * This is the surface that the Mobile workspace (V7) and the agent
 * visual-context injector (V6 #5) read from. Both the DOM walker that
 * snaps frames into the multimodal LLM context, and the V7 annotator
 * that overlays bounding boxes, key off the `data-device` attribute
 * the child frames expose.
 *
 * Scope contract
 * ──────────────
 * - Zero extra network requests: every bezel is the child `DeviceFrame`
 *   rendering pure CSS, no PNG bezel sprites.
 * - Deterministic in jsdom — layout is CSS grid, tests don't need a
 *   real layout engine to assert "six cells rendered in this order".
 * - Ships with a sane default set (`DEFAULT_DEVICE_GRID_ITEMS`) so
 *   callers that just want "same page, six devices" pass a single
 *   `screenshotUrl` and get a grid back.
 * - Per-device overrides (screenshot url / loading / empty / alt) let
 *   callers mix states: V6 #5 will mark three frames as `loading`
 *   while the agent is still re-capturing after a code edit, and the
 *   other three stay visible with the previous frame.
 *
 * The grid is the *view*; state (which device is selected, which are
 * loading) lives in the caller. This keeps the component compatible
 * with both a simple static operator dashboard and a fully-interactive
 * visual-diff workspace.
 */
"use client"

import * as React from "react"
import { cn } from "@/lib/utils"
import {
  DeviceFrame,
  DEVICE_PROFILE_IDS,
  DEVICE_PROFILES,
  getDeviceProfile,
  type DeviceForm,
  type DevicePlatform,
  type DeviceProfile,
  type DeviceProfileId,
} from "@/components/omnisight/device-frame"

// ─── Public shapes ─────────────────────────────────────────────────────────

/**
 * One cell in the grid. Only `device` is required — everything else
 * either inherits from the grid-level defaults (`screenshotUrl`,
 * `loading`, `empty`) or falls back to the `DeviceFrame` default.
 */
export interface DeviceGridItem {
  /** Which preset fills this cell. */
  device: DeviceProfileId
  /** Per-cell screenshot override — beats grid-level `screenshotUrl`. */
  screenshotUrl?: string | null
  /** Per-cell loading override — beats grid-level `loading`. */
  loading?: boolean
  /** Per-cell empty override — beats grid-level `empty`. */
  empty?: boolean
  /** Per-cell alt override — beats frame default (`"<Label> screenshot"`). */
  alt?: string
}

export interface DeviceGridProps {
  /**
   * Explicit list of devices. If omitted, the grid renders all six
   * presets in `DEVICE_PROFILE_IDS` order.
   */
  devices?: readonly (DeviceProfileId | DeviceGridItem)[]
  /** Grid-wide screenshot URL — used when an item has no override. */
  screenshotUrl?: string | null
  /** Grid-wide loading flag — used when an item has no override. */
  loading?: boolean
  /** Grid-wide empty flag — used when an item has no override. */
  empty?: boolean
  /** Width of each child `DeviceFrame` in CSS px. Default 240. */
  frameWidth?: number
  /** Min column width for auto-fit layout. Default `frameWidth + 24`. */
  minColumnWidth?: number
  /** Explicit column count. When set, overrides auto-fit layout. */
  columns?: number
  /** CSS gap between cells in CSS px. Default 20. */
  gap?: number
  /** Show the device label under each frame. Default true. */
  showLabels?: boolean
  /** Filter to a subset of platforms (`ios` / `android`). */
  platforms?: readonly DevicePlatform[]
  /** Filter to a subset of forms (`phone` / `tablet` / `foldable`). */
  forms?: readonly DeviceForm[]
  /** Currently-selected device — renders a highlighted ring. */
  selectedDevice?: DeviceProfileId | null
  /** Invoked when a device frame is clicked or Enter/Space activated. */
  onSelectDevice?: (device: DeviceProfileId) => void
  /** Placeholder text when the grid resolves to zero devices. */
  emptyLabel?: string
  /** Optional section title. Renders inside `<header>` when provided. */
  title?: string
  /** Optional section description (below title). */
  description?: string
  /** Extra classes on the outer `<section>`. */
  className?: string
  /** `data-testid` passthrough — each frame gets `<testid>-frame-<device>`. */
  "data-testid"?: string
  /** ARIA label for the grid region. Defaults to "Multi-device preview". */
  "aria-label"?: string
}

// ─── Defaults & helpers ────────────────────────────────────────────────────

const DEFAULT_FRAME_WIDTH = 240
const DEFAULT_GAP = 20
const DEFAULT_COLUMN_PAD = 24
const DEFAULT_EMPTY_LABEL = "No device previews to display"
const DEFAULT_ARIA_LABEL = "Multi-device preview"

/**
 * The canonical "same page on six devices" default. Callers that pass
 * no `devices` prop get this list, which mirrors
 * `DEVICE_PROFILE_IDS` (iPhone 15 / SE / iPad / Pixel 8 / Fold / Tab).
 */
export const DEFAULT_DEVICE_GRID_ITEMS: readonly DeviceGridItem[] = Object.freeze(
  DEVICE_PROFILE_IDS.map((device) => Object.freeze({ device } as DeviceGridItem)),
)

/**
 * Normalise a mixed list of `DeviceProfileId` strings and
 * `DeviceGridItem` objects into a clean, deduplicated `DeviceGridItem[]`.
 *
 * Rules:
 *   - Unknown device ids are dropped silently (defensive — agent-
 *     supplied data might include a future preset we don't yet render).
 *   - Duplicates collapse to the first occurrence (stable order wins
 *     over per-cell overrides on later entries).
 *   - Never throws — upstream agent output must not crash the view.
 */
export function normaliseDeviceGridItems(
  input: readonly (DeviceProfileId | DeviceGridItem)[] | undefined,
): DeviceGridItem[] {
  if (!input || input.length === 0) return []
  const seen = new Set<DeviceProfileId>()
  const out: DeviceGridItem[] = []
  for (const entry of input) {
    if (!entry) continue
    const item: DeviceGridItem =
      typeof entry === "string" ? { device: entry } : entry
    if (!item.device) continue
    if (!(item.device in DEVICE_PROFILES)) continue
    if (seen.has(item.device)) continue
    seen.add(item.device)
    out.push(item)
  }
  return out
}

/**
 * Apply platform/form filters after normalisation. Kept separate so
 * callers (and tests) can reason about filter logic in isolation.
 */
export function filterDeviceGridItems(
  items: readonly DeviceGridItem[],
  opts: {
    platforms?: readonly DevicePlatform[]
    forms?: readonly DeviceForm[]
  },
): DeviceGridItem[] {
  const { platforms, forms } = opts
  if (!platforms && !forms) return [...items]
  return items.filter((it) => {
    const profile = DEVICE_PROFILES[it.device]
    if (platforms && platforms.length > 0 && !platforms.includes(profile.platform)) {
      return false
    }
    if (forms && forms.length > 0 && !forms.includes(profile.form)) {
      return false
    }
    return true
  })
}

/**
 * Build the CSS grid template. Pure — callers can precompute layout
 * without mounting the component (useful for server-rendered shells).
 *
 * - `columns` > 0  → `repeat(columns, minmax(0, 1fr))`, uniform cells.
 * - otherwise      → `repeat(auto-fit, minmax(<minColumnWidth>px, 1fr))`,
 *                    the layout scales with container width and stays
 *                    sane from a phone (1 col) to a 5k display (6 cols).
 */
export function buildDeviceGridStyle(opts: {
  columns?: number
  minColumnWidth?: number
  gap?: number
}): React.CSSProperties {
  const gap = opts.gap ?? DEFAULT_GAP
  const columns = opts.columns
  if (columns && Number.isFinite(columns) && columns > 0) {
    return {
      display: "grid",
      gridTemplateColumns: `repeat(${Math.floor(columns)}, minmax(0, 1fr))`,
      gap,
    }
  }
  const minColumnWidth = opts.minColumnWidth ?? DEFAULT_FRAME_WIDTH + DEFAULT_COLUMN_PAD
  return {
    display: "grid",
    gridTemplateColumns: `repeat(auto-fit, minmax(${Math.max(
      48,
      minColumnWidth,
    )}px, 1fr))`,
    gap,
  }
}

/**
 * Deterministic next-selection index for arrow-key navigation.
 * Exposed so the keyboard-nav contract is testable without fireEvent.
 */
export function nextSelectionIndex(
  current: number,
  total: number,
  key: "ArrowLeft" | "ArrowRight" | "ArrowUp" | "ArrowDown" | "Home" | "End",
  columns: number,
): number {
  if (total <= 0) return -1
  const safeCols = Math.max(1, Math.floor(columns))
  const base = current < 0 ? 0 : current
  switch (key) {
    case "ArrowLeft":
      return (base - 1 + total) % total
    case "ArrowRight":
      return (base + 1) % total
    case "ArrowUp": {
      const next = base - safeCols
      return next < 0 ? base : next
    }
    case "ArrowDown": {
      const next = base + safeCols
      return next >= total ? base : next
    }
    case "Home":
      return 0
    case "End":
      return total - 1
  }
}

function resolveItem(
  item: DeviceGridItem,
  gridDefaults: {
    screenshotUrl?: string | null
    loading?: boolean
    empty?: boolean
  },
): Required<Pick<DeviceGridItem, "device">> & {
  screenshotUrl: string | null
  loading: boolean
  empty: boolean
  alt?: string
  profile: DeviceProfile
} {
  const profile = getDeviceProfile(item.device)
  return {
    device: item.device,
    screenshotUrl:
      item.screenshotUrl !== undefined
        ? item.screenshotUrl ?? null
        : gridDefaults.screenshotUrl ?? null,
    loading: item.loading !== undefined ? item.loading : !!gridDefaults.loading,
    empty: item.empty !== undefined ? item.empty : !!gridDefaults.empty,
    alt: item.alt,
    profile,
  }
}

// ─── Component ─────────────────────────────────────────────────────────────

const ARROW_KEYS = new Set([
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "ArrowDown",
  "Home",
  "End",
])

export function DeviceGrid({
  devices,
  screenshotUrl,
  loading,
  empty,
  frameWidth = DEFAULT_FRAME_WIDTH,
  minColumnWidth,
  columns,
  gap = DEFAULT_GAP,
  showLabels = true,
  platforms,
  forms,
  selectedDevice = null,
  onSelectDevice,
  emptyLabel = DEFAULT_EMPTY_LABEL,
  title,
  description,
  className,
  "data-testid": testId,
  "aria-label": ariaLabel = DEFAULT_ARIA_LABEL,
}: DeviceGridProps) {
  const resolvedItems = React.useMemo(() => {
    const source = devices ?? DEFAULT_DEVICE_GRID_ITEMS
    return filterDeviceGridItems(normaliseDeviceGridItems(source), {
      platforms,
      forms,
    })
  }, [devices, platforms, forms])

  const gridStyle = React.useMemo(
    () =>
      buildDeviceGridStyle({
        columns,
        minColumnWidth: minColumnWidth ?? frameWidth + DEFAULT_COLUMN_PAD,
        gap,
      }),
    [columns, minColumnWidth, frameWidth, gap],
  )

  // The "effective" column count drives keyboard navigation so Up/Down
  // jumps rows rather than bouncing to wherever the browser wrapped.
  // When the caller pinned `columns`, we trust that; otherwise fall back
  // to the number of cells (arrow Up/Down becomes a no-op, which is
  // correct for a single-row layout on narrow screens in tests).
  const effectiveColumns =
    columns && columns > 0 ? Math.floor(columns) : resolvedItems.length

  const selectedIndex = React.useMemo(() => {
    if (!selectedDevice) return -1
    return resolvedItems.findIndex((it) => it.device === selectedDevice)
  }, [resolvedItems, selectedDevice])

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (!onSelectDevice) return
      if (!ARROW_KEYS.has(event.key)) return
      if (resolvedItems.length === 0) return
      event.preventDefault()
      const nextIdx = nextSelectionIndex(
        selectedIndex,
        resolvedItems.length,
        event.key as
          | "ArrowLeft"
          | "ArrowRight"
          | "ArrowUp"
          | "ArrowDown"
          | "Home"
          | "End",
        effectiveColumns,
      )
      const nextItem = resolvedItems[nextIdx]
      if (nextItem) onSelectDevice(nextItem.device)
    },
    [onSelectDevice, resolvedItems, selectedIndex, effectiveColumns],
  )

  const showEmptyState = resolvedItems.length === 0

  return (
    <section
      className={cn(
        "omnisight-device-grid relative flex flex-col gap-3",
        className,
      )}
      data-testid={testId}
      data-device-count={resolvedItems.length}
      aria-label={ariaLabel}
    >
      {(title || description) && (
        <header
          className="omnisight-device-grid__header flex flex-col gap-1"
          data-testid={testId ? `${testId}-header` : undefined}
        >
          {title && (
            <h3 className="omnisight-device-grid__title text-sm font-medium tracking-wide">
              {title}
            </h3>
          )}
          {description && (
            <p className="omnisight-device-grid__description text-xs text-[var(--muted-foreground,#94a3b8)]">
              {description}
            </p>
          )}
        </header>
      )}

      {showEmptyState ? (
        <div
          role="status"
          className="omnisight-device-grid__empty flex items-center justify-center rounded-md border border-dashed border-[var(--border,#2a2f36)] p-6 text-xs text-[var(--muted-foreground,#94a3b8)]"
          data-testid={testId ? `${testId}-empty` : undefined}
        >
          {emptyLabel}
        </div>
      ) : (
        // FX.7.12: when a selection callback is wired, this grid acts as
        // a single-select listbox (each cell is a selectable option, so
        // `aria-selected` is valid on the cell). When no selection
        // callback is provided, fall back to the plain semantic `list`
        // — `listitem` does not support `aria-selected`, so the cell's
        // aria-selected attribute is also gated on `onSelectDevice`.
        <div
          className="omnisight-device-grid__grid"
          role={onSelectDevice ? "listbox" : "list"}
          style={gridStyle}
          onKeyDown={handleKeyDown}
          data-testid={testId ? `${testId}-grid` : undefined}
        >
          {resolvedItems.map((raw) => {
            const item = resolveItem(raw, { screenshotUrl, loading, empty })
            const isSelected = selectedDevice === item.device
            const cellTestId = testId
              ? `${testId}-cell-${item.device}`
              : undefined
            const frameTestId = testId
              ? `${testId}-frame-${item.device}`
              : undefined
            return (
              <div
                key={item.device}
                role={onSelectDevice ? "option" : "listitem"}
                className={cn(
                  "omnisight-device-grid__cell relative flex flex-col items-center justify-start rounded-md p-2 transition-colors",
                  isSelected &&
                    "bg-[var(--accent,rgba(56,189,248,0.08))] ring-2 ring-[var(--ring,#38bdf8)]",
                )}
                data-testid={cellTestId}
                data-device={item.device}
                data-platform={item.profile.platform}
                data-form={item.profile.form}
                aria-selected={onSelectDevice ? isSelected : undefined}
              >
                <DeviceFrame
                  device={item.device}
                  screenshotUrl={item.screenshotUrl}
                  loading={item.loading}
                  empty={item.empty}
                  alt={item.alt}
                  width={frameWidth}
                  showLabel={showLabels}
                  onClick={
                    onSelectDevice
                      ? () => onSelectDevice(item.device)
                      : undefined
                  }
                  data-testid={frameTestId}
                />
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

export default DeviceGrid

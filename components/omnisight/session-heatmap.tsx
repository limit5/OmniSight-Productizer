"use client"

// ZZ.C2 (#305-2, 2026-04-24) checkbox 2 — SessionHeatmap.
//
// Calendar-style heatmap: y-axis is calendar date, x-axis is hour-of-day
// (0–23), cell shade scales with ``token_total``. Consumes the sparse
// ``[{day, hour, token_total, cost_total}]`` payload shipped by checkbox
// 1 (``GET /runtime/tokens/heatmap?window=7d|30d`` in
// ``backend/routers/system.py::get_token_heatmap``) and paints it into
// a fixed-size grid (7 × 24 or 30 × 24).
//
// Design decisions (the "why"):
//
//   * **UTC → local timezone shift**: backend cell keys are authoritative
//     UTC so two replicas in different regions produce identical
//     response payloads. The heatmap UI then shifts each cell into the
//     browser's local timezone so operators see their own day/hour
//     labels — matches checkbox 5 of the TODO spec ("operator 看到的是
//     local time 不是 UTC"). One UTC ``(day, hour)`` may map onto a
//     different local ``(day, hour)`` depending on offset; cells that
//     fall on the same local slot after shift are summed.
//
//   * **Log colour scale**: per spec ("log scale 避免極端值洗掉細節").
//     A single 10M-token pathological turn would otherwise flatten every
//     other cell to near-invisible on a linear scale. ``log(tokens + 1)
//     / log(max + 1)`` keeps the normal-range turns readable.
//
//   * **Sparse payload padding**: backend omits zero-activity cells; we
//     synthesise the full day axis (anchored on local "today" minus N-1
//     days) so the grid always draws 7 / 30 rows even on a brand-new
//     tenant. Missing cells render as the empty-cell muted background,
//     visually distinct from a low-activity cell.
//
//   * **Hover tooltip**: displays day + hour range + tokens + cost per
//     spec. Uses React state + absolute positioning so the tooltip shows
//     rich formatting (cost in the brand hardware-orange, etc.). A
//     native ``title`` attribute on each cell is the accessibility /
//     keyboard-navigation fallback.

import { useState, useEffect, useMemo, useCallback } from "react"
import { BarChart3, RefreshCw, AlertCircle } from "lucide-react"
import {
  fetchTokenHeatmap,
  type TokenHeatmapCell,
  type TokenHeatmapResponse,
  type TokenHeatmapWindow,
} from "@/lib/api"

/** Exported so integrators (and tests) can enumerate the operator-
 *  facing windows without having to re-derive the backend whitelist. */
export const SESSION_HEATMAP_WINDOWS: TokenHeatmapWindow[] = ["7d", "30d"]

/**
 * Map a token count onto [0, 1] with a log curve. Returns 0 on
 * non-positive / non-finite input. ``maxTokens`` should be the grid's
 * current maximum so the hottest cell anchors at 1.0 and every other
 * cell scales against it.
 */
export function logScaleIntensity(tokens: number, maxTokens: number): number {
  if (!Number.isFinite(tokens) || tokens <= 0) return 0
  if (!Number.isFinite(maxTokens) || maxTokens <= 0) return 0
  const ratio = Math.log(tokens + 1) / Math.log(maxTokens + 1)
  if (!Number.isFinite(ratio)) return 0
  return Math.max(0, Math.min(1, ratio))
}

/** ``YYYY-MM-DD`` key for a Date in the browser's local timezone. */
export function localDayKey(date: Date): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, "0")
  const d = String(date.getDate()).padStart(2, "0")
  return `${y}-${m}-${d}`
}

/**
 * Shift a UTC ``(day, hour)`` tuple into the operator's local timezone.
 * ``day`` is a ``YYYY-MM-DD`` string (the backend emits it in UTC);
 * ``hour`` is 0..23 UTC. Returns ``null`` on malformed input so callers
 * can skip instead of crashing on a legacy row.
 */
export function shiftCellToLocal(
  day: string,
  hour: number,
): { date: Date; dayKey: string; hour: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day)
  if (!m) return null
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return null
  const utcEpoch = Date.UTC(
    Number(m[1]),
    Number(m[2]) - 1,
    Number(m[3]),
    hour,
  )
  const local = new Date(utcEpoch)
  return {
    date: local,
    dayKey: localDayKey(local),
    hour: local.getHours(),
  }
}

interface LocalBucket {
  tokens: number
  cost: number
}

/**
 * Fold the sparse UTC cells into a local-timezone grid keyed by
 * ``<localDay>:<localHour>``. Cells that collide after the shift
 * (e.g. DST transition days) are summed. Also returns ``dayKeys``
 * (sorted ascending) and ``maxTokens`` so the colour scale and the
 * vertical axis can be driven off a single pass.
 */
export function bucketsToLocalGrid(cells: TokenHeatmapCell[]): {
  grid: Map<string, LocalBucket>
  dayKeys: string[]
  maxTokens: number
} {
  const grid = new Map<string, LocalBucket>()
  const daySet = new Set<string>()
  let maxTokens = 0
  for (const c of cells) {
    const shifted = shiftCellToLocal(c.day, c.hour)
    if (!shifted) continue
    const key = `${shifted.dayKey}:${shifted.hour}`
    const tokens = Number.isFinite(c.token_total) ? c.token_total : 0
    const cost = Number.isFinite(c.cost_total) ? c.cost_total : 0
    const prev = grid.get(key)
    if (prev) {
      prev.tokens += tokens
      prev.cost += cost
      if (prev.tokens > maxTokens) maxTokens = prev.tokens
    } else {
      grid.set(key, { tokens, cost })
      if (tokens > maxTokens) maxTokens = tokens
    }
    daySet.add(shifted.dayKey)
  }
  const dayKeys = Array.from(daySet).sort()
  return { grid, dayKeys, maxTokens }
}

/**
 * Build the full list of day keys the heatmap should display, anchored
 * on the browser's local "today" (inclusive) and extending N-1 days
 * backward. Keeps the grid a predictable 7 / 30 rows even when the
 * sparse payload has no cells for idle days.
 */
export function buildDayAxis(
  window: TokenHeatmapWindow,
  anchor: Date = new Date(),
): string[] {
  const count = window === "7d" ? 7 : 30
  const anchorStart = new Date(
    anchor.getFullYear(),
    anchor.getMonth(),
    anchor.getDate(),
  )
  const out: string[] = []
  for (let i = count - 1; i >= 0; i--) {
    const d = new Date(anchorStart)
    d.setDate(anchorStart.getDate() - i)
    out.push(localDayKey(d))
  }
  return out
}

function formatTokens(num: number): string {
  if (num >= 1_000_000) return (num / 1_000_000).toFixed(2) + "M"
  if (num >= 1_000) return (num / 1_000).toFixed(1) + "K"
  return String(num)
}

function formatCost(cost: number): string {
  if (cost <= 0) return "$0.000"
  if (cost >= 1) return "$" + cost.toFixed(2)
  return "$" + cost.toFixed(3)
}

function formatDayLabel(dayKey: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dayKey)
  if (!m) return dayKey
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  if (Number.isNaN(d.getTime())) return dayKey
  return d.toLocaleDateString(undefined, { month: "short", day: "2-digit" })
}

function formatHourRange(hour: number): string {
  const pad = (n: number) => String(n).padStart(2, "0")
  const next = (hour + 1) % 24
  return `${pad(hour)}:00–${pad(next)}:00`
}

/** ZZ.C2 #305-2 checkbox 4 (2026-04-24): sentinel for the "All models"
 *  dropdown row. Kept as an empty string so it round-trips naturally
 *  through ``URLSearchParams`` / form submit handlers (no surprising
 *  serialisation differences from ``null`` / ``undefined``). */
export const SESSION_HEATMAP_ALL_MODELS = ""

export interface SessionHeatmapProps {
  className?: string
  defaultWindow?: TokenHeatmapWindow
  /** Test seam: swap in a fake fetcher to drive the component without
   *  mocking the @/lib/api module. Signature mirrors
   *  ``fetchTokenHeatmap`` — signature widened in checkbox 4 to carry
   *  the optional per-model filter slug. */
  fetchHeatmap?: (
    window: TokenHeatmapWindow,
    model?: string | null,
  ) => Promise<TokenHeatmapResponse>
  /** Auto-refresh cadence. Pass ``0`` to disable polling (tests). */
  refreshIntervalMs?: number
}

export function SessionHeatmap({
  className = "",
  defaultWindow = "7d",
  fetchHeatmap,
  refreshIntervalMs = 60_000,
}: SessionHeatmapProps) {
  const [window, setWindow] = useState<TokenHeatmapWindow>(defaultWindow)
  const [cells, setCells] = useState<TokenHeatmapCell[]>([])
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)
  const [hoverKey, setHoverKey] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  // ZZ.C2 #305-2 checkbox 4 (2026-04-24): per-model filter.
  // Empty string == ``SESSION_HEATMAP_ALL_MODELS`` means "all models"
  // and is explicitly *not* sent to the backend (URL omits the param
  // entirely) — the sentinel is a local UI concept only.
  const [selectedModel, setSelectedModel] = useState<string>(
    SESSION_HEATMAP_ALL_MODELS,
  )
  const [availableModels, setAvailableModels] = useState<string[]>([])
  const fetchFn = fetchHeatmap ?? fetchTokenHeatmap

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const modelArg =
          selectedModel === SESSION_HEATMAP_ALL_MODELS ? null : selectedModel
        const resp = await fetchFn(window, modelArg)
        if (cancelled) return
        setCells(Array.isArray(resp.cells) ? resp.cells : [])
        // ``available_models`` is populated by checkbox-4 backends but
        // older backends may omit it — fall through to empty list so
        // the dropdown degrades to just "All models" rather than
        // crashing on ``undefined.map``.
        const models = Array.isArray(resp.available_models)
          ? resp.available_models.filter(
              (m): m is string => typeof m === "string" && m !== "",
            )
          : []
        setAvailableModels(models)
        // If the currently-selected model has disappeared from the
        // window (no more turns under that slug), silently fall back
        // to "All models" so the dropdown doesn't dangle at an option
        // that no longer exists.
        if (
          selectedModel !== SESSION_HEATMAP_ALL_MODELS &&
          !models.includes(selectedModel)
        ) {
          setSelectedModel(SESSION_HEATMAP_ALL_MODELS)
        }
      } catch (err) {
        if (cancelled) return
        const message = err instanceof Error ? err.message : String(err)
        setError(message)
        setCells([])
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    let interval: ReturnType<typeof setInterval> | null = null
    if (refreshIntervalMs > 0) {
      interval = setInterval(load, refreshIntervalMs)
    }
    return () => {
      cancelled = true
      if (interval) clearInterval(interval)
    }
  }, [window, fetchFn, refreshIntervalMs, nonce, selectedModel])

  const { grid, maxTokens } = useMemo(
    () => bucketsToLocalGrid(cells),
    [cells],
  )
  const dayAxis = useMemo(() => buildDayAxis(window), [window])

  const handleRefresh = useCallback(() => setNonce((n) => n + 1), [])

  const hoverCell = useMemo(() => {
    if (!hoverKey) return null
    const [dayKey, hourStr] = hoverKey.split(":")
    const hour = Number(hourStr)
    const bucket = grid.get(hoverKey)
    return {
      dayKey,
      hour,
      tokens: bucket?.tokens ?? 0,
      cost: bucket?.cost ?? 0,
    }
  }, [hoverKey, grid])

  // Compact cell sizes — 30d grid needs to stay readable without making
  // the whole panel a foot tall. ``px`` units, not Tailwind classes, so
  // the inline ``style`` calc pads correctly regardless of theme scale.
  const cellSize = window === "30d" ? 8 : 14

  return (
    <div
      className={`border-t border-[var(--border)] ${className}`}
      data-testid="session-heatmap"
    >
      {/* 2026-04-25 UX fix: header was `flex items-center
        * justify-between` with model-select (max-w 160 px) + 7d/30d
        * tab group (≈70 px) on the right. On narrow orchestrator
        * panels (typical 350-400 px wide) the right cluster pushed
        * past the panel edge — operator-reported "7d/30d 超出版面".
        * Switched to `flex flex-wrap gap-y-2` so on tight widths the
        * controls drop to a second line under the SESSION HEATMAP
        * label instead of clipping. `gap-x-3` between controls + ml-
        * auto on the right group keeps right-alignment when there's
        * enough room. */}
      <div className="px-4 py-2 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs font-mono text-[var(--muted-foreground)]">
        <div className="flex items-center gap-2">
          <BarChart3 size={12} className="text-[var(--neural-blue)] shrink-0" />
          <span>SESSION HEATMAP</span>
        </div>
        <div className="flex items-center gap-2 ml-auto">
          {/* ZZ.C2 #305-2 checkbox 4 (2026-04-24): per-model filter.
              ``<select>`` is deliberately a native element rather than
              a styled popover — screen readers + keyboard-only
              operators get expected semantics for free, and the
              10-ish option set a tenant usually has doesn't need
              type-ahead / virtual-scroll sugar. */}
          <select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            className="px-1.5 py-0.5 rounded font-mono text-[10px] bg-[var(--secondary)] border border-[var(--border)] text-[var(--muted-foreground)] hover:text-[var(--foreground)] max-w-[160px]"
            aria-label="Filter heatmap by model"
            data-testid="session-heatmap-model-filter"
          >
            <option
              value={SESSION_HEATMAP_ALL_MODELS}
              data-testid="session-heatmap-model-option-all"
            >
              All models
            </option>
            {availableModels.map((m) => (
              <option
                key={m}
                value={m}
                data-testid={`session-heatmap-model-option-${m}`}
              >
                {m}
              </option>
            ))}
          </select>
          <div
            className="flex items-center gap-1 p-0.5 rounded bg-[var(--secondary)] border border-[var(--border)]"
            data-testid="session-heatmap-window-tabs"
          >
            {SESSION_HEATMAP_WINDOWS.map((w) => (
              <button
                key={w}
                type="button"
                onClick={() => setWindow(w)}
                className={`px-1.5 py-0.5 rounded font-mono text-[10px] transition-colors ${
                  window === w
                    ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                    : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                }`}
                aria-pressed={window === w}
                data-testid={`session-heatmap-window-${w}`}
              >
                {w}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={handleRefresh}
            className="p-1 rounded hover:bg-[var(--secondary)] transition-colors"
            title="Refresh heatmap"
            aria-label="Refresh heatmap"
            data-testid="session-heatmap-refresh"
          >
            <RefreshCw size={11} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {error ? (
        <div
          className="mx-4 mb-3 p-2 rounded bg-[var(--critical-red)]/10 border border-[var(--critical-red)]/40 flex items-start gap-2"
          data-testid="session-heatmap-error"
        >
          <AlertCircle size={12} className="text-[var(--critical-red)] mt-0.5 shrink-0" />
          <div className="text-[10px] font-mono text-[var(--critical-red)]">
            Failed to load heatmap: {error}
          </div>
        </div>
      ) : loading && cells.length === 0 ? (
        <div
          className="px-4 pb-3 text-[10px] font-mono text-[var(--muted-foreground)]"
          data-testid="session-heatmap-loading"
        >
          Loading session heatmap…
        </div>
      ) : dayAxis.length === 0 ? (
        <div
          className="px-4 pb-3 text-[10px] font-mono text-[var(--muted-foreground)]"
          data-testid="session-heatmap-empty"
        >
          No session activity in the last {window}
        </div>
      ) : (
        <SessionHeatmapGrid
          cellSize={cellSize}
          dayAxis={dayAxis}
          grid={grid}
          maxTokens={maxTokens}
          hoverKey={hoverKey}
          onHover={setHoverKey}
        />
      )}

      {hoverCell && (
        <div
          className="mx-4 mb-3 p-2 rounded bg-[var(--secondary)] border border-[var(--border)] flex items-center gap-3 text-[10px] font-mono"
          role="status"
          data-testid="session-heatmap-tooltip"
        >
          <span className="text-[var(--foreground)]" data-testid="session-heatmap-tooltip-day">
            {formatDayLabel(hoverCell.dayKey)}
          </span>
          <span className="text-[var(--muted-foreground)]">·</span>
          <span className="text-[var(--muted-foreground)]" data-testid="session-heatmap-tooltip-hour">
            {formatHourRange(hoverCell.hour)}
          </span>
          <span className="text-[var(--muted-foreground)]">·</span>
          <span className="text-[var(--validation-emerald)]" data-testid="session-heatmap-tooltip-tokens">
            {formatTokens(hoverCell.tokens)} tokens
          </span>
          <span className="text-[var(--muted-foreground)]">·</span>
          <span className="text-[var(--hardware-orange)]" data-testid="session-heatmap-tooltip-cost">
            {formatCost(hoverCell.cost)}
          </span>
        </div>
      )}
    </div>
  )
}

interface SessionHeatmapGridProps {
  cellSize: number
  dayAxis: string[]
  grid: Map<string, LocalBucket>
  maxTokens: number
  hoverKey: string | null
  onHover: (key: string | null) => void
}

function SessionHeatmapGrid({
  cellSize,
  dayAxis,
  grid,
  maxTokens,
  hoverKey,
  onHover,
}: SessionHeatmapGridProps) {
  const hours = Array.from({ length: 24 }, (_, i) => i)
  // Show the 0 / 6 / 12 / 18 guide labels only — labelling every hour
  // at 8px cell width would overflow.
  const labelledHours = new Set([0, 6, 12, 18])
  return (
    <div className="px-4 pb-3 overflow-x-auto" data-testid="session-heatmap-grid">
      <div className="inline-block">
        {/* Hour-of-day axis (top) */}
        <div
          className="flex"
          style={{ marginLeft: 64 }}
          data-testid="session-heatmap-hour-axis"
        >
          {hours.map((h) => (
            <div
              key={h}
              className="text-[8px] font-mono text-[var(--muted-foreground)] text-center"
              style={{ width: cellSize + 2, height: 10 }}
            >
              {labelledHours.has(h) ? h : ""}
            </div>
          ))}
        </div>
        {/* Rows */}
        {dayAxis.map((dayKey) => (
          <div
            key={dayKey}
            className="flex items-center"
            data-testid="session-heatmap-row"
            data-day-key={dayKey}
          >
            <div
              className="text-[9px] font-mono text-[var(--muted-foreground)] text-right pr-2 shrink-0"
              style={{ width: 62, height: cellSize + 2 }}
            >
              {formatDayLabel(dayKey)}
            </div>
            {hours.map((h) => {
              const key = `${dayKey}:${h}`
              const bucket = grid.get(key)
              const tokens = bucket?.tokens ?? 0
              const intensity = logScaleIntensity(tokens, maxTokens)
              const isActive = hoverKey === key
              // ``color-mix`` blends the brand emerald against the
              // empty-cell background at the log intensity so a
              // 1-token cell is a faint tint and the max-token cell
              // is full brand emerald.
              const bg =
                tokens > 0
                  ? `color-mix(in srgb, var(--validation-emerald) ${(
                      20 + intensity * 80
                    ).toFixed(1)}%, var(--secondary))`
                  : "var(--secondary)"
              const title = bucket
                ? `${formatDayLabel(dayKey)} ${formatHourRange(h)} · ${formatTokens(
                    tokens,
                  )} tokens · ${formatCost(bucket.cost)}`
                : `${formatDayLabel(dayKey)} ${formatHourRange(h)} · no activity`
              return (
                <button
                  key={key}
                  type="button"
                  className={`shrink-0 rounded-[2px] transition-transform ${
                    isActive ? "ring-1 ring-[var(--neural-blue)] scale-110" : ""
                  }`}
                  style={{
                    width: cellSize,
                    height: cellSize,
                    margin: 1,
                    backgroundColor: bg,
                  }}
                  onMouseEnter={() => onHover(key)}
                  onFocus={() => onHover(key)}
                  onMouseLeave={() => onHover(null)}
                  onBlur={() => onHover(null)}
                  title={title}
                  aria-label={title}
                  data-testid="session-heatmap-cell"
                  data-day-key={dayKey}
                  data-hour={h}
                  data-tokens={tokens}
                  data-intensity={intensity.toFixed(3)}
                />
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

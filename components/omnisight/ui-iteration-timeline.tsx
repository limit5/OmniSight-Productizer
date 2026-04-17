/**
 * V3 #4 (TODO row 1524) — UI iteration timeline.
 *
 * Horizontal timeline of agent-driven UI iterations.  Each time an
 * agent finishes a change, the sandbox emits a snapshot that bundles
 * three things:
 *
 *   1. A preview screenshot of the sandbox at that moment.
 *   2. A unified code diff describing what the agent changed.
 *   3. Provenance metadata (optional commit SHA, short summary, agent
 *      id, timestamp).
 *
 * The timeline lays those snapshots out left-to-right in chronological
 * order, oldest on the left, newest on the right.  Clicking any node
 * selects that version and expands a detail pane beneath the axis
 * showing **both** the preview screenshot and the code diff — the
 * "preview + code 都回到該版本" requirement from the TODO row.  The
 * component does not touch the sandbox filesystem; it emits
 * `onRollback(snapshot)` so the V3 #5 consumer can issue the actual
 * `git checkout` command (which is a different checkbox — this one
 * ends at the UI affordance).
 *
 * Sibling V3 components:
 *   - V3 #1 `visual-annotator.tsx` — the operator annotates the
 *     **current** preview; the timeline stores **historical** previews.
 *     Their schemas are intentionally disjoint — annotations refer to
 *     a single image via normalised coordinates, snapshots are a
 *     versioned list of images + diffs.
 *   - V3 #3 `element-inspector.tsx` — inspects a **live** React tree;
 *     timeline is pure static content (diff text + image URL), so no
 *     overlap in exports either.
 *
 * Controlled + uncontrolled:
 *   Matches the rest of the workspace component family —
 *   `iterations` / `defaultIterations` pin the list, `activeId` /
 *   `defaultActiveId` pin the selected version.  `onActiveChange`
 *   fires on every selection flip (including null), `onRollback`
 *   fires only when the operator explicitly clicks "回到此版本".
 *
 * Coordinate / ordering contract:
 *   The component **does not** mutate the input list's order.  If the
 *   caller passes iterations in reverse-chronological order, the
 *   timeline renders them in reverse-chronological order.  A pure
 *   helper `sortIterationsAscending` is exported for the common case
 *   (backends usually return newest-first).  Whatever order the
 *   caller chose is honoured so the timeline axis reflects the
 *   caller's mental model.
 */
"use client"

import * as React from "react"
import {
  ArrowLeftRight,
  Bot,
  Camera,
  GitCommit,
  History,
  RotateCcw,
  X,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

// ─── Public shapes ─────────────────────────────────────────────────────────

export interface IterationDiffStats {
  /** Lines starting with `+` (excluding unified-diff file headers `+++`). */
  additions: number
  /** Lines starting with `-` (excluding unified-diff file headers `---`). */
  deletions: number
  /**
   * Number of `diff --git` headers in the blob — i.e. how many files
   * the iteration touched.  `0` if the diff is empty or unstructured.
   */
  filesChanged: number
}

export interface IterationSnapshot {
  /** Unique stable id — usually the commit SHA or synthesised row id. */
  id: string
  /**
   * Optional git commit SHA produced by the sandbox.  Separate from
   * `id` because some agents commit to a scratch branch and resolve
   * the SHA asynchronously.
   */
  commitSha?: string | null
  /**
   * URL (or data: URI) for the preview screenshot captured at this
   * iteration.  Empty string renders a "no screenshot" placeholder.
   */
  screenshotSrc: string
  /** Alt text for the screenshot. */
  screenshotAlt?: string
  /** Unified code diff text produced by the agent. */
  diff: string
  /** Short human-readable summary (commit subject). */
  summary: string
  /** Identifier of the agent that produced the iteration. */
  agentId?: string | null
  /** ISO-8601 creation timestamp. */
  createdAt: string
  /**
   * Pre-computed diff stats.  Skipped?  The component falls back to
   * `parseDiffStats(diff)`.  Supplying this saves re-parsing a large
   * diff on every render.
   */
  diffStats?: IterationDiffStats
}

export interface UiIterationTimelineProps {
  /** Controlled iteration list. */
  iterations?: IterationSnapshot[]
  /** Uncontrolled initial iteration list. */
  defaultIterations?: IterationSnapshot[]
  /** Controlled active iteration id.  `null` = nothing selected. */
  activeId?: string | null
  /** Uncontrolled initial active id. */
  defaultActiveId?: string | null
  /** Fires on every selection change (including clearing to null). */
  onActiveChange?: (id: string | null) => void
  /**
   * Fires when the operator clicks "回到此版本".  The V3 #5 consumer
   * wires this to the sandbox git checkout.  Never called with `null`
   * — if the rollback target is absent, the button is hidden.
   */
  onRollback?: (snapshot: IterationSnapshot) => void
  /** Label on the rollback button. */
  rollbackLabel?: string
  /** Disable all interactions (read-only mode). */
  disabled?: boolean
  /** Override the root class. */
  className?: string
  /** Text shown when `iterations` is empty. */
  emptyMessage?: string
  /**
   * Test seam: return the "current" moment for relative-time rendering.
   * Defaults to `new Date()` — tests inject a fixed clock for
   * deterministic snapshot-time labels.
   */
  nowProvider?: () => Date
  /**
   * Test seam: custom diff-stats parser.  Defaults to `parseDiffStats`.
   * Useful when callers have a cheaper structured-diff format.
   */
  parseDiffStatsImpl?: (diff: string) => IterationDiffStats
  /**
   * Test seam: custom relative-time formatter.  Defaults to
   * `formatRelativeTime`.  Callers with i18n/"5 分鐘前" variants can
   * swap it without patching the component.
   */
  formatRelativeImpl?: (iso: string, now: Date) => string
}

// ─── Pure helpers (exported for test coverage) ────────────────────────────

/**
 * Parse a unified diff into addition / deletion line counts and a
 * count of `diff --git` file headers.  Robust against empty strings,
 * non-diff blobs, and mixed line endings.  Does **not** throw.
 *
 * Why skip `+++` and `---`:
 *   Unified diffs use `+++ b/path` / `--- a/path` as the file-header
 *   pair.  Counting those as additions/deletions would double-count
 *   every file, producing noise on the "+12 / -3" badge.
 */
export function parseDiffStats(diff: string): IterationDiffStats {
  if (typeof diff !== "string" || diff.length === 0) {
    return { additions: 0, deletions: 0, filesChanged: 0 }
  }
  let additions = 0
  let deletions = 0
  let filesChanged = 0
  // Split on any line ending — CRLF / LF / CR all valid in diffs.
  const lines = diff.split(/\r\n|\n|\r/)
  for (const line of lines) {
    if (line.startsWith("diff --git")) {
      filesChanged += 1
      continue
    }
    if (line.startsWith("+++")) continue
    if (line.startsWith("---")) continue
    if (line.length === 0) continue
    if (line[0] === "+") {
      additions += 1
      continue
    }
    if (line[0] === "-") {
      deletions += 1
      continue
    }
  }
  return { additions, deletions, filesChanged }
}

/**
 * Render a human-readable "X ago" string.  Matches the set of buckets
 * GitHub / Linear surface — operators scan these every few minutes so
 * the resolution tops out at "Xd ago" before collapsing to the date.
 *
 * Edge cases:
 *   - Invalid ISO string → returns the raw input (never throws).
 *   - Future timestamps (clock skew) → "just now".
 */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  if (typeof iso !== "string" || iso.length === 0) return ""
  const stamp = Date.parse(iso)
  if (Number.isNaN(stamp)) return iso
  const deltaMs = now.getTime() - stamp
  if (deltaMs < 0) return "just now"
  const deltaSec = Math.floor(deltaMs / 1000)
  if (deltaSec < 45) return "just now"
  const deltaMin = Math.floor(deltaSec / 60)
  if (deltaMin < 60) return `${deltaMin}m ago`
  const deltaHr = Math.floor(deltaMin / 60)
  if (deltaHr < 24) return `${deltaHr}h ago`
  const deltaDay = Math.floor(deltaHr / 24)
  if (deltaDay < 7) return `${deltaDay}d ago`
  const deltaWk = Math.floor(deltaDay / 7)
  if (deltaWk < 5) return `${deltaWk}w ago`
  // Fall back to a compact date (avoid year for readability when current).
  const date = new Date(stamp)
  return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`
}

/**
 * Shorten a commit SHA to the conventional 7-char prefix.  Anything
 * shorter than 7 characters passes through unchanged so tests that
 * use human ids ("rev-1") still read as-is.  Returns "" for null /
 * undefined / empty so JSX can `sha && <Badge>{sha}</Badge>` cleanly.
 */
export function shortCommitSha(sha: string | null | undefined): string {
  if (sha === null || sha === undefined) return ""
  if (typeof sha !== "string") return ""
  if (sha.length <= 7) return sha
  return sha.slice(0, 7)
}

/**
 * Stable-sort iterations ascending by `createdAt` (oldest first,
 * which is the timeline's natural left-to-right order).  Returns a
 * **new** array — never mutates the input.  Iterations with identical
 * `createdAt` preserve their input order (Array.prototype.sort is
 * stable in every modern engine).
 */
export function sortIterationsAscending(
  iterations: readonly IterationSnapshot[],
): IterationSnapshot[] {
  const copy = iterations.slice()
  copy.sort((a, b) => {
    const aMs = Date.parse(a.createdAt) || 0
    const bMs = Date.parse(b.createdAt) || 0
    return aMs - bMs
  })
  return copy
}

/** Mirror of `sortIterationsAscending` but newest-first. */
export function sortIterationsDescending(
  iterations: readonly IterationSnapshot[],
): IterationSnapshot[] {
  const copy = iterations.slice()
  copy.sort((a, b) => {
    const aMs = Date.parse(a.createdAt) || 0
    const bMs = Date.parse(b.createdAt) || 0
    return bMs - aMs
  })
  return copy
}

/**
 * Locate an iteration by id.  Returns null when id is null / undefined /
 * empty or when no match exists.  Never throws.
 */
export function findIteration(
  iterations: readonly IterationSnapshot[],
  id: string | null | undefined,
): IterationSnapshot | null {
  if (id === null || id === undefined || id === "") return null
  for (const it of iterations) {
    if (it.id === id) return it
  }
  return null
}

/**
 * Compute per-node axis positions as percentages along the timeline.
 * Single iteration is centred at 50 %.  Zero iterations returns `[]`.
 * Positions map to the iteration's index in the **provided** order —
 * the component does not re-sort, so callers control orientation.
 */
export function computeTimelinePositions(
  iterations: readonly IterationSnapshot[],
): number[] {
  const n = iterations.length
  if (n === 0) return []
  if (n === 1) return [50]
  const result: number[] = []
  for (let i = 0; i < n; i += 1) {
    result.push((i / (n - 1)) * 100)
  }
  return result
}

/**
 * Decide the effective active id given a caller-supplied id + the
 * current list.  If the caller's id no longer exists in the list
 * (e.g. the iteration was pruned), fall back to null so the detail
 * pane closes gracefully instead of rendering stale content.
 */
export function resolveActiveId(
  iterations: readonly IterationSnapshot[],
  candidate: string | null | undefined,
): string | null {
  if (!candidate) return null
  return findIteration(iterations, candidate) ? candidate : null
}

// ─── Defaults / constants ──────────────────────────────────────────────────

const DEFAULT_EMPTY_MESSAGE =
  "No iterations yet — the timeline will populate as the agent makes changes."
const DEFAULT_ROLLBACK_LABEL = "回到此版本"

// ─── Component ─────────────────────────────────────────────────────────────

export function UiIterationTimeline({
  iterations,
  defaultIterations,
  activeId,
  defaultActiveId,
  onActiveChange,
  onRollback,
  rollbackLabel = DEFAULT_ROLLBACK_LABEL,
  disabled = false,
  className,
  emptyMessage = DEFAULT_EMPTY_MESSAGE,
  nowProvider,
  parseDiffStatsImpl = parseDiffStats,
  formatRelativeImpl = formatRelativeTime,
}: UiIterationTimelineProps) {
  // ─ Controlled / uncontrolled state wiring ───────────────────────────────
  const isIterationsControlled = iterations !== undefined
  const [internalIterations, setInternalIterations] = React.useState<IterationSnapshot[]>(
    () => (defaultIterations ? defaultIterations.map((it) => ({ ...it })) : []),
  )
  const effectiveIterations = isIterationsControlled
    ? (iterations as IterationSnapshot[])
    : internalIterations

  const isActiveControlled = activeId !== undefined
  const [internalActiveId, setInternalActiveId] = React.useState<string | null>(
    defaultActiveId ?? null,
  )
  const rawActiveId = isActiveControlled
    ? (activeId as string | null)
    : internalActiveId
  const effectiveActiveId = resolveActiveId(effectiveIterations, rawActiveId)

  // If a controlled caller points at a missing id, clear their
  // selection via callback so their state does not go stale.  Only
  // runs in controlled mode — uncontrolled state is already
  // self-healing because internal state flows through resolveActiveId.
  React.useEffect(() => {
    if (!isActiveControlled) {
      if (internalActiveId !== null && effectiveActiveId === null) {
        setInternalActiveId(null)
      }
      return
    }
    if (rawActiveId !== null && rawActiveId !== undefined && effectiveActiveId === null) {
      onActiveChange?.(null)
    }
    // We intentionally depend on the *ids* only — depending on
    // `iterations` would fire the effect every render when the parent
    // reconstructs its array, flooding onActiveChange.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActiveControlled, rawActiveId, effectiveActiveId, internalActiveId])

  const applyActiveUpdate = React.useCallback(
    (next: string | null) => {
      if (!isActiveControlled) setInternalActiveId(next)
      if (next !== effectiveActiveId) onActiveChange?.(next)
    },
    [effectiveActiveId, isActiveControlled, onActiveChange],
  )

  // ─ Derived ────────────────────────────────────────────────────────────
  const positions = React.useMemo(
    () => computeTimelinePositions(effectiveIterations),
    [effectiveIterations],
  )
  const activeSnapshot = findIteration(effectiveIterations, effectiveActiveId)
  const activeIndex = activeSnapshot
    ? effectiveIterations.findIndex((it) => it.id === activeSnapshot.id)
    : -1

  const now = React.useMemo(
    () => (nowProvider ? nowProvider() : new Date()),
    [nowProvider],
  )

  // ─ Handlers ──────────────────────────────────────────────────────────
  const handleNodeClick = React.useCallback(
    (snapshot: IterationSnapshot) => {
      if (disabled) return
      // Clicking the already-active node clears selection — matches
      // the pin/unpin pattern V3 #3 uses so operators can dismiss the
      // detail pane with a second click.
      if (snapshot.id === effectiveActiveId) {
        applyActiveUpdate(null)
        return
      }
      applyActiveUpdate(snapshot.id)
    },
    [applyActiveUpdate, disabled, effectiveActiveId],
  )

  const handleRollbackClick = React.useCallback(() => {
    if (disabled) return
    if (!activeSnapshot) return
    onRollback?.(activeSnapshot)
  }, [activeSnapshot, disabled, onRollback])

  const handleCloseDetail = React.useCallback(() => {
    if (disabled) return
    applyActiveUpdate(null)
  }, [applyActiveUpdate, disabled])

  const handleKeyDown = React.useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (disabled) return
      if (effectiveIterations.length === 0) return
      if (event.key === "Escape") {
        if (effectiveActiveId !== null) {
          event.preventDefault()
          applyActiveUpdate(null)
        }
        return
      }
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return
      event.preventDefault()
      const n = effectiveIterations.length
      let nextIndex: number
      if (activeIndex < 0) {
        // No selection yet — land on the first / last node depending on arrow.
        nextIndex = event.key === "ArrowLeft" ? n - 1 : 0
      } else if (event.key === "ArrowLeft") {
        nextIndex = (activeIndex - 1 + n) % n
      } else {
        nextIndex = (activeIndex + 1) % n
      }
      applyActiveUpdate(effectiveIterations[nextIndex].id)
    },
    [activeIndex, applyActiveUpdate, disabled, effectiveActiveId, effectiveIterations],
  )

  // ─ Render ────────────────────────────────────────────────────────────
  const hasIterations = effectiveIterations.length > 0

  return (
    <div
      data-testid="ui-iteration-timeline"
      data-disabled={disabled ? "true" : "false"}
      data-has-iterations={hasIterations ? "true" : "false"}
      data-active-id={effectiveActiveId ?? ""}
      role="group"
      aria-label="UI iteration timeline"
      tabIndex={disabled ? -1 : 0}
      onKeyDown={handleKeyDown}
      className={cn(
        "relative flex min-h-0 w-full flex-col gap-3 rounded-md border border-border bg-card/40 p-3 outline-none",
        disabled && "opacity-60",
        className,
      )}
    >
      <header className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <History className="size-4 text-muted-foreground" aria-hidden="true" />
          <h3
            data-testid="ui-iteration-timeline-title"
            className="text-sm font-semibold"
          >
            Iteration timeline
          </h3>
          <Badge
            data-testid="ui-iteration-timeline-count"
            variant="secondary"
            className="font-mono text-[10px]"
          >
            {effectiveIterations.length}
          </Badge>
        </div>
        {activeSnapshot && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            data-testid="ui-iteration-timeline-close-detail"
            aria-label="Close iteration detail"
            disabled={disabled}
            onClick={handleCloseDetail}
          >
            <X className="size-3" aria-hidden="true" />
          </Button>
        )}
      </header>

      {!hasIterations ? (
        <div
          data-testid="ui-iteration-timeline-empty"
          className="rounded-sm border border-dashed border-border bg-muted/30 px-3 py-6 text-center text-xs text-muted-foreground"
        >
          {emptyMessage}
        </div>
      ) : (
        <>
          <TimelineAxis
            iterations={effectiveIterations}
            positions={positions}
            activeId={effectiveActiveId}
            disabled={disabled}
            now={now}
            parseDiffStatsImpl={parseDiffStatsImpl}
            formatRelativeImpl={formatRelativeImpl}
            onNodeClick={handleNodeClick}
          />
          {activeSnapshot && (
            <IterationDetailPanel
              snapshot={activeSnapshot}
              index={activeIndex}
              total={effectiveIterations.length}
              disabled={disabled}
              parseDiffStatsImpl={parseDiffStatsImpl}
              formatRelativeImpl={formatRelativeImpl}
              now={now}
              rollbackLabel={rollbackLabel}
              onRollback={onRollback ? handleRollbackClick : undefined}
            />
          )}
        </>
      )}
    </div>
  )
}

// ─── Axis sub-component ───────────────────────────────────────────────────

interface TimelineAxisProps {
  iterations: IterationSnapshot[]
  positions: number[]
  activeId: string | null
  disabled: boolean
  now: Date
  parseDiffStatsImpl: (diff: string) => IterationDiffStats
  formatRelativeImpl: (iso: string, now: Date) => string
  onNodeClick: (snapshot: IterationSnapshot) => void
}

function TimelineAxis({
  iterations,
  positions,
  activeId,
  disabled,
  now,
  parseDiffStatsImpl,
  formatRelativeImpl,
  onNodeClick,
}: TimelineAxisProps) {
  return (
    <div
      data-testid="ui-iteration-timeline-axis"
      className="relative w-full overflow-x-auto overflow-y-visible pb-10 pt-2"
    >
      <div className="relative h-1 w-full min-w-full rounded-full bg-border">
        {iterations.map((snapshot, idx) => {
          const pct = positions[idx] ?? 0
          const isActive = snapshot.id === activeId
          return (
            <button
              key={snapshot.id}
              type="button"
              data-testid={`ui-iteration-timeline-node-${snapshot.id}`}
              data-active={isActive ? "true" : "false"}
              data-index={idx}
              aria-label={`Iteration ${idx + 1}: ${snapshot.summary || snapshot.id}`}
              aria-pressed={isActive}
              disabled={disabled}
              onClick={() => onNodeClick(snapshot)}
              className={cn(
                "absolute top-1/2 flex -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border-2 bg-background outline-none transition-transform",
                "hover:scale-110 focus-visible:ring-2 focus-visible:ring-ring",
                "disabled:cursor-not-allowed disabled:opacity-60",
                isActive
                  ? "size-4 border-primary bg-primary"
                  : "size-3 border-border",
              )}
              style={{ left: `${pct}%` }}
            >
              <span className="sr-only">{snapshot.summary || snapshot.id}</span>
            </button>
          )
        })}
      </div>
      <div className="relative mt-3 h-6 w-full text-[10px] text-muted-foreground">
        {iterations.map((snapshot, idx) => {
          const pct = positions[idx] ?? 0
          const isActive = snapshot.id === activeId
          return (
            <span
              key={`${snapshot.id}-label`}
              data-testid={`ui-iteration-timeline-node-label-${snapshot.id}`}
              className={cn(
                "absolute -translate-x-1/2 whitespace-nowrap font-mono",
                isActive ? "text-foreground" : "text-muted-foreground",
              )}
              style={{ left: `${pct}%` }}
            >
              {formatRelativeImpl(snapshot.createdAt, now)}
            </span>
          )
        })}
      </div>
      <div className="sr-only" data-testid="ui-iteration-timeline-axis-stats">
        {iterations.length} iterations,
        {iterations
          .map((snapshot) => {
            const stats = snapshot.diffStats ?? parseDiffStatsImpl(snapshot.diff)
            return ` ${snapshot.id} +${stats.additions} / -${stats.deletions}`
          })
          .join(";")}
      </div>
    </div>
  )
}

// ─── Detail-panel sub-component ───────────────────────────────────────────

interface IterationDetailPanelProps {
  snapshot: IterationSnapshot
  index: number
  total: number
  disabled: boolean
  parseDiffStatsImpl: (diff: string) => IterationDiffStats
  formatRelativeImpl: (iso: string, now: Date) => string
  now: Date
  rollbackLabel: string
  onRollback?: () => void
}

function IterationDetailPanel({
  snapshot,
  index,
  total,
  disabled,
  parseDiffStatsImpl,
  formatRelativeImpl,
  now,
  rollbackLabel,
  onRollback,
}: IterationDetailPanelProps) {
  const stats = snapshot.diffStats ?? parseDiffStatsImpl(snapshot.diff)
  const shortSha = shortCommitSha(snapshot.commitSha)
  const hasScreenshot = typeof snapshot.screenshotSrc === "string" && snapshot.screenshotSrc.length > 0

  return (
    <aside
      data-testid="ui-iteration-timeline-detail"
      data-snapshot-id={snapshot.id}
      aria-label={`Iteration ${index + 1} detail`}
      className="flex flex-col gap-3 rounded-md border border-border bg-background/80 p-3"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            data-testid="ui-iteration-timeline-detail-index"
            variant="secondary"
            className="font-mono text-[10px]"
          >
            {index + 1} / {total}
          </Badge>
          {shortSha && (
            <Badge
              data-testid="ui-iteration-timeline-detail-sha"
              variant="outline"
              className="flex items-center gap-1 font-mono text-[10px]"
            >
              <GitCommit className="size-3" aria-hidden="true" />
              {shortSha}
            </Badge>
          )}
          {snapshot.agentId && (
            <Badge
              data-testid="ui-iteration-timeline-detail-agent"
              variant="outline"
              className="flex items-center gap-1 text-[10px]"
            >
              <Bot className="size-3" aria-hidden="true" />
              {snapshot.agentId}
            </Badge>
          )}
          <span
            data-testid="ui-iteration-timeline-detail-timestamp"
            className="font-mono text-[10px] text-muted-foreground"
            title={snapshot.createdAt}
          >
            {formatRelativeImpl(snapshot.createdAt, now)}
          </span>
        </div>
        {onRollback && (
          <Button
            type="button"
            variant="default"
            size="sm"
            data-testid="ui-iteration-timeline-rollback"
            aria-label={rollbackLabel}
            disabled={disabled}
            onClick={onRollback}
            className="gap-1.5"
          >
            <RotateCcw className="size-3" aria-hidden="true" />
            {rollbackLabel}
          </Button>
        )}
      </header>

      {snapshot.summary && (
        <p
          data-testid="ui-iteration-timeline-detail-summary"
          className="text-sm font-medium"
        >
          {snapshot.summary}
        </p>
      )}

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <figure
          data-testid="ui-iteration-timeline-detail-preview"
          className="flex min-h-0 flex-col gap-1 rounded-sm border border-border bg-muted/20 p-2"
        >
          <figcaption className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <Camera className="size-3" aria-hidden="true" />
            Preview
          </figcaption>
          {hasScreenshot ? (
            <img
              data-testid="ui-iteration-timeline-detail-preview-img"
              src={snapshot.screenshotSrc}
              alt={snapshot.screenshotAlt ?? `Preview at iteration ${index + 1}`}
              className="h-auto w-full rounded-sm border border-border"
            />
          ) : (
            <span
              data-testid="ui-iteration-timeline-detail-preview-empty"
              className="block rounded-sm border border-dashed border-border bg-muted/30 px-2 py-6 text-center text-[11px] text-muted-foreground"
            >
              (no screenshot available)
            </span>
          )}
        </figure>

        <div
          data-testid="ui-iteration-timeline-detail-diff"
          className="flex min-h-0 flex-col gap-1 rounded-sm border border-border bg-muted/20 p-2"
        >
          <div className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span className="flex items-center gap-1">
              <ArrowLeftRight className="size-3" aria-hidden="true" />
              Code diff
            </span>
            <span
              data-testid="ui-iteration-timeline-detail-diff-stats"
              className="flex items-center gap-2 font-mono normal-case"
              aria-label={`${stats.additions} additions and ${stats.deletions} deletions across ${stats.filesChanged} files`}
            >
              <span className="text-emerald-600 dark:text-emerald-400">
                +{stats.additions}
              </span>
              <span className="text-rose-600 dark:text-rose-400">
                -{stats.deletions}
              </span>
              {stats.filesChanged > 0 && (
                <span
                  data-testid="ui-iteration-timeline-detail-files-changed"
                  className="text-muted-foreground"
                >
                  {stats.filesChanged}f
                </span>
              )}
            </span>
          </div>
          {snapshot.diff && snapshot.diff.length > 0 ? (
            <pre
              data-testid="ui-iteration-timeline-detail-diff-body"
              className="max-h-[28rem] overflow-auto whitespace-pre rounded-sm border border-border bg-background p-2 font-mono text-[11px] leading-snug"
            >
              {snapshot.diff}
            </pre>
          ) : (
            <span
              data-testid="ui-iteration-timeline-detail-diff-empty"
              className="block rounded-sm border border-dashed border-border bg-muted/30 px-2 py-6 text-center text-[11px] text-muted-foreground"
            >
              (no code changes)
            </span>
          )}
        </div>
      </div>
    </aside>
  )
}

export default UiIterationTimeline

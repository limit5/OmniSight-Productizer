/**
 * V3 #4 (TODO row 1524) — Contract tests for `ui-iteration-timeline.tsx`.
 *
 * Covers the pure helpers (diff stats parser, relative-time formatter,
 * SHA shortener, sorters, finder, position layout, active-id resolver)
 * and the component's render / selection / rollback / keyboard /
 * controlled flows.  jsdom happily mounts the component without any
 * layout seams because the timeline does not read pointer coordinates
 * or bounding rects — positions flow through the pure helper.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"
import * as React from "react"

import {
  UiIterationTimeline,
  computeTimelinePositions,
  findIteration,
  formatRelativeTime,
  parseDiffStats,
  resolveActiveId,
  shortCommitSha,
  sortIterationsAscending,
  sortIterationsDescending,
  type IterationSnapshot,
} from "@/components/omnisight/ui-iteration-timeline"

// Sibling exports we ensure do not collide.
import * as VisualAnnotatorExports from "@/components/omnisight/visual-annotator"
import * as ElementInspectorExports from "@/components/omnisight/element-inspector"
import * as TimelineExports from "@/components/omnisight/ui-iteration-timeline"

// ─── Helpers ───────────────────────────────────────────────────────────────

function makeSnapshot(
  overrides: Partial<IterationSnapshot> = {},
): IterationSnapshot {
  return {
    id: "iter-1",
    commitSha: "abcdef1234567890abcdef1234567890abcdef12",
    screenshotSrc: "data:image/png;base64,iVBORw0KGgo=",
    screenshotAlt: "Iteration 1 preview",
    diff: "diff --git a/file.tsx b/file.tsx\n+++ b/file.tsx\n--- a/file.tsx\n+added line\n-removed line\n unchanged",
    summary: "Agent: tighten card padding",
    agentId: "ui-agent-alpha",
    createdAt: "2026-04-18T10:00:00.000Z",
    ...overrides,
  }
}

function makeSeries(n: number, step = 60_000): IterationSnapshot[] {
  const base = Date.parse("2026-04-18T10:00:00.000Z")
  return Array.from({ length: n }, (_, i) =>
    makeSnapshot({
      id: `iter-${i + 1}`,
      commitSha: `${String(i).repeat(4).padEnd(40, "0").slice(0, 40)}`,
      summary: `Change #${i + 1}`,
      createdAt: new Date(base + i * step).toISOString(),
    }),
  )
}

const FIXED_NOW = () => new Date("2026-04-18T10:30:00.000Z")

// ─── Pure helper: parseDiffStats ──────────────────────────────────────────

describe("parseDiffStats", () => {
  it("returns zeros for empty or non-string input", () => {
    expect(parseDiffStats("")).toEqual({ additions: 0, deletions: 0, filesChanged: 0 })
    // Non-string input is defensively coerced (contract promise).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(parseDiffStats(null as any)).toEqual({ additions: 0, deletions: 0, filesChanged: 0 })
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(parseDiffStats(undefined as any)).toEqual({ additions: 0, deletions: 0, filesChanged: 0 })
  })

  it("counts `+` and `-` lines ignoring file-header `+++` / `---`", () => {
    const diff = [
      "diff --git a/x b/x",
      "--- a/x",
      "+++ b/x",
      "+alpha",
      "+beta",
      "-removed",
      " context",
    ].join("\n")
    expect(parseDiffStats(diff)).toEqual({
      additions: 2,
      deletions: 1,
      filesChanged: 1,
    })
  })

  it("counts multiple file headers", () => {
    const diff = [
      "diff --git a/a b/a",
      "+++ b/a",
      "--- a/a",
      "+first",
      "diff --git a/b b/b",
      "+++ b/b",
      "--- a/b",
      "+second",
      "-gone",
    ].join("\n")
    expect(parseDiffStats(diff)).toEqual({
      additions: 2,
      deletions: 1,
      filesChanged: 2,
    })
  })

  it("handles CRLF line endings", () => {
    const diff = "+one\r\n-two\r\n+three"
    expect(parseDiffStats(diff)).toEqual({
      additions: 2,
      deletions: 1,
      filesChanged: 0,
    })
  })

  it("treats a blob without diff markers as zero stats", () => {
    expect(parseDiffStats("not a diff\nstill not\n")).toEqual({
      additions: 0,
      deletions: 0,
      filesChanged: 0,
    })
  })
})

// ─── Pure helper: formatRelativeTime ──────────────────────────────────────

describe("formatRelativeTime", () => {
  const now = new Date("2026-04-18T12:00:00.000Z")

  it("returns 'just now' for timestamps under 45s ago", () => {
    expect(formatRelativeTime("2026-04-18T11:59:59.000Z", now)).toBe("just now")
    expect(formatRelativeTime("2026-04-18T11:59:30.000Z", now)).toBe("just now")
  })

  it("returns 'just now' for future timestamps (clock skew)", () => {
    expect(formatRelativeTime("2026-04-18T12:05:00.000Z", now)).toBe("just now")
  })

  it("returns minute-granularity buckets for <60m", () => {
    expect(formatRelativeTime("2026-04-18T11:58:00.000Z", now)).toBe("2m ago")
    expect(formatRelativeTime("2026-04-18T11:01:00.000Z", now)).toBe("59m ago")
  })

  it("returns hour-granularity buckets for <24h", () => {
    expect(formatRelativeTime("2026-04-18T09:00:00.000Z", now)).toBe("3h ago")
    expect(formatRelativeTime("2026-04-17T13:00:00.000Z", now)).toBe("23h ago")
  })

  it("returns day-granularity buckets for <7d", () => {
    expect(formatRelativeTime("2026-04-16T12:00:00.000Z", now)).toBe("2d ago")
    expect(formatRelativeTime("2026-04-12T12:00:00.000Z", now)).toBe("6d ago")
  })

  it("returns week-granularity for <5w", () => {
    expect(formatRelativeTime("2026-04-04T12:00:00.000Z", now)).toBe("2w ago")
  })

  it("falls back to ISO date for anything older than 5w", () => {
    expect(formatRelativeTime("2026-01-01T00:00:00.000Z", now)).toBe("2026-01-01")
  })

  it("returns empty string / raw value for invalid input", () => {
    expect(formatRelativeTime("", now)).toBe("")
    expect(formatRelativeTime("not-a-date", now)).toBe("not-a-date")
  })
})

// ─── Pure helper: shortCommitSha ───────────────────────────────────────────

describe("shortCommitSha", () => {
  it("truncates to 7 chars for long SHAs", () => {
    expect(shortCommitSha("abcdef1234567890abcdef1234567890abcdef12")).toBe("abcdef1")
  })

  it("passes through short ids unchanged", () => {
    expect(shortCommitSha("rev-1")).toBe("rev-1")
    expect(shortCommitSha("1234567")).toBe("1234567")
  })

  it("returns empty string for null / undefined / non-string", () => {
    expect(shortCommitSha(null)).toBe("")
    expect(shortCommitSha(undefined)).toBe("")
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(shortCommitSha(42 as any)).toBe("")
    expect(shortCommitSha("")).toBe("")
  })
})

// ─── Pure helper: sortIterations{Ascending,Descending} ────────────────────

describe("sortIterationsAscending / sortIterationsDescending", () => {
  it("returns a new array and never mutates the input", () => {
    const input = makeSeries(3).reverse()
    const originalOrder = input.map((i) => i.id)
    const sorted = sortIterationsAscending(input)
    expect(input.map((i) => i.id)).toEqual(originalOrder) // unchanged
    expect(sorted).not.toBe(input)
    expect(sorted.map((i) => i.id)).toEqual(["iter-1", "iter-2", "iter-3"])
  })

  it("sortDescending mirrors ascending", () => {
    const input = makeSeries(3)
    expect(sortIterationsDescending(input).map((i) => i.id)).toEqual([
      "iter-3",
      "iter-2",
      "iter-1",
    ])
  })

  it("preserves input order for identical timestamps (stable sort)", () => {
    const ts = "2026-04-18T10:00:00.000Z"
    const input: IterationSnapshot[] = [
      makeSnapshot({ id: "a", createdAt: ts }),
      makeSnapshot({ id: "b", createdAt: ts }),
      makeSnapshot({ id: "c", createdAt: ts }),
    ]
    expect(sortIterationsAscending(input).map((i) => i.id)).toEqual(["a", "b", "c"])
    expect(sortIterationsDescending(input).map((i) => i.id)).toEqual(["a", "b", "c"])
  })

  it("handles empty / single-item arrays", () => {
    expect(sortIterationsAscending([])).toEqual([])
    expect(sortIterationsDescending([])).toEqual([])
    const single = [makeSnapshot()]
    expect(sortIterationsAscending(single)).toEqual(single)
    expect(sortIterationsAscending(single)).not.toBe(single) // still a new array
  })
})

// ─── Pure helper: findIteration ────────────────────────────────────────────

describe("findIteration", () => {
  const series = makeSeries(3)

  it("returns the matching snapshot", () => {
    expect(findIteration(series, "iter-2")?.id).toBe("iter-2")
  })

  it("returns null for null / undefined / empty id", () => {
    expect(findIteration(series, null)).toBe(null)
    expect(findIteration(series, undefined)).toBe(null)
    expect(findIteration(series, "")).toBe(null)
  })

  it("returns null for a missing id", () => {
    expect(findIteration(series, "iter-999")).toBe(null)
  })
})

// ─── Pure helper: computeTimelinePositions ────────────────────────────────

describe("computeTimelinePositions", () => {
  it("returns [] for an empty list", () => {
    expect(computeTimelinePositions([])).toEqual([])
  })

  it("centres a single iteration at 50%", () => {
    expect(computeTimelinePositions(makeSeries(1))).toEqual([50])
  })

  it("spreads N iterations from 0% to 100% evenly", () => {
    expect(computeTimelinePositions(makeSeries(2))).toEqual([0, 100])
    expect(computeTimelinePositions(makeSeries(3))).toEqual([0, 50, 100])
    const five = computeTimelinePositions(makeSeries(5))
    expect(five[0]).toBe(0)
    expect(five[4]).toBe(100)
    expect(five[2]).toBe(50)
  })
})

// ─── Pure helper: resolveActiveId ─────────────────────────────────────────

describe("resolveActiveId", () => {
  const series = makeSeries(3)

  it("returns the candidate when it exists in the list", () => {
    expect(resolveActiveId(series, "iter-2")).toBe("iter-2")
  })

  it("returns null when candidate is missing / empty", () => {
    expect(resolveActiveId(series, null)).toBe(null)
    expect(resolveActiveId(series, undefined)).toBe(null)
    expect(resolveActiveId(series, "")).toBe(null)
    expect(resolveActiveId(series, "iter-999")).toBe(null)
  })
})

// ─── Component: empty state ───────────────────────────────────────────────

describe("<UiIterationTimeline /> empty state", () => {
  it("renders the empty placeholder and hides the axis when no iterations", () => {
    render(<UiIterationTimeline iterations={[]} />)
    expect(screen.getByTestId("ui-iteration-timeline-empty")).toBeInTheDocument()
    expect(screen.queryByTestId("ui-iteration-timeline-axis")).not.toBeInTheDocument()
    expect(screen.queryByTestId("ui-iteration-timeline-detail")).not.toBeInTheDocument()
    expect(screen.getByTestId("ui-iteration-timeline-count")).toHaveTextContent("0")
  })

  it("supports a custom empty message", () => {
    render(
      <UiIterationTimeline
        iterations={[]}
        emptyMessage="Waiting for the first agent iteration…"
      />,
    )
    expect(screen.getByTestId("ui-iteration-timeline-empty")).toHaveTextContent(
      "Waiting for the first agent iteration…",
    )
  })

  it("renders the root even when disabled + empty", () => {
    render(<UiIterationTimeline iterations={[]} disabled />)
    const root = screen.getByTestId("ui-iteration-timeline")
    expect(root).toHaveAttribute("data-disabled", "true")
    expect(root).toHaveAttribute("data-has-iterations", "false")
    expect(root.getAttribute("tabindex")).toBe("-1")
  })
})

// ─── Component: rendering nodes ────────────────────────────────────────────

describe("<UiIterationTimeline /> node rendering", () => {
  it("renders a node per iteration and preserves caller-provided order", () => {
    const iterations = makeSeries(3)
    render(<UiIterationTimeline iterations={iterations} nowProvider={FIXED_NOW} />)
    expect(screen.getByTestId("ui-iteration-timeline-count")).toHaveTextContent("3")
    const nodes = [0, 1, 2].map((i) =>
      screen.getByTestId(`ui-iteration-timeline-node-iter-${i + 1}`),
    )
    nodes.forEach((n, idx) => {
      expect(n).toHaveAttribute("data-index", String(idx))
      expect(n).toHaveAttribute("data-active", "false")
    })
  })

  it("uses defaultIterations when iterations prop is omitted (uncontrolled)", () => {
    const iterations = makeSeries(2)
    render(
      <UiIterationTimeline
        defaultIterations={iterations}
        nowProvider={FIXED_NOW}
      />,
    )
    expect(screen.getByTestId("ui-iteration-timeline-count")).toHaveTextContent("2")
    expect(
      screen.getByTestId("ui-iteration-timeline-node-iter-1"),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId("ui-iteration-timeline-node-iter-2"),
    ).toBeInTheDocument()
  })

  it("labels each node with the relative timestamp", () => {
    const iterations = makeSeries(2)
    render(<UiIterationTimeline iterations={iterations} nowProvider={FIXED_NOW} />)
    // iter-1 is 30m old vs FIXED_NOW, iter-2 is 29m old.
    expect(
      screen.getByTestId("ui-iteration-timeline-node-label-iter-1"),
    ).toHaveTextContent("30m ago")
    expect(
      screen.getByTestId("ui-iteration-timeline-node-label-iter-2"),
    ).toHaveTextContent("29m ago")
  })

  it("does not render the detail panel until a node is selected", () => {
    render(<UiIterationTimeline iterations={makeSeries(2)} nowProvider={FIXED_NOW} />)
    expect(screen.queryByTestId("ui-iteration-timeline-detail")).not.toBeInTheDocument()
  })
})

// ─── Component: selection (uncontrolled) ──────────────────────────────────

describe("<UiIterationTimeline /> selection — uncontrolled", () => {
  it("selects a node on click and expands the detail panel", () => {
    const iterations = makeSeries(3)
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={iterations}
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.click(screen.getByTestId("ui-iteration-timeline-node-iter-2"))
    expect(onActiveChange).toHaveBeenCalledWith("iter-2")
    expect(
      screen.getByTestId("ui-iteration-timeline-node-iter-2"),
    ).toHaveAttribute("data-active", "true")
    const detail = screen.getByTestId("ui-iteration-timeline-detail")
    expect(detail).toHaveAttribute("data-snapshot-id", "iter-2")
    expect(screen.getByTestId("ui-iteration-timeline-detail-index")).toHaveTextContent("2 / 3")
  })

  it("clicking the already-active node clears the selection", () => {
    const iterations = makeSeries(2)
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={iterations}
        defaultActiveId="iter-1"
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.click(screen.getByTestId("ui-iteration-timeline-node-iter-1"))
    expect(onActiveChange).toHaveBeenCalledWith(null)
    expect(screen.queryByTestId("ui-iteration-timeline-detail")).not.toBeInTheDocument()
  })

  it("close button in the header clears the selection", () => {
    render(
      <UiIterationTimeline
        iterations={makeSeries(2)}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.click(screen.getByTestId("ui-iteration-timeline-close-detail"))
    expect(screen.queryByTestId("ui-iteration-timeline-detail")).not.toBeInTheDocument()
  })
})

// ─── Component: selection (controlled) ────────────────────────────────────

describe("<UiIterationTimeline /> selection — controlled", () => {
  it("pins the active id via the controlled prop", () => {
    const iterations = makeSeries(3)
    const { rerender } = render(
      <UiIterationTimeline
        iterations={iterations}
        activeId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.getByTestId("ui-iteration-timeline-node-iter-1"),
    ).toHaveAttribute("data-active", "true")
    rerender(
      <UiIterationTimeline
        iterations={iterations}
        activeId="iter-3"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.getByTestId("ui-iteration-timeline-node-iter-3"),
    ).toHaveAttribute("data-active", "true")
  })

  it("fires onActiveChange without mutating internal state in controlled mode", () => {
    const iterations = makeSeries(2)
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={iterations}
        activeId={null}
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.click(screen.getByTestId("ui-iteration-timeline-node-iter-2"))
    expect(onActiveChange).toHaveBeenCalledWith("iter-2")
    // Not pinned because the controlled caller hasn't swapped activeId.
    expect(
      screen.getByTestId("ui-iteration-timeline-node-iter-2"),
    ).toHaveAttribute("data-active", "false")
  })

  it("clears stale controlled activeId when the snapshot disappears", () => {
    const iterations = makeSeries(3)
    const onActiveChange = vi.fn()
    const { rerender } = render(
      <UiIterationTimeline
        iterations={iterations}
        activeId="iter-3"
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    rerender(
      <UiIterationTimeline
        iterations={iterations.slice(0, 2)}
        activeId="iter-3"
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    expect(onActiveChange).toHaveBeenCalledWith(null)
    expect(screen.queryByTestId("ui-iteration-timeline-detail")).not.toBeInTheDocument()
  })
})

// ─── Component: detail panel content ──────────────────────────────────────

describe("<UiIterationTimeline /> detail panel", () => {
  it("shows screenshot, diff text, SHA, agent, summary and relative timestamp", () => {
    const snapshot = makeSnapshot({
      id: "iter-main",
      commitSha: "1234567890abcdef1234567890abcdef12345678",
      summary: "Agent: tweak hero CTA",
      agentId: "ui-agent-beta",
      createdAt: "2026-04-18T10:00:00.000Z",
    })
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-main"
        nowProvider={FIXED_NOW}
      />,
    )
    const detail = screen.getByTestId("ui-iteration-timeline-detail")
    expect(detail).toHaveAttribute("data-snapshot-id", "iter-main")
    expect(screen.getByTestId("ui-iteration-timeline-detail-index")).toHaveTextContent("1 / 1")
    expect(screen.getByTestId("ui-iteration-timeline-detail-sha")).toHaveTextContent("1234567")
    expect(screen.getByTestId("ui-iteration-timeline-detail-agent")).toHaveTextContent("ui-agent-beta")
    expect(screen.getByTestId("ui-iteration-timeline-detail-summary")).toHaveTextContent("Agent: tweak hero CTA")
    expect(screen.getByTestId("ui-iteration-timeline-detail-timestamp")).toHaveTextContent("30m ago")
    expect(
      screen.getByTestId("ui-iteration-timeline-detail-preview-img"),
    ).toHaveAttribute("src", snapshot.screenshotSrc)
    expect(screen.getByTestId("ui-iteration-timeline-detail-diff-body")).toHaveTextContent("+added line")
  })

  it("falls back to a placeholder when screenshot is missing", () => {
    const snapshot = makeSnapshot({ screenshotSrc: "" })
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.getByTestId("ui-iteration-timeline-detail-preview-empty"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("ui-iteration-timeline-detail-preview-img"),
    ).not.toBeInTheDocument()
  })

  it("falls back to a placeholder when diff is empty", () => {
    const snapshot = makeSnapshot({ diff: "" })
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.getByTestId("ui-iteration-timeline-detail-diff-empty"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("ui-iteration-timeline-detail-diff-body"),
    ).not.toBeInTheDocument()
  })

  it("renders diff stats (+adds / -dels / files) parsed from the diff body", () => {
    const snapshot = makeSnapshot({
      diff: [
        "diff --git a/a b/a",
        "+++ b/a",
        "--- a/a",
        "+one",
        "+two",
        "-gone",
        "diff --git a/b b/b",
        "+++ b/b",
        "--- a/b",
        "+three",
      ].join("\n"),
    })
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    const stats = screen.getByTestId("ui-iteration-timeline-detail-diff-stats")
    expect(stats).toHaveTextContent("+3")
    expect(stats).toHaveTextContent("-1")
    expect(
      screen.getByTestId("ui-iteration-timeline-detail-files-changed"),
    ).toHaveTextContent("2f")
  })

  it("hides the filesChanged pill when diff contains no `diff --git` headers", () => {
    const snapshot = makeSnapshot({ diff: "+tiny\n-change" })
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.queryByTestId("ui-iteration-timeline-detail-files-changed"),
    ).not.toBeInTheDocument()
  })

  it("prefers pre-computed diffStats over re-parsing the diff body", () => {
    const snapshot = makeSnapshot({
      diff: "+a\n+b\n-c",
      diffStats: { additions: 99, deletions: 77, filesChanged: 7 },
    })
    const parseFn = vi.fn(parseDiffStats)
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-1"
        parseDiffStatsImpl={parseFn}
        nowProvider={FIXED_NOW}
      />,
    )
    const stats = screen.getByTestId("ui-iteration-timeline-detail-diff-stats")
    expect(stats).toHaveTextContent("+99")
    expect(stats).toHaveTextContent("-77")
    // Implementation detail — but contract-worthy: the cached stats
    // spare every render the diff-parse cost.
    expect(parseFn).not.toHaveBeenCalled()
  })

  it("omits SHA badge when commitSha is null / empty", () => {
    const snapshot = makeSnapshot({ commitSha: null })
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.queryByTestId("ui-iteration-timeline-detail-sha"),
    ).not.toBeInTheDocument()
  })

  it("omits agent badge when agentId is absent", () => {
    const snapshot = makeSnapshot({ agentId: null })
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.queryByTestId("ui-iteration-timeline-detail-agent"),
    ).not.toBeInTheDocument()
  })
})

// ─── Component: rollback ───────────────────────────────────────────────────

describe("<UiIterationTimeline /> rollback", () => {
  it("fires onRollback with the active snapshot when the button is clicked", () => {
    const snapshot = makeSnapshot({ id: "iter-7" })
    const onRollback = vi.fn()
    render(
      <UiIterationTimeline
        iterations={[snapshot]}
        defaultActiveId="iter-7"
        onRollback={onRollback}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.click(screen.getByTestId("ui-iteration-timeline-rollback"))
    expect(onRollback).toHaveBeenCalledTimes(1)
    expect(onRollback).toHaveBeenCalledWith(snapshot)
  })

  it("hides the rollback button when no onRollback is provided", () => {
    render(
      <UiIterationTimeline
        iterations={makeSeries(1)}
        defaultActiveId="iter-1"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.queryByTestId("ui-iteration-timeline-rollback"),
    ).not.toBeInTheDocument()
  })

  it("does not fire onRollback when disabled", () => {
    const onRollback = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(1)}
        defaultActiveId="iter-1"
        onRollback={onRollback}
        disabled
        nowProvider={FIXED_NOW}
      />,
    )
    const btn = screen.getByTestId("ui-iteration-timeline-rollback")
    // Browser-level disabled also suppresses click — fire it anyway to
    // prove the handler-side guard is belt + braces.
    fireEvent.click(btn)
    expect(onRollback).not.toHaveBeenCalled()
  })

  it("respects a custom rollbackLabel", () => {
    render(
      <UiIterationTimeline
        iterations={makeSeries(1)}
        defaultActiveId="iter-1"
        onRollback={() => {}}
        rollbackLabel="Restore this version"
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.getByTestId("ui-iteration-timeline-rollback"),
    ).toHaveTextContent("Restore this version")
  })
})

// ─── Component: keyboard navigation ───────────────────────────────────────

describe("<UiIterationTimeline /> keyboard navigation", () => {
  it("ArrowRight advances the active node", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(3)}
        defaultActiveId="iter-1"
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "ArrowRight" })
    expect(onActiveChange).toHaveBeenCalledWith("iter-2")
  })

  it("ArrowLeft moves to the previous node (wraps)", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(3)}
        defaultActiveId="iter-1"
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "ArrowLeft" })
    expect(onActiveChange).toHaveBeenCalledWith("iter-3")
  })

  it("lands on the first node when there is no selection yet (ArrowRight)", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(3)}
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "ArrowRight" })
    expect(onActiveChange).toHaveBeenCalledWith("iter-1")
  })

  it("lands on the last node when there is no selection yet (ArrowLeft)", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(3)}
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "ArrowLeft" })
    expect(onActiveChange).toHaveBeenCalledWith("iter-3")
  })

  it("Escape clears the current selection", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(2)}
        defaultActiveId="iter-1"
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "Escape" })
    expect(onActiveChange).toHaveBeenCalledWith(null)
  })

  it("Escape is a no-op when nothing is selected", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(2)}
        onActiveChange={onActiveChange}
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "Escape" })
    expect(onActiveChange).not.toHaveBeenCalled()
  })

  it("keyboard is a no-op when disabled", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(2)}
        defaultActiveId="iter-1"
        onActiveChange={onActiveChange}
        disabled
        nowProvider={FIXED_NOW}
      />,
    )
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "ArrowRight" })
    fireEvent.keyDown(screen.getByTestId("ui-iteration-timeline"), { key: "Escape" })
    expect(onActiveChange).not.toHaveBeenCalled()
  })
})

// ─── Component: disabled ───────────────────────────────────────────────────

describe("<UiIterationTimeline /> disabled", () => {
  it("marks the root and suppresses node click callbacks", () => {
    const onActiveChange = vi.fn()
    render(
      <UiIterationTimeline
        iterations={makeSeries(2)}
        onActiveChange={onActiveChange}
        disabled
        nowProvider={FIXED_NOW}
      />,
    )
    const root = screen.getByTestId("ui-iteration-timeline")
    expect(root).toHaveAttribute("data-disabled", "true")
    expect(root.getAttribute("tabindex")).toBe("-1")
    fireEvent.click(screen.getByTestId("ui-iteration-timeline-node-iter-1"))
    expect(onActiveChange).not.toHaveBeenCalled()
  })
})

// ─── Component: custom formatters / seams ─────────────────────────────────

describe("<UiIterationTimeline /> custom seams", () => {
  it("calls the caller-supplied formatRelativeImpl for node labels + timestamp", () => {
    const customFormat = vi.fn((iso: string) => `ago:${iso.slice(11, 19)}`)
    render(
      <UiIterationTimeline
        iterations={makeSeries(2)}
        defaultActiveId="iter-1"
        formatRelativeImpl={customFormat}
        nowProvider={FIXED_NOW}
      />,
    )
    expect(
      screen.getByTestId("ui-iteration-timeline-node-label-iter-1"),
    ).toHaveTextContent("ago:10:00:00")
    expect(
      screen.getByTestId("ui-iteration-timeline-detail-timestamp"),
    ).toHaveTextContent("ago:10:00:00")
    // node label + detail timestamp + node label for iter-2
    expect(customFormat).toHaveBeenCalled()
  })

  it("calls the caller-supplied parseDiffStatsImpl when no cached diffStats", () => {
    const customParser = vi.fn(() => ({
      additions: 123,
      deletions: 45,
      filesChanged: 6,
    }))
    render(
      <UiIterationTimeline
        iterations={[makeSnapshot({ diff: "+x" })]}
        defaultActiveId="iter-1"
        parseDiffStatsImpl={customParser}
        nowProvider={FIXED_NOW}
      />,
    )
    expect(customParser).toHaveBeenCalled()
    const stats = screen.getByTestId("ui-iteration-timeline-detail-diff-stats")
    expect(stats).toHaveTextContent("+123")
    expect(stats).toHaveTextContent("-45")
  })
})

// ─── Sibling contract — export disjoint-ness ─────────────────────────────

describe("V3 component family — sibling contract", () => {
  it("ui-iteration-timeline does not collide with visual-annotator / element-inspector exports", () => {
    const timelineNames = new Set(Object.keys(TimelineExports))
    const annotatorNames = Object.keys(VisualAnnotatorExports)
    const inspectorNames = Object.keys(ElementInspectorExports)
    const overlap = [...annotatorNames, ...inspectorNames].filter((n) =>
      timelineNames.has(n),
    )
    // `default` is a shared convention — every module exports a default
    // component — we only care about *named* collisions.
    expect(overlap.filter((n) => n !== "default")).toEqual([])
  })
})

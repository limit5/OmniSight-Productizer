/**
 * V8 #3 (TODO row 2704 / issue #324) — Contract tests for
 * `test-coverage-viewer.tsx`.
 *
 * Test buckets (mirroring V7 #4 / V7 #5 / V8 #2 sibling shape):
 *
 *   1. Pure helpers — classification, format, ratio, metric build,
 *      empty-summary helper, aggregate, sort, filter, uncovered line
 *      extraction, range squash, path shortener.
 *   2. LCOV parser — single-file / multi-file / empty / fall-back to
 *      DA-rolled-up LF/LH when explicit LF/LH missing.
 *   3. Istanbul parser — pre-summary path (Vitest v8) + raw counter map
 *      path (Istanbul JSON) + null/garbage input.
 *   4. Rendering — empty state / metric badges / file row colour / file
 *      selection / search filter / uncovered line highlighting in the
 *      source view / controlled-mode pass-through.
 */

import { describe, expect, it, vi } from "vitest"
import { fireEvent, render, screen } from "@testing-library/react"

import {
  COVERAGE_CLASS_LABELS,
  COVERAGE_METRIC_LABELS,
  DEFAULT_COVERAGE_THRESHOLDS,
  TestCoverageViewer,
  aggregateCoverageSummary,
  buildCoverageMetric,
  classifyCoverage,
  coverageClassColorVar,
  coverageClassLabel,
  emptyCoverageSummary,
  filterFilesByQuery,
  formatCoverageRatio,
  formatCoveragePct,
  parseIstanbul,
  parseLcov,
  shortenCoveragePath,
  sortFilesByCoverage,
  uncoveredLineNumbers,
  uncoveredRanges,
  type CoverageFile,
  type CoverageReport,
} from "@/components/omnisight/test-coverage-viewer"

// ─── Test helpers ──────────────────────────────────────────────────────────

function makeFile(
  path: string,
  linePct: number,
  overrides: Partial<CoverageFile> = {},
): CoverageFile {
  const total = 100
  const covered = Math.round((linePct / 100) * total)
  return {
    path,
    summary: {
      lines: buildCoverageMetric(covered, total),
      branches: buildCoverageMetric(covered, total),
      functions: buildCoverageMetric(covered, total),
      statements: buildCoverageMetric(covered, total),
    },
    lines: [],
    ...overrides,
  }
}

function makeReport(files: CoverageFile[]): CoverageReport {
  return {
    summary: aggregateCoverageSummary(files),
    files,
    source: "test",
    generatedAt: "2026-04-25T00:00:00Z",
  }
}

// ─── Pure helpers ──────────────────────────────────────────────────────────

describe("classifyCoverage", () => {
  it("buckets by default thresholds (high≥80, medium≥50, low<50)", () => {
    expect(classifyCoverage(100)).toBe("high")
    expect(classifyCoverage(80)).toBe("high")
    expect(classifyCoverage(79)).toBe("medium")
    expect(classifyCoverage(50)).toBe("medium")
    expect(classifyCoverage(49)).toBe("low")
    expect(classifyCoverage(0)).toBe("low")
  })
  it("returns unknown for null / NaN / Infinity", () => {
    expect(classifyCoverage(null)).toBe("unknown")
    expect(classifyCoverage(Number.NaN)).toBe("unknown")
    expect(classifyCoverage(Number.POSITIVE_INFINITY)).toBe("unknown")
  })
  it("honours custom thresholds", () => {
    expect(classifyCoverage(70, { high: 90, medium: 60 })).toBe("medium")
    expect(classifyCoverage(95, { high: 90, medium: 60 })).toBe("high")
    expect(classifyCoverage(55, { high: 90, medium: 60 })).toBe("low")
  })
})

describe("coverageClassColorVar", () => {
  it("emerald / amber / rose / muted for the four classes", () => {
    expect(coverageClassColorVar("high")).toBe("var(--validation-emerald)")
    expect(coverageClassColorVar("medium")).toBe("var(--warning-amber)")
    expect(coverageClassColorVar("low")).toBe("var(--critical-red)")
    expect(coverageClassColorVar("unknown")).toBe("var(--muted-foreground)")
  })
})

describe("coverageClassLabel", () => {
  it("matches the COVERAGE_CLASS_LABELS table", () => {
    expect(coverageClassLabel("high")).toBe(COVERAGE_CLASS_LABELS.high)
    expect(coverageClassLabel("medium")).toBe(COVERAGE_CLASS_LABELS.medium)
    expect(coverageClassLabel("low")).toBe(COVERAGE_CLASS_LABELS.low)
    expect(coverageClassLabel("unknown")).toBe(COVERAGE_CLASS_LABELS.unknown)
  })
})

describe("formatCoveragePct", () => {
  it("formats with one decimal and a percent suffix", () => {
    expect(formatCoveragePct(0)).toBe("0.0%")
    expect(formatCoveragePct(75.34)).toBe("75.3%")
    expect(formatCoveragePct(100)).toBe("100.0%")
  })
  it("returns dash for null / NaN / Infinity / undefined", () => {
    expect(formatCoveragePct(null)).toBe("—")
    expect(formatCoveragePct(undefined)).toBe("—")
    expect(formatCoveragePct(Number.NaN)).toBe("—")
    expect(formatCoveragePct(Number.POSITIVE_INFINITY)).toBe("—")
  })
})

describe("formatCoverageRatio", () => {
  it("renders covered / total", () => {
    expect(formatCoverageRatio({ covered: 12, total: 34, pct: 35.3 })).toBe(
      "12 / 34",
    )
  })
})

describe("buildCoverageMetric", () => {
  it("computes pct from covered/total", () => {
    expect(buildCoverageMetric(50, 100)).toEqual({
      covered: 50,
      total: 100,
      pct: 50,
    })
  })
  it("clamps pct into [0, 100]", () => {
    // We don't expect callers to pass covered > total but if they do
    // the helper still bounds the value.
    const m = buildCoverageMetric(120, 100)
    expect(m.pct).toBe(100)
  })
  it("returns null pct when total is 0", () => {
    expect(buildCoverageMetric(0, 0)).toEqual({
      covered: 0,
      total: 0,
      pct: null,
    })
  })
})

describe("emptyCoverageSummary", () => {
  it("zeroes every metric", () => {
    const s = emptyCoverageSummary()
    for (const k of ["lines", "branches", "functions", "statements"] as const) {
      expect(s[k].covered).toBe(0)
      expect(s[k].total).toBe(0)
      expect(s[k].pct).toBeNull()
    }
  })
})

describe("aggregateCoverageSummary", () => {
  it("sums covered/total across files and recomputes pct", () => {
    const files = [
      makeFile("a.ts", 60),
      makeFile("b.ts", 80),
    ]
    const s = aggregateCoverageSummary(files)
    expect(s.lines.covered).toBe(60 + 80)
    expect(s.lines.total).toBe(200)
    expect(s.lines.pct).toBeCloseTo(70, 5)
  })
  it("returns the empty summary for an empty file list", () => {
    expect(aggregateCoverageSummary([])).toEqual(emptyCoverageSummary())
  })
})

describe("sortFilesByCoverage", () => {
  it("ascending: worst pct first", () => {
    const files = [makeFile("hi", 90), makeFile("lo", 10), makeFile("md", 50)]
    const sorted = sortFilesByCoverage(files, "asc")
    expect(sorted.map((f) => f.path)).toEqual(["lo", "md", "hi"])
  })
  it("descending: best pct first", () => {
    const files = [makeFile("hi", 90), makeFile("lo", 10), makeFile("md", 50)]
    const sorted = sortFilesByCoverage(files, "desc")
    expect(sorted.map((f) => f.path)).toEqual(["hi", "md", "lo"])
  })
  it("null pct (empty modules) sort to the end regardless of order", () => {
    const empty = makeFile("empty.ts", 0, {
      summary: {
        ...emptyCoverageSummary(),
      },
    })
    const files = [empty, makeFile("a.ts", 50)]
    const asc = sortFilesByCoverage(files, "asc")
    expect(asc[asc.length - 1].path).toBe("empty.ts")
    const desc = sortFilesByCoverage(files, "desc")
    expect(desc[desc.length - 1].path).toBe("empty.ts")
  })
  it("breaks ties by path", () => {
    const files = [makeFile("b.ts", 50), makeFile("a.ts", 50)]
    expect(sortFilesByCoverage(files, "asc").map((f) => f.path)).toEqual([
      "a.ts",
      "b.ts",
    ])
  })
})

describe("filterFilesByQuery", () => {
  it("matches by path substring (case-insensitive)", () => {
    const files = [makeFile("backend/auth.py", 50), makeFile("frontend/x.ts", 90)]
    expect(filterFilesByQuery(files, "AUTH").map((f) => f.path)).toEqual([
      "backend/auth.py",
    ])
  })
  it("returns the full list (cloned) when query is blank", () => {
    const files = [makeFile("a", 50)]
    const filtered = filterFilesByQuery(files, "   ")
    expect(filtered).toEqual(files)
    // Must be a new array — the host might mutate it.
    expect(filtered).not.toBe(files)
  })
})

describe("uncoveredLineNumbers", () => {
  it("returns line numbers with hits === 0", () => {
    const file = makeFile("x", 50, {
      lines: [
        { line: 1, hits: 5 },
        { line: 2, hits: 0 },
        { line: 3, hits: null },
        { line: 4, hits: 0 },
      ],
    })
    expect(uncoveredLineNumbers(file)).toEqual([2, 4])
  })
  it("returns [] when the file has no per-line data", () => {
    expect(uncoveredLineNumbers(makeFile("x", 50))).toEqual([])
  })
})

describe("uncoveredRanges", () => {
  it("squashes contiguous uncovered runs into [start, end] pairs", () => {
    const file = makeFile("x", 50, {
      lines: [
        { line: 1, hits: 1 },
        { line: 2, hits: 0 },
        { line: 3, hits: 0 },
        { line: 4, hits: 0 },
        { line: 5, hits: 1 },
        { line: 9, hits: 0 },
        { line: 10, hits: 0 },
        { line: 12, hits: 0 },
      ],
    })
    expect(uncoveredRanges(file)).toEqual([
      { start: 2, end: 4 },
      { start: 9, end: 10 },
      { start: 12, end: 12 },
    ])
  })
  it("returns [] when nothing is uncovered", () => {
    expect(uncoveredRanges(makeFile("x", 50))).toEqual([])
  })
})

describe("shortenCoveragePath", () => {
  it("keeps the last two segments when the path is deep", () => {
    expect(shortenCoveragePath("src/a/b/c/file.ts")).toBe("…/c/file.ts")
  })
  it("returns the input when there are at most two segments", () => {
    expect(shortenCoveragePath("a/file.ts")).toBe("a/file.ts")
    expect(shortenCoveragePath("file.ts")).toBe("file.ts")
  })
})

// ─── LCOV parser ───────────────────────────────────────────────────────────

describe("parseLcov", () => {
  it("parses a single record with explicit LH/LF + DA lines", () => {
    const text = [
      "TN:",
      "SF:src/auth.ts",
      "DA:1,5",
      "DA:2,0",
      "DA:3,3",
      "LH:2",
      "LF:3",
      "BRH:1",
      "BRF:2",
      "FNH:0",
      "FNF:1",
      "end_of_record",
    ].join("\n")
    const r = parseLcov(text)
    expect(r.files).toHaveLength(1)
    const f = r.files[0]
    expect(f.path).toBe("src/auth.ts")
    expect(f.summary.lines.covered).toBe(2)
    expect(f.summary.lines.total).toBe(3)
    expect(f.summary.lines.pct).toBeCloseTo(66.66, 1)
    expect(f.summary.branches.pct).toBe(50)
    expect(f.summary.functions.pct).toBe(0)
    // Per-line hits captured.
    expect(f.lines).toHaveLength(3)
    expect(f.lines[1]).toEqual({ line: 2, hits: 0, source: null })
  })
  it("falls back to DA-rolled-up counts when LH/LF are absent", () => {
    const text = [
      "SF:no-summary.ts",
      "DA:1,1",
      "DA:2,0",
      "DA:3,2",
      "end_of_record",
    ].join("\n")
    const r = parseLcov(text)
    expect(r.files[0].summary.lines.covered).toBe(2)
    expect(r.files[0].summary.lines.total).toBe(3)
  })
  it("parses multiple records", () => {
    const text = [
      "SF:a.ts",
      "DA:1,1",
      "LH:1",
      "LF:1",
      "end_of_record",
      "SF:b.ts",
      "DA:1,0",
      "DA:2,0",
      "LH:0",
      "LF:2",
      "end_of_record",
    ].join("\n")
    const r = parseLcov(text)
    expect(r.files.map((f) => f.path)).toEqual(["a.ts", "b.ts"])
    expect(r.summary.lines.covered).toBe(1)
    expect(r.summary.lines.total).toBe(3)
  })
  it("returns an empty report for empty / malformed input", () => {
    expect(parseLcov("").files).toEqual([])
    expect(parseLcov("garbage\nnonsense\n").files).toEqual([])
  })
  it("stamps source = lcov on the report", () => {
    expect(parseLcov("SF:x.ts\nend_of_record\n").source).toBe("lcov")
  })
})

// ─── Istanbul / Vitest v8 parser ───────────────────────────────────────────

describe("parseIstanbul", () => {
  it("uses pre-computed summary when present (Vitest v8 shape)", () => {
    const json = {
      "src/a.ts": {
        path: "src/a.ts",
        summary: {
          lines: { covered: 8, total: 10, pct: 80 },
          branches: { covered: 4, total: 5, pct: 80 },
          functions: { covered: 2, total: 3, pct: 66.7 },
          statements: { covered: 8, total: 10, pct: 80 },
        },
      },
    }
    const r = parseIstanbul(json)
    expect(r.files).toHaveLength(1)
    expect(r.files[0].summary.lines).toEqual({
      covered: 8,
      total: 10,
      pct: 80,
    })
    expect(r.source).toBe("istanbul")
  })
  it("derives counts from raw maps (Istanbul shape)", () => {
    const json = {
      "src/b.ts": {
        path: "src/b.ts",
        s: { "0": 1, "1": 0, "2": 3 },
        f: { "0": 1, "1": 0 },
        b: { "0": [1, 0] },
        statementMap: {
          "0": { start: { line: 1 }, end: { line: 1 } },
          "1": { start: { line: 2 }, end: { line: 2 } },
          "2": { start: { line: 3 }, end: { line: 3 } },
        },
      },
    }
    const r = parseIstanbul(json)
    expect(r.files[0].summary.statements).toEqual({
      covered: 2,
      total: 3,
      pct: (2 / 3) * 100,
    })
    expect(r.files[0].summary.functions.covered).toBe(1)
    expect(r.files[0].summary.functions.total).toBe(2)
    expect(r.files[0].summary.branches.covered).toBe(1)
    expect(r.files[0].summary.branches.total).toBe(2)
    // Per-line hits derived.
    expect(r.files[0].lines).toEqual([
      { line: 1, hits: 1, source: null },
      { line: 2, hits: 0, source: null },
      { line: 3, hits: 3, source: null },
    ])
  })
  it("falls back to the JSON key when path is omitted", () => {
    const r = parseIstanbul({
      "fallback/path.ts": { summary: { lines: { covered: 0, total: 0 } } },
    } as Record<string, unknown>)
    expect(r.files[0].path).toBe("fallback/path.ts")
  })
  it("returns the empty report for null / non-object input", () => {
    expect(parseIstanbul(null).files).toEqual([])
    expect(parseIstanbul(undefined).files).toEqual([])
  })
})

// ─── Rendering ─────────────────────────────────────────────────────────────

describe("<TestCoverageViewer /> — rendering contracts", () => {
  it("renders the empty state when the report is null", () => {
    render(<TestCoverageViewer report={null} />)
    expect(
      screen.getByTestId("test-coverage-viewer-files-empty"),
    ).toBeInTheDocument()
    expect(
      screen
        .getByTestId("test-coverage-viewer")
        .getAttribute("data-file-count"),
    ).toBe("0")
  })

  it("renders the four metric badges with covered/total/pct", () => {
    const report = makeReport([
      makeFile("a.ts", 80),
      makeFile("b.ts", 60),
    ])
    render(<TestCoverageViewer report={report} />)
    for (const k of ["lines", "branches", "functions", "statements"] as const) {
      expect(
        screen.getByTestId(`test-coverage-viewer-metric-${k}-pct`).textContent,
      ).toBe("70.0%")
      expect(
        screen.getByTestId(`test-coverage-viewer-metric-${k}-ratio`)
          .textContent,
      ).toBe("140 / 200")
    }
    // Header label visible for at least one metric.
    expect(screen.getAllByText(COVERAGE_METRIC_LABELS.lines).length).toBeGreaterThan(0)
  })

  it("rollup gate is passed when line pct is at or above the medium threshold", () => {
    const report = makeReport([makeFile("a.ts", 60)])
    render(<TestCoverageViewer report={report} />)
    expect(
      screen
        .getByTestId("test-coverage-viewer")
        .getAttribute("data-rollup-gate"),
    ).toBe("passed")
  })

  it("rollup gate is failed when line pct is below the medium threshold", () => {
    const report = makeReport([makeFile("a.ts", 30)])
    render(<TestCoverageViewer report={report} />)
    expect(
      screen
        .getByTestId("test-coverage-viewer")
        .getAttribute("data-rollup-gate"),
    ).toBe("failed")
  })

  it("renders one file row per file in ascending coverage order", () => {
    const report = makeReport([
      makeFile("hi.ts", 90),
      makeFile("lo.ts", 10),
      makeFile("md.ts", 50),
    ])
    render(<TestCoverageViewer report={report} />)
    const rows = [
      screen.getByTestId("test-coverage-viewer-file-lo.ts"),
      screen.getByTestId("test-coverage-viewer-file-md.ts"),
      screen.getByTestId("test-coverage-viewer-file-hi.ts"),
    ]
    // Sanity: order on the rendered grid matches ascending pct.
    const list = screen.getByTestId("test-coverage-viewer-files")
    const orderedTestIds = Array.from(
      list.querySelectorAll("[data-testid^='test-coverage-viewer-file-']"),
    )
      .map((el) => el.getAttribute("data-testid"))
      .filter(
        (t): t is string =>
          !!t &&
          /^test-coverage-viewer-file-[^-]+\.ts$/.test(t),
      )
    expect(orderedTestIds[0]).toBe("test-coverage-viewer-file-lo.ts")
    expect(orderedTestIds[orderedTestIds.length - 1]).toBe(
      "test-coverage-viewer-file-hi.ts",
    )
    for (const r of rows) expect(r).toBeInTheDocument()
  })

  it("file row data-coverage-class reflects the threshold bucket", () => {
    const report = makeReport([
      makeFile("a.ts", 90),
      makeFile("b.ts", 60),
      makeFile("c.ts", 20),
    ])
    render(<TestCoverageViewer report={report} />)
    expect(
      screen
        .getByTestId("test-coverage-viewer-file-a.ts")
        .getAttribute("data-coverage-class"),
    ).toBe("high")
    expect(
      screen
        .getByTestId("test-coverage-viewer-file-b.ts")
        .getAttribute("data-coverage-class"),
    ).toBe("medium")
    expect(
      screen
        .getByTestId("test-coverage-viewer-file-c.ts")
        .getAttribute("data-coverage-class"),
    ).toBe("low")
  })

  it("clicking a file row fires onSelectFile + selects it (uncontrolled)", () => {
    const onSelect = vi.fn()
    const report = makeReport([
      makeFile("a.ts", 90),
      makeFile("b.ts", 30),
    ])
    render(<TestCoverageViewer report={report} onSelectFile={onSelect} />)
    fireEvent.click(screen.getByTestId("test-coverage-viewer-file-a.ts"))
    expect(onSelect).toHaveBeenCalledWith("a.ts")
    expect(
      screen
        .getByTestId("test-coverage-viewer-file-a.ts")
        .getAttribute("data-selected"),
    ).toBe("true")
  })

  it("respects the controlled selectedFilePath prop", () => {
    const report = makeReport([
      makeFile("a.ts", 90, {
        lines: [{ line: 1, hits: 1 }],
      }),
      makeFile("b.ts", 30, {
        lines: [{ line: 1, hits: 0 }, { line: 2, hits: 1 }],
      }),
    ])
    render(<TestCoverageViewer report={report} selectedFilePath="b.ts" />)
    expect(
      screen
        .getByTestId("test-coverage-viewer-file-b.ts")
        .getAttribute("data-selected"),
    ).toBe("true")
    expect(
      screen.getByTestId("test-coverage-viewer-source").getAttribute("data-file"),
    ).toBe("b.ts")
  })

  it("source view highlights uncovered lines + reports the count", () => {
    const file = makeFile("hot.ts", 50, {
      lines: [
        { line: 1, hits: 5 },
        { line: 2, hits: 0 },
        { line: 3, hits: 0 },
        { line: 4, hits: 1 },
      ],
    })
    const report = makeReport([file])
    render(<TestCoverageViewer report={report} selectedFilePath="hot.ts" />)
    expect(
      screen
        .getByTestId("test-coverage-viewer-source")
        .getAttribute("data-uncovered-count"),
    ).toBe("2")
    expect(
      screen
        .getByTestId("test-coverage-viewer-line-2")
        .getAttribute("data-uncovered"),
    ).toBe("true")
    expect(
      screen
        .getByTestId("test-coverage-viewer-line-1")
        .getAttribute("data-uncovered"),
    ).toBe("false")
    // Range strip surfaces the contiguous block.
    expect(
      screen.getByTestId("test-coverage-viewer-source-ranges").textContent,
    ).toContain("L2–L3")
  })

  it("source view shows an empty state when no per-line data is attached", () => {
    const report = makeReport([makeFile("nodata.ts", 50)])
    render(<TestCoverageViewer report={report} selectedFilePath="nodata.ts" />)
    expect(
      screen.getByTestId("test-coverage-viewer-no-source"),
    ).toBeInTheDocument()
  })

  it("source view shows the placeholder when nothing is selected", () => {
    const report = makeReport([
      makeFile("a.ts", 90, { lines: [{ line: 1, hits: 1 }] }),
    ])
    render(<TestCoverageViewer report={report} />)
    expect(
      screen.getByTestId("test-coverage-viewer-empty"),
    ).toBeInTheDocument()
  })

  it("search filter narrows the file list", () => {
    const report = makeReport([
      makeFile("backend/auth.py", 50),
      makeFile("frontend/file.ts", 90),
    ])
    render(<TestCoverageViewer report={report} />)
    const search = screen.getByTestId(
      "test-coverage-viewer-search",
    ) as HTMLInputElement
    fireEvent.change(search, { target: { value: "auth" } })
    expect(
      screen.getByTestId("test-coverage-viewer-file-backend/auth.py"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("test-coverage-viewer-file-frontend/file.ts"),
    ).toBeNull()
    expect(
      screen
        .getByTestId("test-coverage-viewer")
        .getAttribute("data-visible-count"),
    ).toBe("1")
  })

  it("controlled search query is forwarded via onSearchChange", () => {
    const onChange = vi.fn()
    const report = makeReport([makeFile("x.ts", 50)])
    render(
      <TestCoverageViewer
        report={report}
        searchQuery="x"
        onSearchChange={onChange}
      />,
    )
    const input = screen.getByTestId(
      "test-coverage-viewer-search",
    ) as HTMLInputElement
    expect(input.value).toBe("x")
    fireEvent.change(input, { target: { value: "xy" } })
    expect(onChange).toHaveBeenCalledWith("xy")
    // Controlled mode: viewer does not own the input state, so the
    // displayed value still reflects the prop.
    expect(input.value).toBe("x")
  })

  it("data attributes expose rollup class and file count for instrumentation", () => {
    const report = makeReport([makeFile("a.ts", 30)])
    render(<TestCoverageViewer report={report} />)
    const root = screen.getByTestId("test-coverage-viewer")
    expect(root.getAttribute("data-rollup-class")).toBe("low")
    expect(root.getAttribute("data-file-count")).toBe("1")
  })

  it("renders the source badge + generated-at when present on the report", () => {
    const report = makeReport([makeFile("a.ts", 50)])
    render(<TestCoverageViewer report={report} />)
    expect(
      screen.getByTestId("test-coverage-viewer-source-badge").textContent,
    ).toBe("test")
    expect(
      screen.getByTestId("test-coverage-viewer-generated-at").textContent,
    ).toContain("2026-04-25")
  })

  it("respects custom thresholds", () => {
    const report = makeReport([makeFile("a.ts", 70)])
    render(
      <TestCoverageViewer
        report={report}
        thresholds={{ high: 95, medium: 80 }}
      />,
    )
    // 70% is under 80% medium → low.
    expect(
      screen
        .getByTestId("test-coverage-viewer")
        .getAttribute("data-rollup-class"),
    ).toBe("low")
  })
})

// ─── Drift guards ──────────────────────────────────────────────────────────

describe("DEFAULT_COVERAGE_THRESHOLDS", () => {
  it("locks the conventional 80/50 pair", () => {
    expect(DEFAULT_COVERAGE_THRESHOLDS).toEqual({ high: 80, medium: 50 })
  })
})

describe("COVERAGE_CLASS_LABELS", () => {
  it("covers every CoverageClass variant with a non-empty label", () => {
    for (const k of ["high", "medium", "low", "unknown"] as const) {
      expect(COVERAGE_CLASS_LABELS[k].length).toBeGreaterThan(0)
    }
  })
})

describe("COVERAGE_METRIC_LABELS", () => {
  it("covers the four metric keys", () => {
    for (const k of ["lines", "branches", "functions", "statements"] as const) {
      expect(COVERAGE_METRIC_LABELS[k].length).toBeGreaterThan(0)
    }
  })
})

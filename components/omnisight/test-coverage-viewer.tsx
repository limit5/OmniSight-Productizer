/**
 * V8 #3 (TODO row 2704 / issue #324) — Test coverage viewer.
 *
 * The third and final row of V8 "Software Workspace UI".  Where V8 #1
 * (`app/workspace/software/page.tsx`) is the workspace shell and V8 #2
 * (`components/omnisight/software-release-dashboard.tsx`) is the
 * platform-grid release surface, this row is the at-a-glance coverage
 * report for the software workspace:
 *
 *   ┌────────────────────────────┬──────────────────────────────────────┐
 *   │ summary header             │   per-file table on the left,        │
 *   │ - lines / branches / funcs │   collapsible source view on the     │
 *   │ - covered / total / pct    │   right with uncovered-line          │
 *   │ - threshold colour gate    │   highlighting (red gutter + tint).  │
 *   └────────────────────────────┴──────────────────────────────────────┘
 *
 * Three operator-visible surfaces:
 *
 *   1. Summary header — top strip with line / branch / function /
 *      statement metrics.  Each metric is `covered / total — pct%` plus
 *      a colour-coded badge (`high` ≥ 80% emerald, `medium` ≥ 50% amber,
 *      `low` < 50% rose).  Status `passed` (rollup ≥ medium threshold)
 *      / `failed` (rollup < medium threshold) sits at the far right.
 *   2. File list — left pane lists every `CoverageFile` with a coverage
 *      bar (line %) + numerical pct + tiny mini-bar showing the
 *      branch/function ratio.  Sorted by ascending line % by default
 *      (worst on top so the operator sees the gaps first).  Search
 *      input filters the list.
 *   3. Source view — right pane shows the source of the selected file
 *      with one line per row.  Uncovered lines render with a rose
 *      background tint + a `×` glyph in the gutter.  Hit count badge
 *      next to every line shows execution count.  Lines with no
 *      execution data (inline blank lines / comments outside the
 *      coverage map) render neutral.
 *
 * Schema-agnostic — the host page parses LCOV / Vitest v8 / Istanbul
 * JSON into the shared `CoverageReport` shape this viewer renders.  Two
 * pure parsers ship out-of-the-box (`parseLcov`, `parseIstanbul`); both
 * are exported for unit tests + ad-hoc CLI use.  No SSE wiring — the
 * report is a static artifact pushed by the host.
 *
 * Module-global state audit (SOP Step 1):
 *   N/A — pure React client component.  All state is `useState` per
 *   instance; the threshold catalogue (`DEFAULT_COVERAGE_THRESHOLDS`) is
 *   a frozen module-level constant — every worker derives the same
 *   value from the same source module (SOP Step 1 qualifying answer
 *   #1).
 *
 * Read-after-write audit:
 *   N/A — no async / DB / pool / lock interaction.  Pure helpers + pure
 *   render.
 *
 * Intentional non-goals:
 *   - The viewer does NOT trigger a coverage run; the host page wires
 *     the `Run coverage` action to its own backend.
 *   - The viewer does NOT bundle a syntax highlighter.  Source lines
 *     render as monospace plain text.  A future row can swap in
 *     `shiki` / `highlight.js` if requested — current scope keeps the
 *     bundle small.
 *   - The viewer does NOT diff coverage against a baseline.  That is a
 *     follow-up row.
 */
"use client"

import * as React from "react"
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  FileCode2,
  Filter,
  Search,
  XCircle,
} from "lucide-react"

import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"

// ─── Public shapes ─────────────────────────────────────────────────────────

/** Coverage class — drives the colour-coding across the viewer. */
export type CoverageClass = "high" | "medium" | "low" | "unknown"

/** Per-metric covered/total/pct triple. */
export interface CoverageMetric {
  covered: number
  total: number
  /** Pct in `[0, 100]` — `null` when `total === 0` (no measurable units). */
  pct: number | null
}

/**
 * Per-file (or whole-report) summary.  Mirrors the `lines` / `branches`
 * / `functions` / `statements` shape both Istanbul JSON and Vitest v8
 * emit.
 */
export interface CoverageSummary {
  lines: CoverageMetric
  branches: CoverageMetric
  functions: CoverageMetric
  statements: CoverageMetric
}

/**
 * Per-line execution data.  `hits` is the execution count from the
 * coverage tool (LCOV `DA:line,hits` or Istanbul `s` map); `null` when
 * the line is not in the coverage map (blank / comment / outside scope).
 */
export interface CoverageLine {
  line: number
  hits: number | null
  /** Source text for the line — populated when the host attaches it. */
  source?: string | null
}

export interface CoverageFile {
  /** Repo-relative path — the stable id for selection. */
  path: string
  /** Per-file summary — drives the file row's bar + pct. */
  summary: CoverageSummary
  /**
   * Per-line execution data.  Empty array means the host hasn't attached
   * line-level data yet (the file row still renders, source view shows a
   * `No source attached` empty state).
   */
  lines: readonly CoverageLine[]
}

export interface CoverageReport {
  /** Aggregate summary across every file in `files`. */
  summary: CoverageSummary
  files: readonly CoverageFile[]
  /** ISO-8601 timestamp the report was produced — optional. */
  generatedAt?: string | null
  /** Human label — `"vitest run --coverage"`, `"pytest --cov"`, etc. */
  source?: string | null
}

/** Coverage thresholds — pct values that drive the colour gate. */
export interface CoverageThresholds {
  /** Pct ≥ this is `high` (emerald). */
  high: number
  /** Pct ≥ this (and < `high`) is `medium` (amber); below is `low`. */
  medium: number
}

// ─── Public constants ──────────────────────────────────────────────────────

/**
 * Default coverage thresholds — matches the conventional 80/50 split
 * Istanbul / Vitest emit out-of-the-box.  Frozen so a future regression
 * (someone bumping `medium` to 70 in one place + forgetting another) is
 * visible at the call site.
 */
export const DEFAULT_COVERAGE_THRESHOLDS: Readonly<CoverageThresholds> =
  Object.freeze({
    high: 80,
    medium: 50,
  })

/** Per-class human label. */
export const COVERAGE_CLASS_LABELS: Readonly<Record<CoverageClass, string>> =
  Object.freeze({
    high: "Healthy",
    medium: "Watch",
    low: "Risk",
    unknown: "—",
  })

/** Per-metric human label. */
export const COVERAGE_METRIC_LABELS: Readonly<
  Record<keyof CoverageSummary, string>
> = Object.freeze({
  lines: "Lines",
  branches: "Branches",
  functions: "Functions",
  statements: "Statements",
})

// ─── Pure helpers (exported for tests) ─────────────────────────────────────

/**
 * Bucket a pct into one of four classes given the active thresholds.
 * `null` pct (zero measurable units) returns `unknown` so the host can
 * still render a row but not penalise empty modules.
 */
export function classifyCoverage(
  pct: number | null,
  thresholds: CoverageThresholds = DEFAULT_COVERAGE_THRESHOLDS,
): CoverageClass {
  if (pct == null || !Number.isFinite(pct)) return "unknown"
  if (pct >= thresholds.high) return "high"
  if (pct >= thresholds.medium) return "medium"
  return "low"
}

/** Class → CSS colour variable. */
export function coverageClassColorVar(klass: CoverageClass): string {
  switch (klass) {
    case "high":
      return "var(--validation-emerald)"
    case "medium":
      return "var(--warning-amber)"
    case "low":
      return "var(--critical-red)"
    case "unknown":
    default:
      return "var(--muted-foreground)"
  }
}

/** Class → human label.  Thin wrapper around the constant. */
export function coverageClassLabel(klass: CoverageClass): string {
  return COVERAGE_CLASS_LABELS[klass]
}

/**
 * Format a pct as `"75.3%"`; `null` / non-finite degrades to `"—"`.
 * Stable output so tests can pin the exact string.
 */
export function formatCoveragePct(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return "—"
  return `${pct.toFixed(1)}%`
}

/** Format `covered / total` as a dense "12 / 34". */
export function formatCoverageRatio(metric: CoverageMetric): string {
  return `${metric.covered} / ${metric.total}`
}

/** Build a `CoverageMetric` from raw counts. */
export function buildCoverageMetric(
  covered: number,
  total: number,
): CoverageMetric {
  if (total <= 0) return { covered: 0, total: 0, pct: null }
  const pct = Math.min(100, Math.max(0, (covered / total) * 100))
  return { covered, total, pct }
}

/** Empty-summary helper — every metric zeroed. */
export function emptyCoverageSummary(): CoverageSummary {
  return {
    lines: { covered: 0, total: 0, pct: null },
    branches: { covered: 0, total: 0, pct: null },
    functions: { covered: 0, total: 0, pct: null },
    statements: { covered: 0, total: 0, pct: null },
  }
}

/**
 * Aggregate per-file summaries into a single report-level summary by
 * summing covered/total across every file and recomputing pct.  Pure
 * + idempotent so the host can re-aggregate after editing a file row.
 */
export function aggregateCoverageSummary(
  files: readonly CoverageFile[],
): CoverageSummary {
  let lc = 0,
    lt = 0,
    bc = 0,
    bt = 0,
    fc = 0,
    ft = 0,
    sc = 0,
    st = 0
  for (const f of files) {
    lc += f.summary.lines.covered
    lt += f.summary.lines.total
    bc += f.summary.branches.covered
    bt += f.summary.branches.total
    fc += f.summary.functions.covered
    ft += f.summary.functions.total
    sc += f.summary.statements.covered
    st += f.summary.statements.total
  }
  return {
    lines: buildCoverageMetric(lc, lt),
    branches: buildCoverageMetric(bc, bt),
    functions: buildCoverageMetric(fc, ft),
    statements: buildCoverageMetric(sc, st),
  }
}

/**
 * Sort file rows.  Default is ascending line pct so the worst files
 * float to the top — operator gets the gaps first.  Files with `null`
 * line pct (empty modules) sort last regardless of order.
 */
export function sortFilesByCoverage(
  files: readonly CoverageFile[],
  order: "asc" | "desc" = "asc",
): CoverageFile[] {
  const arr = [...files]
  arr.sort((a, b) => {
    const pa = a.summary.lines.pct
    const pb = b.summary.lines.pct
    if (pa == null && pb == null) return a.path.localeCompare(b.path)
    if (pa == null) return 1
    if (pb == null) return -1
    if (pa === pb) return a.path.localeCompare(b.path)
    return order === "asc" ? pa - pb : pb - pa
  })
  return arr
}

/** Filter file rows by a free-text query (case-insensitive on `path`). */
export function filterFilesByQuery(
  files: readonly CoverageFile[],
  query: string,
): CoverageFile[] {
  const q = query.trim().toLowerCase()
  if (!q) return [...files]
  return files.filter((f) => f.path.toLowerCase().includes(q))
}

/**
 * Extract uncovered line numbers from a `CoverageFile` — every line
 * whose `hits === 0`.  `null` hits (no coverage data) are excluded;
 * those rows render neutrally in the source view.
 */
export function uncoveredLineNumbers(file: CoverageFile): number[] {
  const out: number[] = []
  for (const ln of file.lines) {
    if (ln.hits === 0) out.push(ln.line)
  }
  return out
}

/**
 * Squash a sorted line-number list into contiguous `[start, end]`
 * ranges.  Useful for compact display ("L12-L18, L42") and for tests
 * that want to assert "the gap is one block, not three scattered
 * lines".
 */
export function uncoveredRanges(
  file: CoverageFile,
): Array<{ start: number; end: number }> {
  const lines = uncoveredLineNumbers(file).slice().sort((a, b) => a - b)
  if (lines.length === 0) return []
  const ranges: Array<{ start: number; end: number }> = []
  let start = lines[0]
  let end = lines[0]
  for (let i = 1; i < lines.length; i++) {
    const ln = lines[i]
    if (ln === end + 1) {
      end = ln
    } else {
      ranges.push({ start, end })
      start = ln
      end = ln
    }
  }
  ranges.push({ start, end })
  return ranges
}

/** Short relative path for display — keeps the last two segments only. */
export function shortenCoveragePath(path: string): string {
  const parts = path.split("/").filter(Boolean)
  if (parts.length <= 2) return path
  return `…/${parts.slice(-2).join("/")}`
}

// ─── LCOV parser ───────────────────────────────────────────────────────────

/**
 * Parse an LCOV `info` text into a `CoverageReport`.  The LCOV format
 * is line-based; relevant directives:
 *
 *   - `SF:<path>`             — source file (begins a record)
 *   - `DA:<line>,<hits>`      — per-line hit
 *   - `LH:<count>` `LF:<count>` — line covered / total
 *   - `BRH:<count>` `BRF:<count>` — branch covered / total
 *   - `FNH:<count>` `FNF:<count>` — function covered / total
 *   - `end_of_record`         — closes the file
 *
 * LCOV does not carry per-statement counts; we mirror line counts into
 * `statements` so the summary header has a non-empty value.  Source
 * text is NOT included in LCOV — the host attaches it separately.
 */
export function parseLcov(lcovText: string): CoverageReport {
  const lines = (lcovText ?? "").split(/\r?\n/)
  const files: CoverageFile[] = []
  let currentPath: string | null = null
  let currentLines: CoverageLine[] = []
  let lh = 0,
    lf = 0,
    brh = 0,
    brf = 0,
    fnh = 0,
    fnf = 0
  for (const raw of lines) {
    const line = raw.trim()
    if (!line) continue
    if (line.startsWith("SF:")) {
      currentPath = line.slice(3).trim()
      currentLines = []
      lh = 0
      lf = 0
      brh = 0
      brf = 0
      fnh = 0
      fnf = 0
    } else if (line.startsWith("DA:") && currentPath) {
      const body = line.slice(3)
      const [lnStr, hitsStr] = body.split(",")
      const ln = Number(lnStr)
      const hits = Number(hitsStr)
      if (Number.isFinite(ln) && Number.isFinite(hits)) {
        currentLines.push({ line: ln, hits, source: null })
      }
    } else if (line.startsWith("LH:")) lh = Number(line.slice(3)) || 0
    else if (line.startsWith("LF:")) lf = Number(line.slice(3)) || 0
    else if (line.startsWith("BRH:")) brh = Number(line.slice(4)) || 0
    else if (line.startsWith("BRF:")) brf = Number(line.slice(4)) || 0
    else if (line.startsWith("FNH:")) fnh = Number(line.slice(4)) || 0
    else if (line.startsWith("FNF:")) fnf = Number(line.slice(4)) || 0
    else if (line === "end_of_record" && currentPath) {
      // Fall back to per-line DA: roll-up if explicit LH/LF absent.
      const lhEff = lf > 0 ? lh : currentLines.filter((l) => (l.hits ?? 0) > 0).length
      const lfEff = lf > 0 ? lf : currentLines.length
      const summary: CoverageSummary = {
        lines: buildCoverageMetric(lhEff, lfEff),
        branches: buildCoverageMetric(brh, brf),
        functions: buildCoverageMetric(fnh, fnf),
        // LCOV doesn't carry statements — mirror lines.
        statements: buildCoverageMetric(lhEff, lfEff),
      }
      files.push({
        path: currentPath,
        summary,
        lines: currentLines,
      })
      currentPath = null
      currentLines = []
    }
  }
  return {
    summary: aggregateCoverageSummary(files),
    files,
    source: "lcov",
  }
}

// ─── Istanbul / Vitest v8 parser ───────────────────────────────────────────

/**
 * Istanbul / Vitest v8 `coverage-final.json` shape we accept.  Both
 * tools emit the same root-level structure (`{ [path]: FileCoverage }`)
 * so a single parser handles both.  We accept the loose superset:
 * either the `"summary"` key directly or the per-statement / per-branch
 * / per-function maps to derive a summary from.
 */
type IstanbulCounts = Record<string | number, number>

interface IstanbulFile {
  path?: string
  s?: IstanbulCounts // statement → hits
  b?: Record<string | number, number[]>
  f?: IstanbulCounts // function → hits
  statementMap?: Record<
    string | number,
    {
      start?: { line?: number; column?: number } | null
      end?: { line?: number; column?: number } | null
    }
  >
  // Optional pre-computed summary — Vitest v8 includes it.
  summary?: {
    lines?: CoverageMetric
    branches?: CoverageMetric
    functions?: CoverageMetric
    statements?: CoverageMetric
  }
}

/**
 * Parse an Istanbul-shaped JSON object into a `CoverageReport`.  Robust
 * to either pre-computed `summary` (Vitest v8) or the raw counter maps
 * (Istanbul JSON).  Source text is NOT in Istanbul JSON — the host
 * attaches it separately if it wants to.
 */
export function parseIstanbul(
  json: Record<string, unknown> | null | undefined,
): CoverageReport {
  if (!json || typeof json !== "object") {
    return { summary: emptyCoverageSummary(), files: [] }
  }
  const files: CoverageFile[] = []
  for (const [key, raw] of Object.entries(json)) {
    if (!raw || typeof raw !== "object") continue
    const f = raw as IstanbulFile
    const path = typeof f.path === "string" && f.path ? f.path : key
    let summary = emptyCoverageSummary()
    if (f.summary) {
      summary = {
        lines: coerceMetric(f.summary.lines),
        branches: coerceMetric(f.summary.branches),
        functions: coerceMetric(f.summary.functions),
        statements: coerceMetric(f.summary.statements),
      }
    } else {
      summary = deriveIstanbulSummary(f)
    }
    const fileLines = deriveIstanbulLines(f)
    files.push({
      path,
      summary,
      lines: fileLines,
    })
  }
  return {
    summary: aggregateCoverageSummary(files),
    files,
    source: "istanbul",
  }
}

function coerceMetric(m: CoverageMetric | undefined): CoverageMetric {
  if (!m || typeof m !== "object") return { covered: 0, total: 0, pct: null }
  const covered = typeof m.covered === "number" ? m.covered : 0
  const total = typeof m.total === "number" ? m.total : 0
  if (total <= 0) return { covered: 0, total: 0, pct: null }
  const pct =
    typeof m.pct === "number" && Number.isFinite(m.pct)
      ? m.pct
      : (covered / total) * 100
  return { covered, total, pct: Math.min(100, Math.max(0, pct)) }
}

function deriveIstanbulSummary(f: IstanbulFile): CoverageSummary {
  let sCov = 0,
    sTot = 0
  if (f.s) {
    for (const v of Object.values(f.s)) {
      sTot++
      if (Number(v) > 0) sCov++
    }
  }
  let fCov = 0,
    fTot = 0
  if (f.f) {
    for (const v of Object.values(f.f)) {
      fTot++
      if (Number(v) > 0) fCov++
    }
  }
  let bCov = 0,
    bTot = 0
  if (f.b) {
    for (const arr of Object.values(f.b)) {
      if (!Array.isArray(arr)) continue
      for (const v of arr) {
        bTot++
        if (Number(v) > 0) bCov++
      }
    }
  }
  // Lines = unique source lines from `statementMap` (best-effort).
  const lineHits = new Map<number, number>()
  if (f.statementMap && f.s) {
    for (const [id, loc] of Object.entries(f.statementMap)) {
      const ln = loc?.start?.line
      if (typeof ln !== "number") continue
      const hits = Number(f.s[id] ?? 0)
      const prev = lineHits.get(ln) ?? 0
      lineHits.set(ln, prev + hits)
    }
  }
  let lCov = 0,
    lTot = 0
  for (const hits of lineHits.values()) {
    lTot++
    if (hits > 0) lCov++
  }
  return {
    lines: buildCoverageMetric(lCov, lTot),
    branches: buildCoverageMetric(bCov, bTot),
    functions: buildCoverageMetric(fCov, fTot),
    statements: buildCoverageMetric(sCov, sTot),
  }
}

function deriveIstanbulLines(f: IstanbulFile): CoverageLine[] {
  const lineHits = new Map<number, number>()
  if (f.statementMap && f.s) {
    for (const [id, loc] of Object.entries(f.statementMap)) {
      const ln = loc?.start?.line
      if (typeof ln !== "number") continue
      const hits = Number(f.s[id] ?? 0)
      const prev = lineHits.get(ln)
      // A line is "covered" if any statement on it has hits > 0.
      lineHits.set(ln, Math.max(prev ?? 0, hits))
    }
  }
  const out: CoverageLine[] = []
  for (const [line, hits] of [...lineHits.entries()].sort((a, b) => a[0] - b[0])) {
    out.push({ line, hits, source: null })
  }
  return out
}

// ─── Sub-components ────────────────────────────────────────────────────────

interface CoverageMetricBadgeProps {
  metric: CoverageMetric
  label: string
  thresholds: CoverageThresholds
  testId: string
}

function CoverageMetricBadge({
  metric,
  label,
  thresholds,
  testId,
}: CoverageMetricBadgeProps) {
  const klass = classifyCoverage(metric.pct, thresholds)
  return (
    <div
      data-testid={testId}
      data-metric-class={klass}
      className="flex flex-col gap-0.5 rounded-md border border-border/50 bg-background/40 px-2 py-1.5 text-xs"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-muted-foreground">
          {label}
        </span>
        <Badge
          variant="outline"
          className="h-4 gap-1 px-1 text-[10px]"
          style={{ color: coverageClassColorVar(klass) }}
        >
          {coverageClassLabel(klass)}
        </Badge>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span
          data-testid={`${testId}-pct`}
          className="font-mono text-base font-semibold"
          style={{ color: coverageClassColorVar(klass) }}
        >
          {formatCoveragePct(metric.pct)}
        </span>
        <span
          data-testid={`${testId}-ratio`}
          className="font-mono text-[10px] text-muted-foreground"
        >
          {formatCoverageRatio(metric)}
        </span>
      </div>
    </div>
  )
}

interface CoveragePctBarProps {
  pct: number | null
  thresholds: CoverageThresholds
  testId?: string
}

function CoveragePctBar({ pct, thresholds, testId }: CoveragePctBarProps) {
  const klass = classifyCoverage(pct, thresholds)
  const filled = pct == null ? 0 : Math.min(100, Math.max(0, pct))
  return (
    <div
      data-testid={testId}
      data-coverage-class={klass}
      className="relative h-1.5 w-full overflow-hidden rounded-full bg-muted/40"
    >
      <div
        className="absolute inset-y-0 left-0 transition-all"
        style={{
          width: `${filled}%`,
          backgroundColor: coverageClassColorVar(klass),
        }}
      />
    </div>
  )
}

interface CoverageFileRowProps {
  file: CoverageFile
  isSelected: boolean
  onSelect: (path: string) => void
  thresholds: CoverageThresholds
  testId: string
}

function CoverageFileRow({
  file,
  isSelected,
  onSelect,
  thresholds,
  testId,
}: CoverageFileRowProps) {
  const linePct = file.summary.lines.pct
  const klass = classifyCoverage(linePct, thresholds)
  const uncovered = uncoveredLineNumbers(file).length
  return (
    <li>
      <button
        type="button"
        data-testid={`${testId}-file-${file.path}`}
        data-coverage-class={klass}
        data-selected={isSelected ? "true" : "false"}
        onClick={() => onSelect(file.path)}
        className={cn(
          "flex w-full flex-col gap-1 rounded-md border px-2 py-1.5 text-left text-xs transition-colors",
          isSelected
            ? "border-sky-500/60 bg-sky-500/10"
            : "border-border/50 hover:bg-muted/30",
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-1.5">
            <FileCode2
              className="size-3.5 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <span
              data-testid={`${testId}-file-${file.path}-path`}
              className="truncate font-mono text-[11px] text-foreground"
              title={file.path}
            >
              {shortenCoveragePath(file.path)}
            </span>
          </span>
          <span
            data-testid={`${testId}-file-${file.path}-pct`}
            className="font-mono text-[11px] font-semibold"
            style={{ color: coverageClassColorVar(klass) }}
          >
            {formatCoveragePct(linePct)}
          </span>
        </div>
        <CoveragePctBar
          pct={linePct}
          thresholds={thresholds}
          testId={`${testId}-file-${file.path}-bar`}
        />
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
          <span data-testid={`${testId}-file-${file.path}-ratio`}>
            {formatCoverageRatio(file.summary.lines)} lines
          </span>
          <span>·</span>
          <span data-testid={`${testId}-file-${file.path}-uncovered`}>
            {uncovered} uncovered
          </span>
        </div>
      </button>
    </li>
  )
}

interface CoverageSourceViewProps {
  file: CoverageFile | null
  testId: string
}

function CoverageSourceView({ file, testId }: CoverageSourceViewProps) {
  if (!file) {
    return (
      <div
        data-testid={`${testId}-empty`}
        className="flex h-full min-h-32 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border/50 p-4 text-xs text-muted-foreground"
      >
        <ChevronRight className="size-4" aria-hidden="true" />
        Select a file to inspect uncovered lines.
      </div>
    )
  }
  if (file.lines.length === 0) {
    return (
      <div
        data-testid={`${testId}-no-source`}
        className="flex h-full min-h-32 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-border/50 p-4 text-xs text-muted-foreground"
      >
        <Filter className="size-4" aria-hidden="true" />
        No per-line coverage attached for{" "}
        <code className="font-mono text-[11px]">{file.path}</code>.
      </div>
    )
  }
  const ranges = uncoveredRanges(file)
  return (
    <div
      data-testid={`${testId}-source`}
      data-file={file.path}
      data-uncovered-count={uncoveredLineNumbers(file).length}
      className="flex h-full min-h-0 flex-col rounded-md border border-border/50 bg-background/40"
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-border/50 px-2 py-1.5 text-[11px]">
        <FileCode2 className="size-3.5" aria-hidden="true" />
        <code
          data-testid={`${testId}-source-path`}
          className="truncate font-mono text-[11px] text-foreground"
        >
          {file.path}
        </code>
        {ranges.length > 0 && (
          <span
            data-testid={`${testId}-source-ranges`}
            className="ml-auto flex items-center gap-1 text-[10px] text-rose-300"
          >
            <AlertTriangle className="size-3" aria-hidden="true" />
            {ranges
              .map((r) => (r.start === r.end ? `L${r.start}` : `L${r.start}–L${r.end}`))
              .slice(0, 6)
              .join(", ")}
            {ranges.length > 6 && ` +${ranges.length - 6} more`}
          </span>
        )}
      </div>
      <ol
        data-testid={`${testId}-source-lines`}
        className="overflow-auto py-1 font-mono text-[11px] leading-tight"
      >
        {file.lines.map((ln) => {
          const uncovered = ln.hits === 0
          const noData = ln.hits == null
          return (
            <li
              key={ln.line}
              data-testid={`${testId}-line-${ln.line}`}
              data-line={ln.line}
              data-uncovered={uncovered ? "true" : "false"}
              data-no-data={noData ? "true" : "false"}
              className={cn(
                "grid grid-cols-[40px_56px_1fr] gap-1.5 px-2 py-0.5",
                uncovered && "bg-rose-500/15",
                noData && "opacity-60",
              )}
            >
              <span className="select-none text-right text-muted-foreground">
                {ln.line}
              </span>
              <span
                data-testid={`${testId}-line-${ln.line}-hits`}
                className={cn(
                  "select-none text-right font-mono text-[10px]",
                  uncovered
                    ? "text-rose-300"
                    : ln.hits != null
                      ? "text-emerald-300"
                      : "text-muted-foreground",
                )}
              >
                {uncovered ? "× 0" : ln.hits != null ? `× ${ln.hits}` : "—"}
              </span>
              <span
                className={cn(
                  "whitespace-pre text-foreground",
                  uncovered && "text-rose-100",
                )}
              >
                {ln.source ?? ""}
              </span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

// ─── Main viewer ───────────────────────────────────────────────────────────

export interface TestCoverageViewerProps {
  /** Coverage report to render — typically produced by `parseLcov` /
   *  `parseIstanbul` in the host page. */
  report: CoverageReport | null
  /** Selected file path — controlled mode.  When omitted, the viewer
   *  manages its own selection. */
  selectedFilePath?: string | null
  /** Fired when the operator selects a file row. */
  onSelectFile?: (path: string) => void
  /** Initial search query — uncontrolled mode bootstrap. */
  initialSearchQuery?: string
  /** Controlled search query — when set, the viewer becomes a pure
   *  render surface and does not own the input state. */
  searchQuery?: string
  /** Fired when the operator types into the search box. */
  onSearchChange?: (query: string) => void
  /** Sort order for the file list.  Default is ascending line pct. */
  sortOrder?: "asc" | "desc"
  /** Coverage thresholds. */
  thresholds?: CoverageThresholds
  /** `data-testid` root (defaults to `test-coverage-viewer`). */
  testId?: string
}

/**
 * `TestCoverageViewer` — the full panel.  See module-level docstring
 * for the contract.
 */
export function TestCoverageViewer(props: TestCoverageViewerProps) {
  const {
    report,
    selectedFilePath = null,
    onSelectFile,
    initialSearchQuery = "",
    searchQuery: controlledQuery,
    onSearchChange,
    sortOrder = "asc",
    thresholds = DEFAULT_COVERAGE_THRESHOLDS,
    testId = "test-coverage-viewer",
  } = props

  const [internalQuery, setInternalQuery] = React.useState(initialSearchQuery)
  const query = controlledQuery ?? internalQuery
  const handleQuery = (q: string) => {
    if (controlledQuery == null) setInternalQuery(q)
    onSearchChange?.(q)
  }

  const [internalSelected, setInternalSelected] = React.useState<string | null>(
    selectedFilePath,
  )
  const selectedPath = selectedFilePath ?? internalSelected
  const handleSelect = (path: string) => {
    if (selectedFilePath == null) setInternalSelected(path)
    onSelectFile?.(path)
  }

  const files = report?.files ?? []

  const visibleFiles = React.useMemo(() => {
    const filtered = filterFilesByQuery(files, query)
    return sortFilesByCoverage(filtered, sortOrder)
  }, [files, query, sortOrder])

  const selectedFile = React.useMemo(
    () => files.find((f) => f.path === selectedPath) ?? null,
    [files, selectedPath],
  )

  const summary = report?.summary ?? emptyCoverageSummary()
  const rollupClass = classifyCoverage(summary.lines.pct, thresholds)
  const rollupGate: "passed" | "failed" | "unknown" =
    rollupClass === "unknown"
      ? "unknown"
      : rollupClass === "low"
        ? "failed"
        : "passed"

  return (
    <section
      data-testid={testId}
      data-rollup-class={rollupClass}
      data-rollup-gate={rollupGate}
      data-file-count={files.length}
      data-visible-count={visibleFiles.length}
      className="flex min-h-0 flex-col gap-2 rounded-md border border-border bg-background/60 p-2"
    >
      {/* ─── Summary header ─────────────────────────────────────────────── */}
      <header
        data-testid={`${testId}-header`}
        className="flex flex-col gap-2"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            data-testid={`${testId}-rollup-badge`}
            variant="outline"
            className="h-5 gap-1 px-1.5 text-[11px]"
            style={{ color: coverageClassColorVar(rollupClass) }}
          >
            {rollupGate === "passed" ? (
              <CheckCircle2 className="size-3.5" aria-hidden="true" />
            ) : rollupGate === "failed" ? (
              <XCircle className="size-3.5" aria-hidden="true" />
            ) : (
              <Filter className="size-3.5" aria-hidden="true" />
            )}
            {coverageClassLabel(rollupClass)} ·{" "}
            {formatCoveragePct(summary.lines.pct)}
          </Badge>
          {report?.source && (
            <Badge
              data-testid={`${testId}-source-badge`}
              variant="secondary"
              className="h-5 px-1.5 text-[11px] font-mono"
            >
              {report.source}
            </Badge>
          )}
          {report?.generatedAt && (
            <span
              data-testid={`${testId}-generated-at`}
              className="font-mono text-[10px] text-muted-foreground"
            >
              {report.generatedAt}
            </span>
          )}
          <span
            data-testid={`${testId}-file-count`}
            className="ml-auto font-mono text-[10px] text-muted-foreground"
          >
            {files.length} files
          </span>
        </div>
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
          <CoverageMetricBadge
            metric={summary.lines}
            label={COVERAGE_METRIC_LABELS.lines}
            thresholds={thresholds}
            testId={`${testId}-metric-lines`}
          />
          <CoverageMetricBadge
            metric={summary.branches}
            label={COVERAGE_METRIC_LABELS.branches}
            thresholds={thresholds}
            testId={`${testId}-metric-branches`}
          />
          <CoverageMetricBadge
            metric={summary.functions}
            label={COVERAGE_METRIC_LABELS.functions}
            thresholds={thresholds}
            testId={`${testId}-metric-functions`}
          />
          <CoverageMetricBadge
            metric={summary.statements}
            label={COVERAGE_METRIC_LABELS.statements}
            thresholds={thresholds}
            testId={`${testId}-metric-statements`}
          />
        </div>
      </header>

      <Separator />

      {/* ─── File list + source view ────────────────────────────────────── */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div className="flex min-h-0 flex-col gap-1.5">
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              data-testid={`${testId}-search`}
              type="search"
              placeholder="Filter files…"
              value={query}
              onChange={(e) => handleQuery(e.target.value)}
              className="h-7 pl-7 text-xs"
            />
          </div>
          {visibleFiles.length === 0 ? (
            <div
              data-testid={`${testId}-files-empty`}
              className="flex flex-col items-center justify-center gap-1 rounded-md border border-dashed border-border/50 p-4 text-xs text-muted-foreground"
            >
              {files.length === 0
                ? "No coverage report attached."
                : "No files match the filter."}
            </div>
          ) : (
            <ul
              data-testid={`${testId}-files`}
              className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto pr-1"
            >
              {visibleFiles.map((file) => (
                <CoverageFileRow
                  key={file.path}
                  file={file}
                  isSelected={file.path === selectedPath}
                  onSelect={handleSelect}
                  thresholds={thresholds}
                  testId={testId}
                />
              ))}
            </ul>
          )}
        </div>
        <CoverageSourceView file={selectedFile} testId={testId} />
      </div>
    </section>
  )
}

export default TestCoverageViewer

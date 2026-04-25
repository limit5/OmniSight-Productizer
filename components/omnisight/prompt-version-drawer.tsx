"use client"

// ZZ.C1 (#305-1, 2026-04-24) checkbox 3 — PromptVersionDrawer.
// ZZ.C1 (#305-1, 2026-04-24) checkbox 4 — jump-to-next-hunk (``n`` key)
// + unfold context (default 3 lines) layered on top.
//
// Opens from the ORCHESTRATOR AI panel ("System Prompt Versions" button
// under the LLM MODEL section). Shows a per-agent timeline of prompt
// snapshots already captured by checkbox 1 + 2 (``GET /runtime/prompts``
// returns a deduped-by-content_hash list ordered newest-first), and lets
// the operator pick any two rows to render a side-by-side diff.
//
// Diff strategy: the list endpoint already returns ``content`` (full body)
// per row, so we run ``Diff.diffLines(oldBody, newBody)`` from the ``diff``
// npm lib client-side rather than hitting ``GET /runtime/prompts/diff``.
// One round-trip per agent_type instead of two; the diff endpoint stays
// available for clients that don't carry the bodies, but the drawer skips
// it. Removed lines render with red background on the LEFT column,
// added lines with green background on the RIGHT column. Context lines
// render in both columns with neutral background. This is the literal
// "新增行綠 / 刪除行紅" requirement from the TODO row spec.
//
// Hunk navigation + unfold context (ZZ.C1 checkbox 4):
//   - The flat ``DiffRow[]`` from ``changesToDiffRows`` is grouped into
//     ``DiffSegment[]`` by ``groupRowsIntoSegments`` — each hunk (run of
//     removed/added rows) becomes one segment, and runs of context rows
//     that exceed ``2 × contextLines`` collapse behind an "Expand N
//     lines" button matching git / GitHub / GitLab's 3-line-context
//     convention. ``contextLines`` defaults to 3 to line up with the
//     backend ``difflib.unified_diff(n=3)`` call in
//     ``backend/routers/system.py::get_prompt_diff``.
//   - ``n`` advances ``activeHunkIndex`` to the next hunk (wraps) and
//     calls ``scrollIntoView`` on that hunk's anchor row.
//     ``Shift+n`` / ``N`` steps backward. The binding only fires when
//     the drawer is open, a diff is rendered, and focus is not inside
//     a form control (select / input / textarea) so the agent
//     dropdown's type-ahead still works.
//
// Subtype list: spec calls for 12 subtypes ("orchestrator / firmware /
// software / etc."). The list is exposed as ``DEFAULT_PROMPT_AGENT_SUBTYPES``
// so app integrators can override. Default mapping derives slugs from
// ``backend/agents/prompts/<slug>.md`` — checkbox 2's
// ``_snapshot_path_for(agent_type, sub_type)`` writes
// ``firmware__bsp.md`` / ``firmware__isp.md`` etc., so the slugs below
// match the on-disk filenames the snapshot capture produces.

import { useState, useEffect, useMemo, useCallback, useRef } from "react"
import { createPortal } from "react-dom"
import {
  X,
  History,
  GitCompare,
  RefreshCw,
  ChevronRight,
  AlertCircle,
  ChevronsUpDown,
  ArrowDown,
  ArrowUp,
} from "lucide-react"
import { diffLines, type Change } from "diff"
import {
  fetchPromptVersions,
  type PromptVersionEntry,
  type PromptVersionsListResponse,
} from "@/lib/api"

/** ZZ.C1 checkbox 4: default number of context lines kept visible around
 *  each hunk. Matches ``difflib.unified_diff(n=3)`` the backend uses in
 *  ``backend/routers/system.py::get_prompt_diff`` and the git /
 *  GitHub / GitLab convention, so the drawer and the server-rendered
 *  diff agree on how much context an unfold click reveals. */
export const DEFAULT_DIFF_CONTEXT_LINES = 3

/** ZZ.C1 checkbox 3: dropdown options for the agent_type selector. The
 *  spec calls for "12 subtype" — orchestrator + 4 firmware (general,
 *  bsp, isp, hal) + 4 software (general, algorithm, ai-deploy, middleware)
 *  + 3 validator (general, sdet, security) = 12. The slug column matches
 *  ``backend/agents/prompts/<slug>.md`` — i.e. the keys ``register_active``
 *  + the snapshot capture path use, so a row freshly snapshot-captured for
 *  ``firmware__bsp`` will appear in the timeline when the operator picks
 *  "Firmware – BSP". */
export interface PromptAgentSubtype {
  value: string
  label: string
  group?: string
}

export const DEFAULT_PROMPT_AGENT_SUBTYPES: PromptAgentSubtype[] = [
  { value: "orchestrator", label: "Orchestrator", group: "Core" },
  { value: "firmware", label: "Firmware (general)", group: "Firmware" },
  { value: "firmware__bsp", label: "Firmware – BSP", group: "Firmware" },
  { value: "firmware__isp", label: "Firmware – ISP/3A", group: "Firmware" },
  { value: "firmware__hal", label: "Firmware – HAL", group: "Firmware" },
  { value: "software", label: "Software (general)", group: "Software" },
  { value: "software__algorithm", label: "Software – Algorithm", group: "Software" },
  { value: "software__ai-deploy", label: "Software – AI Deploy", group: "Software" },
  { value: "software__middleware", label: "Software – Middleware", group: "Software" },
  { value: "validator", label: "Validator (general)", group: "Validator" },
  { value: "validator__sdet", label: "Validator – SDET", group: "Validator" },
  { value: "validator__security", label: "Validator – Security", group: "Validator" },
]

export interface PromptVersionDrawerProps {
  open: boolean
  onClose: () => void
  /** ZZ.C1 checkbox 3: agent slug pre-selected when the drawer opens. */
  defaultAgentType?: string
  /** Override the dropdown options (tests / multi-tenant). */
  subtypes?: PromptAgentSubtype[]
  /** Test seam: lets the unit test drive the drawer with a fake fetcher
   *  instead of mocking the @/lib/api module. */
  fetchVersions?: (
    agentType: string,
    limit?: number,
  ) => Promise<PromptVersionsListResponse>
  /** Test seam: lets the unit test override how the side-by-side rows
   *  are computed. Exposed so the diff-rendering edge cases in
   *  prompt-version-drawer.test.tsx can drive the rendering branch
   *  independently of the ``diff`` lib. */
  diffLinesFn?: (a: string, b: string) => Change[]
  /** ZZ.C1 checkbox 4: how many context lines to keep visible around
   *  each hunk before collapsing the middle. Defaults to 3 to match
   *  ``difflib.unified_diff(n=3)`` server-side and the git convention. */
  contextLines?: number
}

interface DiffRow {
  /** ``"context"`` = same in both / ``"removed"`` = only in old / ``"added"`` = only in new */
  kind: "context" | "removed" | "added"
  /** Original (left) column content; null on a pure-added row. */
  left: string | null
  /** New (right) column content; null on a pure-removed row. */
  right: string | null
}

/** ZZ.C1 checkbox 4: one piece of the rendered diff after the
 *  ``groupRowsIntoSegments`` pass. A ``hunk`` is a contiguous run of
 *  changed rows (removed / added — what ``n`` jumps between). A
 *  ``context-visible`` segment renders its rows inline. A
 *  ``context-collapsed`` segment is hidden behind an "Expand N lines"
 *  button and only renders its rows after the operator clicks to
 *  unfold. */
export type DiffSegment =
  | { type: "hunk"; hunkIndex: number; rows: DiffRow[]; id: string }
  | { type: "context-visible"; rows: DiffRow[]; id: string }
  | { type: "context-collapsed"; rows: DiffRow[]; id: string }

function shortHash(hash: string): string {
  return hash ? hash.slice(0, 8) : "—"
}

function formatTimestamp(iso: string): string {
  if (!iso) return "—"
  // The backend emits ``YYYY-MM-DDTHH:MM:SSZ``. Show ``MM-DD HH:MM`` to
  // keep the timeline rows compact; full ISO is in the title attr.
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso)
  if (!m) return iso
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`
}

/**
 * Convert ``diff.diffLines`` output into a flat list of rows for the
 * side-by-side renderer. The ``diff`` lib emits "change" objects with a
 * single value containing N lines; we explode each change into
 * one-row-per-line so the operator can scan removals / additions
 * line-aligned. Removed and added blocks are zipped side-by-side when
 * adjacent (a typical "edit" — N lines deleted + M lines added) so
 * matching positions render on the same row; otherwise extras spill onto
 * their own column with the opposite side blank.
 *
 * Edge cases enumerated by the row spec ("empty / identical / 完全重寫")
 * all collapse here: identical bodies → all rows ``kind="context"``;
 * empty/empty → zero rows; total rewrite → all ``removed`` then all
 * ``added`` with no context rows.
 */
export function changesToDiffRows(changes: Change[]): DiffRow[] {
  const rows: DiffRow[] = []
  function splitLines(value: string): string[] {
    if (!value) return []
    // ``diffLines`` keeps trailing newlines on each chunk. Strip the
    // final empty after split so a value of "a\nb\n" yields ["a","b"]
    // not ["a","b",""].
    const parts = value.split("\n")
    if (parts.length > 0 && parts[parts.length - 1] === "") parts.pop()
    return parts
  }

  for (let i = 0; i < changes.length; i++) {
    const ch = changes[i]
    if (!ch.added && !ch.removed) {
      for (const line of splitLines(ch.value)) {
        rows.push({ kind: "context", left: line, right: line })
      }
      continue
    }
    if (ch.removed) {
      const removedLines = splitLines(ch.value)
      // Adjacent ``added`` chunk → zip side-by-side as edit rows.
      const next = changes[i + 1]
      if (next && next.added) {
        const addedLines = splitLines(next.value)
        const zipLen = Math.max(removedLines.length, addedLines.length)
        for (let k = 0; k < zipLen; k++) {
          const l = removedLines[k] ?? null
          const r = addedLines[k] ?? null
          if (l !== null && r !== null) {
            // Both sides present → render two rows so the colour
            // separation stays clean (one red on left, one green on
            // right) at the same vertical position.
            rows.push({ kind: "removed", left: l, right: null })
            rows.push({ kind: "added", left: null, right: r })
          } else if (l !== null) {
            rows.push({ kind: "removed", left: l, right: null })
          } else if (r !== null) {
            rows.push({ kind: "added", left: null, right: r })
          }
        }
        i += 1 // consumed the ``added`` chunk
        continue
      }
      // Pure deletion (no following addition).
      for (const line of removedLines) {
        rows.push({ kind: "removed", left: line, right: null })
      }
      continue
    }
    // Pure addition (no preceding removal).
    for (const line of splitLines(ch.value)) {
      rows.push({ kind: "added", left: null, right: line })
    }
  }
  return rows
}

/**
 * ZZ.C1 checkbox 4: partition the flat ``DiffRow[]`` into a linear list
 * of "segments" for the side-by-side renderer. A segment is either a
 * **hunk** (a contiguous run of removed/added rows — what git calls a
 * "hunk" in unified-diff terminology) or a **context band** (a
 * contiguous run of unchanged rows). Context bands longer than
 * ``2 × contextLines`` get split into a visible head, a collapsed
 * middle, and a visible tail, matching the git / GitHub / GitLab
 * convention of showing a few lines of context around each hunk and
 * hiding the rest behind an "Expand" button.
 *
 * Edge cases:
 *  - ``rows=[]`` → ``[]`` (nothing to render)
 *  - Whole diff is context (no changed rows) → single visible segment
 *    if len ≤ contextLines, else visible head + collapsed tail.
 *  - Two hunks with < ``2 × contextLines`` of context between them →
 *    context stays fully visible (no collapse)
 *  - Leading context before the first hunk → collapsed head +
 *    visible tail (only the contextLines rows immediately before the
 *    hunk stay visible)
 *  - Trailing context after the last hunk → visible head +
 *    collapsed tail
 *
 * Segment ids are derived from the row index where the underlying run
 * starts. The drawer's ``expandedContextIds`` Set keys off these ids,
 * and the ids stay stable across re-renders of the same diff but
 * naturally reset when the operator picks a new pair (the row list
 * changes and the ``useMemo`` tied to the selected pair drops the
 * stale expand state).
 */
export function groupRowsIntoSegments(
  rows: DiffRow[],
  contextLines: number = DEFAULT_DIFF_CONTEXT_LINES,
): DiffSegment[] {
  if (rows.length === 0) return []
  const segments: DiffSegment[] = []

  interface Run {
    changed: boolean
    start: number
    rows: DiffRow[]
  }
  const runs: Run[] = []
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i]
    const changed = r.kind !== "context"
    const last = runs[runs.length - 1]
    if (last && last.changed === changed) {
      last.rows.push(r)
    } else {
      runs.push({ changed, start: i, rows: [r] })
    }
  }

  const n = Math.max(0, Math.floor(contextLines))
  let hunkCounter = 0
  for (let r = 0; r < runs.length; r++) {
    const run = runs[r]
    if (run.changed) {
      segments.push({
        type: "hunk",
        hunkIndex: hunkCounter++,
        rows: run.rows,
        id: `hunk-${run.start}`,
      })
      continue
    }
    const hasHunkBefore = r > 0
    const hasHunkAfter = r < runs.length - 1
    const len = run.rows.length
    if (!hasHunkBefore && !hasHunkAfter) {
      // No hunk anywhere — whole diff is context.
      if (n === 0 && len > 0) {
        segments.push({
          type: "context-collapsed",
          rows: run.rows,
          id: `ctx-${run.start}-all`,
        })
      } else if (len <= n) {
        segments.push({
          type: "context-visible",
          rows: run.rows,
          id: `ctx-${run.start}-only`,
        })
      } else {
        // Show a head of ``n`` lines + collapsed tail.
        segments.push({
          type: "context-visible",
          rows: run.rows.slice(0, n),
          id: `ctx-${run.start}-head`,
        })
        segments.push({
          type: "context-collapsed",
          rows: run.rows.slice(n),
          id: `ctx-${run.start}-tail`,
        })
      }
      continue
    }
    if (hasHunkBefore && hasHunkAfter) {
      // Between two hunks — show n at top (tail of prev context) + n at
      // bottom (head of next). Collapse the middle if longer than 2n.
      if (len <= n * 2) {
        segments.push({
          type: "context-visible",
          rows: run.rows,
          id: `ctx-${run.start}-full`,
        })
      } else {
        if (n > 0) {
          segments.push({
            type: "context-visible",
            rows: run.rows.slice(0, n),
            id: `ctx-${run.start}-top`,
          })
        }
        segments.push({
          type: "context-collapsed",
          rows: run.rows.slice(n, len - n),
          id: `ctx-${run.start}-mid`,
        })
        if (n > 0) {
          segments.push({
            type: "context-visible",
            rows: run.rows.slice(len - n),
            id: `ctx-${run.start}-bot`,
          })
        }
      }
      continue
    }
    if (!hasHunkBefore && hasHunkAfter) {
      // Leading context — anchor the visible tail to the upcoming hunk.
      if (len <= n) {
        segments.push({
          type: "context-visible",
          rows: run.rows,
          id: `ctx-${run.start}-lead`,
        })
      } else {
        segments.push({
          type: "context-collapsed",
          rows: run.rows.slice(0, len - n),
          id: `ctx-${run.start}-lead-hidden`,
        })
        segments.push({
          type: "context-visible",
          rows: run.rows.slice(len - n),
          id: `ctx-${run.start}-lead-tail`,
        })
      }
      continue
    }
    // hasHunkBefore && !hasHunkAfter → trailing context.
    if (len <= n) {
      segments.push({
        type: "context-visible",
        rows: run.rows,
        id: `ctx-${run.start}-trail`,
      })
    } else {
      segments.push({
        type: "context-visible",
        rows: run.rows.slice(0, n),
        id: `ctx-${run.start}-trail-head`,
      })
      segments.push({
        type: "context-collapsed",
        rows: run.rows.slice(n),
        id: `ctx-${run.start}-trail-hidden`,
      })
    }
  }
  return segments
}

export function PromptVersionDrawer({
  open,
  onClose,
  defaultAgentType = "orchestrator",
  subtypes = DEFAULT_PROMPT_AGENT_SUBTYPES,
  fetchVersions = fetchPromptVersions,
  diffLinesFn = diffLines,
  contextLines = DEFAULT_DIFF_CONTEXT_LINES,
}: PromptVersionDrawerProps) {
  const [agentType, setAgentType] = useState<string>(defaultAgentType)
  const [versions, setVersions] = useState<PromptVersionEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  // Reset selection on agent change — old IDs don't apply across agents
  // and the diff endpoint would 400 anyway (cross-agent guard).
  const lastAgentRef = useRef(agentType)

  // Fetch list when drawer opens or agent changes.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loading flag is the external-system sync signal for the fetch.
    setLoading(true)
    setError(null)
    fetchVersions(agentType, 20)
      .then(res => {
        if (cancelled) return
        setVersions(res.versions)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const msg = err instanceof Error ? err.message : String(err)
        setError(msg)
        setVersions([])
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [open, agentType, fetchVersions])

  // Wipe selection when the agent slug changes (cross-agent ids are
  // rejected by the backend diff endpoint anyway, and the in-component
  // diff would compare bodies from two unrelated prompts).
  useEffect(() => {
    if (lastAgentRef.current !== agentType) {
      lastAgentRef.current = agentType
      // eslint-disable-next-line react-hooks/set-state-in-effect -- reset depends on prop transition, not derivable during render.
      setSelectedIds([])
    }
  }, [agentType])

  // ESC closes the drawer (matches TurnDetailDrawer convention).
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [open, onClose])

  const toggleSelect = useCallback((id: number) => {
    setSelectedIds(prev => {
      if (prev.includes(id)) return prev.filter(x => x !== id)
      // Max 2 selections; if already 2, drop the oldest pick (FIFO).
      if (prev.length >= 2) return [prev[1], id]
      return [...prev, id]
    })
  }, [])

  const selectedPair = useMemo(() => {
    if (selectedIds.length !== 2) return null
    const a = versions.find(v => v.id === selectedIds[0])
    const b = versions.find(v => v.id === selectedIds[1])
    if (!a || !b) return null
    // Diff oldest → newest: order by version ascending so additions on
    // the right column reflect the forward edit (operator intuition: the
    // newer prompt is on the right).
    if (a.version <= b.version) return { from: a, to: b }
    return { from: b, to: a }
  }, [selectedIds, versions])

  const diffRows: DiffRow[] | null = useMemo(() => {
    if (!selectedPair) return null
    const { from, to } = selectedPair
    if (from.content_hash === to.content_hash) {
      // Identical-body shortcut — same hash means dedupe missed an
      // upstream race (or the operator picked the same row twice on
      // purpose). Either way, no diff to render.
      return []
    }
    const changes = diffLinesFn(from.content, to.content)
    return changesToDiffRows(changes)
  }, [selectedPair, diffLinesFn])

  const grouped = useMemo(() => {
    const out: { group: string; opts: PromptAgentSubtype[] }[] = []
    const seen = new Map<string, PromptAgentSubtype[]>()
    for (const opt of subtypes) {
      const g = opt.group ?? "Other"
      if (!seen.has(g)) seen.set(g, [])
      seen.get(g)!.push(opt)
    }
    for (const [group, opts] of seen) out.push({ group, opts })
    return out
  }, [subtypes])

  if (!open) return null

  // 2026-04-25 UX fix: portal the drawer into document.body to escape
  // the orchestrator panel's `.holo-glass` ancestor. `.holo-glass` sets
  // both `backdrop-filter: blur(10px)` AND `clip-path: polygon(...)` —
  // either one alone is enough to make the panel a *containing block
  // for fixed descendants* (CSS spec). Without portal, the drawer's
  // `fixed inset-0` was pinned to the orchestrator panel's bounding
  // box (≈half the viewport), which is why the operator saw "排版壞
  // 掉了，也沒辦法選擇跟復原" — backdrop and panel were clipped to
  // the orchestrator area, controls were unreachable. Portaling to
  // document.body ensures `fixed` is viewport-relative again.
  // SSR safety: `typeof document` guard so Next.js server render
  // doesn't crash; it returns null on the server (drawer is
  // operator-interactive only, no SEO/initial-render need).
  if (typeof document === "undefined") return null

  const drawerNode = (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="System prompt version drawer"
      data-testid="prompt-version-drawer"
      className="fixed inset-0 z-[70] flex items-stretch justify-end"
    >
      <div
        data-testid="prompt-version-drawer-backdrop"
        className="absolute inset-0 bg-[var(--deep-space-start,#010409)]/70 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />
      <div
        data-testid="prompt-version-drawer-panel"
        className="relative w-[min(960px,calc(100vw-2rem))] h-full bg-[var(--background)] border-l border-[var(--border)] shadow-2xl flex flex-col overflow-hidden"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 bg-[var(--background)] border-b border-[var(--border)] px-4 py-3 flex items-center gap-3">
          <History size={14} className="text-[var(--artifact-purple)] shrink-0" />
          <div className="flex-1 min-w-0">
            <p className="font-mono text-xs font-semibold text-[var(--foreground)]">
              System Prompt Versions
            </p>
            <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
              Pick two rows to side-by-side diff (additions green / deletions red).
              Press <kbd className="px-1 rounded bg-[var(--secondary)] text-[var(--foreground)]">n</kbd> to jump to the next hunk.
            </p>
          </div>
          <select
            data-testid="prompt-version-drawer-agent-select"
            aria-label="Agent type"
            value={agentType}
            onChange={e => setAgentType(e.target.value)}
            className="font-mono text-[11px] bg-[var(--secondary)] border border-[var(--border)] rounded px-2 py-1 text-[var(--foreground)] focus:outline-none focus:ring-1 focus:ring-[var(--neural-blue)]"
          >
            {grouped.map(g => (
              <optgroup key={g.group} label={g.group}>
                {g.opts.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </optgroup>
            ))}
          </select>
          <button
            type="button"
            onClick={() => {
              // Manual refresh — re-runs the effect by toggling via a
              // version-specific load trigger. We just call the same
              // fetcher directly; the effect's dep set is unchanged so
              // toggling state would be wasteful.
              setLoading(true)
              setError(null)
              fetchVersions(agentType, 20)
                .then(res => setVersions(res.versions))
                .catch((err: unknown) => {
                  setError(err instanceof Error ? err.message : String(err))
                  setVersions([])
                })
                .finally(() => setLoading(false))
            }}
            data-testid="prompt-version-drawer-refresh"
            aria-label="Refresh"
            className="p-1 rounded hover:bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close drawer"
            data-testid="prompt-version-drawer-close"
            className="p-1 rounded hover:bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
          >
            <X size={14} />
          </button>
        </div>

        <div className="flex-1 min-h-0 grid" style={{ gridTemplateColumns: "320px 1fr" }}>
          {/* Timeline list */}
          <div
            data-testid="prompt-version-drawer-timeline"
            className="border-r border-[var(--border)] overflow-y-auto"
          >
            {loading && versions.length === 0 && (
              <div
                data-testid="prompt-version-drawer-loading"
                className="px-3 py-6 font-mono text-[10px] text-[var(--muted-foreground)] flex items-center gap-2"
              >
                <RefreshCw size={10} className="animate-spin" />
                Loading…
              </div>
            )}
            {error && (
              <div
                data-testid="prompt-version-drawer-error"
                className="px-3 py-3 font-mono text-[10px] text-[var(--critical-red)] flex items-start gap-2"
              >
                <AlertCircle size={10} className="mt-0.5 shrink-0" />
                <span>{error}</span>
              </div>
            )}
            {!loading && !error && versions.length === 0 && (
              <div
                data-testid="prompt-version-drawer-empty"
                className="px-3 py-6 font-mono text-[10px] text-[var(--muted-foreground)]"
              >
                No prompt versions captured yet for{" "}
                <code className="text-[var(--foreground)]">{agentType}</code>.
                The backend snapshot capture (ZZ.C1 checkbox 2) writes a
                row on the next agent turn — refresh after the next call.
              </div>
            )}
            {versions.map(v => {
              const isSelected = selectedIds.includes(v.id)
              const selectionIndex = selectedIds.indexOf(v.id)
              return (
                <button
                  key={v.id}
                  type="button"
                  data-testid="prompt-version-row"
                  data-version-id={v.id}
                  data-version-selected={isSelected || undefined}
                  onClick={() => toggleSelect(v.id)}
                  className={`w-full text-left px-3 py-2 border-b border-[var(--border)]/40 transition-colors ${
                    isSelected
                      ? "bg-[var(--artifact-purple)]/15"
                      : "hover:bg-[var(--secondary)]/60"
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className="font-mono text-[10px] font-semibold tabular-nums"
                      style={{ color: isSelected ? "var(--artifact-purple)" : "var(--foreground)" }}
                    >
                      v{v.version}
                    </span>
                    <span
                      className="font-mono text-[9px] px-1 rounded"
                      style={{
                        backgroundColor: "color-mix(in srgb, var(--neural-blue) 15%, transparent)",
                        color: "var(--neural-blue)",
                      }}
                    >
                      {v.role || "—"}
                    </span>
                    <span
                      data-testid="prompt-version-hash"
                      className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums"
                      title={v.content_hash}
                    >
                      {shortHash(v.content_hash)}
                    </span>
                    <span
                      data-testid="prompt-version-created-at"
                      className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums ml-auto"
                      title={v.created_at}
                    >
                      {formatTimestamp(v.created_at)}
                    </span>
                    {selectionIndex >= 0 && (
                      <span
                        data-testid="prompt-version-selection-badge"
                        className="font-mono text-[9px] px-1 rounded bg-[var(--artifact-purple)] text-white tabular-nums shrink-0"
                      >
                        {selectionIndex === 0 ? "FROM" : "TO"}
                      </span>
                    )}
                  </div>
                  <pre
                    data-testid="prompt-version-preview"
                    className="font-mono text-[10px] text-[var(--muted-foreground)] mt-1 whitespace-pre-wrap break-words"
                  >
                    {v.content_preview || "(empty)"}
                  </pre>
                </button>
              )
            })}
          </div>

          {/* Diff viewer */}
          <div
            data-testid="prompt-version-drawer-diff"
            className="overflow-auto"
          >
            {!selectedPair && (
              <div
                data-testid="prompt-version-drawer-diff-prompt"
                className="h-full flex items-center justify-center px-6"
              >
                <div className="font-mono text-[11px] text-[var(--muted-foreground)] text-center max-w-sm">
                  <GitCompare size={24} className="mx-auto mb-2 opacity-50" />
                  Select <span className="text-[var(--foreground)]">two rows</span> from the timeline
                  to render a side-by-side diff. The first pick is the
                  baseline (FROM), the second the comparison (TO). Picking
                  a third row drops the oldest pick.
                </div>
              </div>
            )}
            {selectedPair && diffRows && (
              <DiffPanel
                from={selectedPair.from}
                to={selectedPair.to}
                rows={diffRows}
                contextLines={contextLines}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  )

  return createPortal(drawerNode, document.body)
}

function DiffPanel({
  from,
  to,
  rows,
  contextLines,
}: {
  from: PromptVersionEntry
  to: PromptVersionEntry
  rows: DiffRow[]
  contextLines: number
}) {
  const identical = from.content_hash === to.content_hash
  const empty = !identical && rows.length === 0

  const removedCount = rows.filter(r => r.kind === "removed").length
  const addedCount = rows.filter(r => r.kind === "added").length

  const segments = useMemo(
    () => groupRowsIntoSegments(rows, contextLines),
    [rows, contextLines],
  )

  // ``expandedContextIds`` tracks which collapsed segments the operator
  // has unfolded. Reset whenever the segment list changes (different
  // pair picked / contextLines changed).
  const [expandedContextIds, setExpandedContextIds] = useState<Set<string>>(
    () => new Set(),
  )
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- collapse state is derived from the (new) segment identities; explicit reset on the segment transition.
    setExpandedContextIds(new Set())
  }, [segments])

  const hunkCount = useMemo(
    () => segments.filter(s => s.type === "hunk").length,
    [segments],
  )

  const [activeHunkIndex, setActiveHunkIndex] = useState<number>(0)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset the cursor when the rendered diff changes.
    setActiveHunkIndex(0)
  }, [segments])

  const hunkRefs = useRef<Map<number, HTMLTableSectionElement | null>>(new Map())

  const jumpToHunk = useCallback(
    (next: number) => {
      if (hunkCount === 0) return
      const target = ((next % hunkCount) + hunkCount) % hunkCount
      setActiveHunkIndex(target)
      const el = hunkRefs.current.get(target)
      if (el && typeof el.scrollIntoView === "function") {
        el.scrollIntoView({ block: "nearest", behavior: "smooth" })
      }
    },
    [hunkCount],
  )

  // ZZ.C1 checkbox 4: ``n`` / ``Shift+n`` jumps between hunks when the
  // diff is rendered. Skip when focus is inside a form control so the
  // agent dropdown's type-ahead (operator types ``o`` to jump to
  // "Orchestrator" etc.) still works.
  useEffect(() => {
    if (identical || empty || hunkCount === 0) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "n" && e.key !== "N") return
      if (e.ctrlKey || e.metaKey || e.altKey) return
      const target = e.target as HTMLElement | null
      if (target) {
        const tag = target.tagName
        if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return
        if (target.isContentEditable) return
      }
      e.preventDefault()
      const goBack = e.key === "N" || e.shiftKey
      jumpToHunk(goBack ? activeHunkIndex - 1 : activeHunkIndex + 1)
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [identical, empty, hunkCount, activeHunkIndex, jumpToHunk])

  const expandSegment = useCallback((id: string) => {
    setExpandedContextIds(prev => {
      const next = new Set(prev)
      next.add(id)
      return next
    })
  }, [])

  return (
    <div className="h-full flex flex-col">
      {/* Diff header — version + hash + created_at on each side. */}
      <div
        data-testid="prompt-version-diff-header"
        className="px-3 py-2 border-b border-[var(--border)] grid grid-cols-2 gap-2 text-[10px] font-mono"
      >
        <div className="flex items-center gap-2">
          <span className="px-1 rounded bg-[var(--critical-red)]/15 text-[var(--critical-red)]">FROM</span>
          <span className="text-[var(--foreground)] tabular-nums">v{from.version}</span>
          <span className="text-[var(--muted-foreground)]" title={from.content_hash}>
            {shortHash(from.content_hash)}
          </span>
          <span className="text-[var(--muted-foreground)] truncate" title={from.created_at}>
            {formatTimestamp(from.created_at)}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="px-1 rounded bg-[var(--validation-emerald)]/15 text-[var(--validation-emerald)]">TO</span>
          <span className="text-[var(--foreground)] tabular-nums">v{to.version}</span>
          <span className="text-[var(--muted-foreground)]" title={to.content_hash}>
            {shortHash(to.content_hash)}
          </span>
          <span className="text-[var(--muted-foreground)] truncate" title={to.created_at}>
            {formatTimestamp(to.created_at)}
          </span>
        </div>
        <div
          data-testid="prompt-version-diff-summary"
          className="col-span-2 text-[var(--muted-foreground)] flex items-center gap-3"
        >
          <span className="text-[var(--critical-red)]">−{removedCount}</span>
          <span className="text-[var(--validation-emerald)]">+{addedCount}</span>
          <ChevronRight size={10} className="opacity-40" />
          <span>{rows.length} rendered row{rows.length === 1 ? "" : "s"}</span>
          {hunkCount > 0 && (
            <>
              <ChevronRight size={10} className="opacity-40" />
              <span data-testid="prompt-version-diff-hunk-counter">
                hunk {activeHunkIndex + 1} / {hunkCount}
              </span>
              <button
                type="button"
                onClick={() => jumpToHunk(activeHunkIndex - 1)}
                aria-label="Previous hunk"
                data-testid="prompt-version-diff-prev-hunk"
                className="ml-auto p-0.5 rounded hover:bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
              >
                <ArrowUp size={10} />
              </button>
              <button
                type="button"
                onClick={() => jumpToHunk(activeHunkIndex + 1)}
                aria-label="Next hunk (n)"
                data-testid="prompt-version-diff-next-hunk"
                className="p-0.5 rounded hover:bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
              >
                <ArrowDown size={10} />
              </button>
              <kbd
                className="font-mono text-[9px] px-1 rounded border border-[var(--border)] bg-[var(--secondary)] text-[var(--muted-foreground)]"
                title="Press n to jump to the next hunk, Shift+n for the previous"
              >
                n
              </kbd>
            </>
          )}
        </div>
      </div>

      {identical && (
        <div
          data-testid="prompt-version-diff-identical"
          className="px-3 py-6 font-mono text-[10px] text-[var(--muted-foreground)]"
        >
          Both versions share the same <code className="text-[var(--foreground)]">content_hash</code> —
          the bodies are byte-identical. Nothing to diff.
        </div>
      )}

      {empty && (
        <div
          data-testid="prompt-version-diff-empty"
          className="px-3 py-6 font-mono text-[10px] text-[var(--muted-foreground)]"
        >
          Both versions are empty. Nothing to diff.
        </div>
      )}

      {!identical && !empty && (
        <div
          data-testid="prompt-version-diff-side-by-side"
          className="flex-1 overflow-auto font-mono text-[10px] tabular-nums"
        >
          <table className="w-full border-collapse">
            {segments.map(segment => {
              if (segment.type === "hunk") {
                const isActive = segment.hunkIndex === activeHunkIndex
                return (
                  <tbody
                    key={segment.id}
                    data-testid="prompt-version-diff-hunk"
                    data-hunk-index={segment.hunkIndex}
                    data-hunk-active={isActive || undefined}
                    ref={el => {
                      if (el) hunkRefs.current.set(segment.hunkIndex, el)
                      else hunkRefs.current.delete(segment.hunkIndex)
                    }}
                    style={
                      isActive
                        ? {
                            outline: "1px solid var(--artifact-purple)",
                            outlineOffset: "-1px",
                          }
                        : undefined
                    }
                  >
                    {segment.rows.map((row, i) => (
                      <DiffTableRow
                        key={`${segment.id}-${i}`}
                        row={row}
                        testid="prompt-version-diff-row"
                      />
                    ))}
                  </tbody>
                )
              }
              if (segment.type === "context-visible") {
                return (
                  <tbody
                    key={segment.id}
                    data-testid="prompt-version-diff-context"
                  >
                    {segment.rows.map((row, i) => (
                      <DiffTableRow
                        key={`${segment.id}-${i}`}
                        row={row}
                        testid="prompt-version-diff-row"
                      />
                    ))}
                  </tbody>
                )
              }
              // collapsed segment
              const isExpanded = expandedContextIds.has(segment.id)
              if (isExpanded) {
                return (
                  <tbody
                    key={segment.id}
                    data-testid="prompt-version-diff-context-unfolded"
                    data-segment-id={segment.id}
                  >
                    {segment.rows.map((row, i) => (
                      <DiffTableRow
                        key={`${segment.id}-${i}`}
                        row={row}
                        testid="prompt-version-diff-row"
                      />
                    ))}
                  </tbody>
                )
              }
              const hiddenCount = segment.rows.length
              return (
                <tbody key={segment.id} data-testid="prompt-version-diff-context-collapsed-group">
                  <tr>
                    <td colSpan={2} className="p-0">
                      <button
                        type="button"
                        data-testid="prompt-version-diff-context-collapsed"
                        data-segment-id={segment.id}
                        data-hidden-lines={hiddenCount}
                        onClick={() => expandSegment(segment.id)}
                        className="w-full flex items-center justify-center gap-2 py-1 font-mono text-[10px] text-[var(--muted-foreground)] bg-[var(--secondary)]/40 hover:bg-[var(--secondary)]/80 border-y border-[var(--border)]/40 transition-colors"
                        aria-label={`Expand ${hiddenCount} hidden context line${hiddenCount === 1 ? "" : "s"}`}
                        title={`Expand ${hiddenCount} hidden context line${hiddenCount === 1 ? "" : "s"}`}
                      >
                        <ChevronsUpDown size={10} className="opacity-60" />
                        <span>
                          Expand {hiddenCount} line{hiddenCount === 1 ? "" : "s"} of context
                        </span>
                      </button>
                    </td>
                  </tr>
                </tbody>
              )
            })}
          </table>
        </div>
      )}
    </div>
  )
}

function DiffTableRow({
  row,
  testid,
}: {
  row: DiffRow
  testid: string
}) {
  return (
    <tr data-testid={testid} data-row-kind={row.kind}>
      <td
        className="align-top w-1/2 px-2 py-0.5 whitespace-pre-wrap break-words"
        style={{
          backgroundColor:
            row.kind === "removed"
              ? "color-mix(in srgb, var(--critical-red) 18%, transparent)"
              : "transparent",
          color: row.kind === "removed" ? "var(--critical-red)" : "var(--foreground)",
        }}
      >
        {row.left ?? " "}
      </td>
      <td
        className="align-top w-1/2 px-2 py-0.5 whitespace-pre-wrap break-words border-l border-[var(--border)]/40"
        style={{
          backgroundColor:
            row.kind === "added"
              ? "color-mix(in srgb, var(--validation-emerald) 18%, transparent)"
              : "transparent",
          color: row.kind === "added" ? "var(--validation-emerald)" : "var(--foreground)",
        }}
      >
        {row.right ?? " "}
      </td>
    </tr>
  )
}

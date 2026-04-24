"use client"

// ZZ.C1 (#305-1, 2026-04-24) checkbox 3 — PromptVersionDrawer.
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
// Subtype list: spec calls for 12 subtypes ("orchestrator / firmware /
// software / etc."). The list is exposed as ``DEFAULT_PROMPT_AGENT_SUBTYPES``
// so app integrators can override. Default mapping derives slugs from
// ``backend/agents/prompts/<slug>.md`` — checkbox 2's
// ``_snapshot_path_for(agent_type, sub_type)`` writes
// ``firmware__bsp.md`` / ``firmware__isp.md`` etc., so the slugs below
// match the on-disk filenames the snapshot capture produces.

import { useState, useEffect, useMemo, useCallback, useRef } from "react"
import {
  X,
  History,
  GitCompare,
  RefreshCw,
  ChevronRight,
  AlertCircle,
} from "lucide-react"
import { diffLines, type Change } from "diff"
import {
  fetchPromptVersions,
  type PromptVersionEntry,
  type PromptVersionsListResponse,
} from "@/lib/api"

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
}

interface DiffRow {
  /** ``"context"`` = same in both / ``"removed"`` = only in old / ``"added"`` = only in new */
  kind: "context" | "removed" | "added"
  /** Original (left) column content; null on a pure-added row. */
  left: string | null
  /** New (right) column content; null on a pure-removed row. */
  right: string | null
}

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

export function PromptVersionDrawer({
  open,
  onClose,
  defaultAgentType = "orchestrator",
  subtypes = DEFAULT_PROMPT_AGENT_SUBTYPES,
  fetchVersions = fetchPromptVersions,
  diffLinesFn = diffLines,
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

  return (
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
              />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function DiffPanel({
  from,
  to,
  rows,
}: {
  from: PromptVersionEntry
  to: PromptVersionEntry
  rows: DiffRow[]
}) {
  const identical = from.content_hash === to.content_hash
  const empty = !identical && rows.length === 0

  const removedCount = rows.filter(r => r.kind === "removed").length
  const addedCount = rows.filter(r => r.kind === "added").length

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
            <tbody>
              {rows.map((row, i) => (
                <tr
                  key={i}
                  data-testid="prompt-version-diff-row"
                  data-row-kind={row.kind}
                >
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
                    {row.left ?? " "}
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
                    {row.right ?? " "}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

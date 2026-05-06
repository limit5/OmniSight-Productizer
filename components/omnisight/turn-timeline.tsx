"use client"

// ZZ.B1 (#304-1, 2026-04-24): per-turn timeline cards — ccxray 招牌 UI.
//
// Checkbox 1 landed the five-line TurnCard (horizontal/vertical layouts,
// ring buffer of the last 100 turns, SSE wiring to ``turn_metrics`` +
// ``turn_tool_stats``). Checkbox 2 (this change) adds the click-to-expand
// detail drawer: tapping a card opens ``<TurnDetailDrawer>`` showing full
// LLM messages (system / user / assistant / tool), per-message prompt
// token breakdown, and used-tools detail (name + success + args + result
// + duration). The dedicated ``turn.complete`` SSE event that will carry
// the messages + per-tool-call bodies is checkbox 3; until it lands the
// drawer degrades gracefully — it shows the metadata we already have
// (tokens, cost, cache, latency, gap, context) plus any ``failed_tools``
// from ``turn_tool_stats``, and a clear "waiting for turn.complete event"
// placeholder in the messages section so operators know WHY the body is
// empty. This is the NULL-vs-genuine-zero contract ZZ.A1 established for
// every other surface on this card.
//
// Ring buffer: last 100 turns kept in component state (LRU by turn
// arrival order). The buffer matches the spec's "前端用 ring buffer 存
// 最近 100 turn" requirement; once ``GET /runtime/turns?limit=50`` lands
// in a later checkbox we'll seed the buffer on mount.

import { useState, useEffect, useRef, useCallback } from "react"
import {
  Clock,
  ChevronUp,
  ChevronDown,
  Rows3,
  MoveHorizontal,
  X,
  AlertTriangle,
  CheckCircle2,
  Wrench,
} from "lucide-react"
import { subscribeEvents, fetchTurnHistory, type TurnCompletePayload } from "@/lib/api"
import { getModelInfo } from "./agent-matrix-wall"
import { Block } from "./block"

const RING_BUFFER_SIZE = 100
const LINE5_MAX_CHARS = 80

/**
 * ZZ.B1 checkbox 2: one LLM message part inside a turn — system prompt,
 * the user's inbound message, the assistant response, or a tool result.
 * Populated by the (future) ``turn.complete`` SSE event / ``GET
 * /runtime/turns`` endpoint (ZZ.B1 checkbox 3); absent until that ships,
 * in which case the drawer degrades to a placeholder.
 */
export interface TurnMessagePart {
  role: "system" | "user" | "assistant" | "tool"
  content: string
  /** Prompt token count contributed by this message (for Line 3 of the
   *  drawer's "prompt token breakdown per message" section). `null` when
   *  the backend cannot attribute tokens at message granularity. */
  tokens?: number | null
  /** Only meaningful for ``role="tool"`` (the tool whose result this
   *  message carries) or ``role="assistant"`` entries that represent
   *  tool-call requests. */
  toolName?: string | null
}

/**
 * ZZ.B1 checkbox 2: one tool invocation's detail inside a turn. The
 * drawer renders a list of these in the "used tools 明細" section.
 * ``success`` is required (drives the pass/fail badge); args / result /
 * durationMs are optional and rendered only when present.
 */
export interface TurnToolCallDetail {
  name: string
  success: boolean
  args?: Record<string, unknown> | null
  result?: string | null
  durationMs?: number | null
}

export interface TurnCardData {
  turnNumber: number
  /** ZZ.B1 checkbox 3 (2026-04-24): backend-assigned unique id carried
   *  by the ``turn.complete`` SSE event / ``GET /runtime/turns`` rows.
   *  Used to match an incoming ``turn.complete`` to an existing ring-
   *  buffer entry so the drawer upgrades in place instead of creating
   *  a duplicate card. ``null`` when the turn was materialised from
   *  ``turn_metrics`` alone (turn.complete hasn't landed for it yet). */
  turnId: string | null
  timestamp: string
  tsMs: number
  model: string
  provider: string | null
  agentSubtype: string | null
  inputTokens: number
  outputTokens: number
  tokensUsed: number
  cacheReadTokens: number | null
  cacheCreateTokens: number | null
  cacheHitRatio: number | null
  contextLimit: number | null
  contextUsagePct: number | null
  latencyMs: number
  toolCallCount: number | null
  toolFailureCount: number | null
  failedTools: string[]
  gapMs: number | null
  costUsd: number | null
  summary: string | null
  /** ZZ.B1 checkbox 2 drawer payload — populated when ``turn.complete``
   *  ships (checkbox 3). ``undefined`` = not yet received → drawer shows
   *  "waiting for turn.complete" placeholder; ``[]`` = received but
   *  empty (degenerate turn, e.g. synthesised cancellation) → drawer
   *  shows "no messages recorded for this turn". */
  messages?: TurnMessagePart[]
  /** ZZ.B1 checkbox 2 drawer payload — tool invocation detail. Same
   *  NULL-vs-empty-array contract as ``messages``. When absent, the
   *  drawer falls back to the summary aggregates from ``turn_tool_stats``
   *  (call count + failed tool names) so the section isn't empty. */
  toolCallDetails?: TurnToolCallDetail[]
}

// Rough public-list pricing per 1M tokens (input / output). ZZ.B1 first
// checkbox emits cost estimates from frontend because ``turn_metrics``
// does not carry cost today; the authoritative ``turn.complete`` event
// (separate checkbox) will replace this with backend-computed cost.
// Kept intentionally small — fuzzy-matched by prefix so a provider
// upgrade (opus-4-7 → opus-4-8) keeps working without redeploy.
const MODEL_PRICING_PER_MTOK: Record<string, { in: number; out: number }> = {
  "claude-opus": { in: 15, out: 75 },
  "claude-sonnet": { in: 3, out: 15 },
  "claude-haiku": { in: 0.8, out: 4 },
  "gpt-5": { in: 5, out: 15 },
  "gpt-4o": { in: 2.5, out: 10 },
  "gemini-3": { in: 1.25, out: 5 },
  "gemini-1.5": { in: 1.25, out: 5 },
  "deepseek": { in: 0.27, out: 1.1 },
  "grok": { in: 3, out: 15 },
  "mistral": { in: 2, out: 6 },
  "llama": { in: 0, out: 0 },
  "ollama": { in: 0, out: 0 },
  "gemma": { in: 0, out: 0 },
}

function estimateCost(model: string, inTok: number, outTok: number): number | null {
  const lower = model.toLowerCase()
  const slashIdx = lower.indexOf("/")
  const normalized = slashIdx > 0 ? lower.slice(slashIdx + 1) : lower
  const keys = Object.keys(MODEL_PRICING_PER_MTOK).sort((a, b) => b.length - a.length)
  for (const k of keys) {
    if (normalized.startsWith(k) || normalized.includes(k)) {
      const p = MODEL_PRICING_PER_MTOK[k]
      return (inTok / 1_000_000) * p.in + (outTok / 1_000_000) * p.out
    }
  }
  return null
}

function formatRelativeTs(tsMs: number, anchorMs: number): string {
  if (!anchorMs || anchorMs <= 0) return "+00:00:00"
  const deltaMs = Math.max(0, tsMs - anchorMs)
  const hh = Math.floor(deltaMs / 3_600_000)
  const mm = Math.floor((deltaMs % 3_600_000) / 60_000)
  const ss = Math.floor((deltaMs % 60_000) / 1000)
  const pad = (n: number) => n.toString().padStart(2, "0")
  return `+${pad(hh)}:${pad(mm)}:${pad(ss)}`
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M"
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k"
  return n.toString()
}

function formatCost(c: number | null): string {
  if (c === null) return "$—"
  if (c >= 1) return `$${c.toFixed(2)}`
  if (c >= 0.001) return `$${c.toFixed(3)}`
  if (c > 0) return `$${c.toFixed(4)}`
  return "$0"
}

/**
 * ZZ.B1 checkbox 3 (2026-04-24): synthesise a fresh ``TurnCardData``
 * from a ``turn.complete`` payload. Used in two paths:
 *   (a) mount-time backfill from ``GET /runtime/turns`` — we don't have
 *       prior ``turn_metrics`` for historical turns, so the complete
 *       payload is the only source of truth and must materialise the
 *       full card on its own.
 *   (b) live SSE when ``turn_metrics`` never landed for this turn
 *       (reconnect mid-turn / dropped event / ring-buffer eviction).
 */
function turnFromCompletePayload(
  d: TurnCompletePayload,
  turnNumber: number,
): TurnCardData {
  const parsed = d.timestamp ? Date.parse(d.timestamp) : NaN
  const tsMs = Number.isFinite(parsed) ? parsed : Date.now()
  const cacheRead = d.cache_read_tokens ?? null
  const cacheCreate = d.cache_create_tokens ?? null
  const denom = (d.input_tokens || 0) + (cacheRead ?? 0) + (cacheCreate ?? 0)
  const cacheHitRatio = (cacheRead !== null && denom > 0) ? cacheRead / denom : null
  return {
    turnNumber,
    turnId: d.turn_id,
    timestamp: d.timestamp || new Date(tsMs).toISOString(),
    tsMs,
    model: d.model,
    provider: d.provider ?? null,
    agentSubtype: d.agent_type ?? null,
    inputTokens: d.input_tokens,
    outputTokens: d.output_tokens,
    tokensUsed: d.tokens_used,
    cacheReadTokens: cacheRead,
    cacheCreateTokens: cacheCreate,
    cacheHitRatio,
    contextLimit: d.context_limit,
    contextUsagePct: d.context_usage_pct,
    latencyMs: d.latency_ms ?? 0,
    toolCallCount: d.tool_call_count,
    toolFailureCount: d.tool_failure_count,
    failedTools: (d.tool_calls ?? [])
      .filter(tc => !tc.success)
      .map(tc => tc.name),
    gapMs: null,
    costUsd: d.cost_usd,
    summary: d.summary ?? null,
    messages: (d.messages ?? []).map(m => ({
      role: m.role,
      content: m.content,
      tokens: m.tokens ?? null,
      toolName: m.tool_name ?? null,
    })),
    toolCallDetails: (d.tool_calls ?? []).map(tc => ({
      name: tc.name,
      success: tc.success,
      args: tc.args ?? null,
      result: tc.result ?? null,
      durationMs: tc.duration_ms ?? null,
    })),
  }
}

export type TurnTimelineLayout = "horizontal" | "vertical"

interface TurnTimelineProps {
  className?: string
  maxTurns?: number
  /** Optional controlled layout. If omitted, the component owns its own state. */
  layout?: TurnTimelineLayout
  onLayoutChange?: (layout: TurnTimelineLayout) => void
  /** Escape-hatch for tests / future ``GET /runtime/turns`` backfill. When
   *  supplied, the internal SSE subscription is bypassed. */
  externalTurns?: TurnCardData[]
}

export function TurnTimeline({
  className = "",
  maxTurns = RING_BUFFER_SIZE,
  layout: layoutProp,
  onLayoutChange,
  externalTurns,
}: TurnTimelineProps) {
  const [expanded, setExpanded] = useState(true)
  const [internalLayout, setInternalLayout] = useState<TurnTimelineLayout>("horizontal")
  const layout = layoutProp ?? internalLayout
  const setLayout = (next: TurnTimelineLayout) => {
    if (layoutProp === undefined) setInternalLayout(next)
    onLayoutChange?.(next)
  }

  const [turns, setTurns] = useState<TurnCardData[]>(externalTurns ?? [])
  const turnCounterRef = useRef(0)
  // ZZ.B1 checkbox 2: drawer open state. Keyed by turnNumber because
  // the TurnCardData reference changes every time ``turn_tool_stats``
  // attaches to the latest turn (immutable update) — tracking the card
  // by identity would close the drawer on every SSE frame. ``null`` =
  // closed.
  const [selectedTurnNumber, setSelectedTurnNumber] = useState<number | null>(null)
  const closeDrawer = useCallback(() => setSelectedTurnNumber(null), [])
  const selectedTurn = selectedTurnNumber !== null
    ? turns.find(t => t.turnNumber === selectedTurnNumber) ?? null
    : null

  useEffect(() => {
    if (externalTurns) {
      setTurns(externalTurns) // eslint-disable-line react-hooks/set-state-in-effect -- sync controlled prop to local state
      return
    }

    // ZZ.B1 checkbox 3: seed ring buffer from persisted turn.complete
    // rows so a fresh mount (page reload, tab switch) doesn't have to
    // wait for the next live emit to show anything. "cancelled"
    // guards against the unmount-before-fetch-resolves race — without
    // it the old component's setTurns fires after unmount, which (a)
    // React logs a warning about and (b) briefly shows stale history
    // behind the next mount. Best-effort: a fetch failure leaves the
    // empty state visible (the live SSE below will still populate).
    let cancelled = false
    fetchTurnHistory({ limit: Math.min(maxTurns, 50) })
      .then(resp => {
        if (cancelled) return
        const backfilled = resp.turns
          .slice()
          .reverse() // endpoint returns newest-first; ring buffer wants oldest-first
          .map(p => turnFromCompletePayload(p, ++turnCounterRef.current))
        if (backfilled.length > 0) setTurns(backfilled)
      })
      .catch(() => { /* swallow — not fatal to live SSE */ })

    const handle = subscribeEvents((event) => {
      if (event.event === "turn_metrics") {
        const d = event.data
        if (!d.model) return
        const parsed = d.timestamp ? Date.parse(d.timestamp) : NaN
        const tsMs = Number.isFinite(parsed) ? parsed : Date.now()
        setTurns(prev => {
          const sameModelPrev = [...prev].reverse().find(
            t => t.model.toLowerCase() === d.model.toLowerCase()
          )
          const rawGap = sameModelPrev
            ? (tsMs - sameModelPrev.tsMs) - (d.latency_ms ?? 0)
            : null
          const gapMs = rawGap !== null && rawGap >= 0 ? Math.round(rawGap) : null
          const cacheRead = d.cache_read_tokens ?? null
          const cacheCreate = d.cache_create_tokens ?? null
          // Hit ratio = fraction of prompt traffic served from cache.
          const denom = (d.input_tokens || 0) + (cacheRead ?? 0) + (cacheCreate ?? 0)
          const cacheHitRatio = (cacheRead !== null && denom > 0) ? cacheRead / denom : null
          turnCounterRef.current += 1
          const newCard: TurnCardData = {
            turnNumber: turnCounterRef.current,
            turnId: null,
            timestamp: d.timestamp || new Date(tsMs).toISOString(),
            tsMs,
            model: d.model,
            provider: d.provider ?? null,
            agentSubtype: null,
            inputTokens: d.input_tokens,
            outputTokens: d.output_tokens,
            tokensUsed: d.tokens_used,
            cacheReadTokens: cacheRead,
            cacheCreateTokens: cacheCreate,
            cacheHitRatio,
            contextLimit: d.context_limit,
            contextUsagePct: d.context_usage_pct,
            latencyMs: d.latency_ms ?? 0,
            toolCallCount: null,
            toolFailureCount: null,
            failedTools: [],
            gapMs,
            costUsd: estimateCost(d.model, d.input_tokens, d.output_tokens),
            summary: null,
          }
          const next = [...prev, newCard]
          return next.length > maxTurns ? next.slice(next.length - maxTurns) : next
        })
      } else if (event.event === "turn_tool_stats") {
        const d = event.data
        setTurns(prev => {
          if (prev.length === 0) return prev
          const last = prev[prev.length - 1]
          return [
            ...prev.slice(0, -1),
            {
              ...last,
              agentSubtype: last.agentSubtype ?? d.agent_type ?? null,
              toolCallCount: d.tool_call_count,
              toolFailureCount: d.tool_failure_count,
              failedTools: d.failed_tools ?? [],
            },
          ]
        })
      } else if (event.event === "turn.complete") {
        // ZZ.B1 checkbox 3: terminal per-turn event. Upgrade the card
        // materialised by the earlier turn_metrics emit in place
        // (match by turn_id first — the backend assigns a unique one —
        // falling back to "the latest card for this model" for the
        // narrow window where turn_metrics has landed but turn.complete
        // lost its turn_id on the wire). If no matching card exists
        // (ring buffer evicted it, or reconnect mid-turn) append a
        // fresh card synthesised from the turn.complete payload so
        // the drawer still has something to open.
        const d = event.data
        if (!d.model) return
        setTurns(prev => {
          const matchByIdIdx = d.turn_id
            ? prev.findIndex(t => t.turnId === d.turn_id)
            : -1
          const matchByModelIdx = matchByIdIdx >= 0
            ? matchByIdIdx
            : (() => {
                for (let i = prev.length - 1; i >= 0; i--) {
                  if (prev[i].turnId === null
                      && prev[i].model.toLowerCase() === d.model.toLowerCase()) {
                    return i
                  }
                }
                return -1
              })()

          if (matchByModelIdx >= 0) {
            const existing = prev[matchByModelIdx]
            const merged: TurnCardData = {
              ...existing,
              turnId: d.turn_id,
              // turn.complete is backend-authoritative — prefer its
              // cost / summary / messages / tool_calls over frontend
              // estimates carried on the pre-complete card.
              costUsd: d.cost_usd ?? existing.costUsd,
              summary: d.summary ?? existing.summary,
              agentSubtype: existing.agentSubtype ?? d.agent_type ?? null,
              messages: (d.messages ?? []).map(m => ({
                role: m.role,
                content: m.content,
                tokens: m.tokens ?? null,
                toolName: m.tool_name ?? null,
              })),
              toolCallDetails: (d.tool_calls ?? []).map(tc => ({
                name: tc.name,
                success: tc.success,
                args: tc.args ?? null,
                result: tc.result ?? null,
                durationMs: tc.duration_ms ?? null,
              })),
              // Don't clobber toolCallCount if turn_tool_stats already
              // landed — that event carries the graph-level tally
              // including retries; turn.complete carries only the
              // detail list which may be a subset today.
              toolCallCount: existing.toolCallCount ?? d.tool_call_count ?? null,
              toolFailureCount: existing.toolFailureCount ?? d.tool_failure_count ?? null,
            }
            return [...prev.slice(0, matchByModelIdx), merged, ...prev.slice(matchByModelIdx + 1)]
          }

          // No match — synthesise a brand-new card from turn.complete.
          turnCounterRef.current += 1
          const fresh = turnFromCompletePayload(d, turnCounterRef.current)
          const next = [...prev, fresh]
          return next.length > maxTurns ? next.slice(next.length - maxTurns) : next
        })
      }
    })
    return () => {
      cancelled = true
      handle.close()
    }
  }, [externalTurns, maxTurns])

  const anchorMs = turns.length > 0 ? turns[0].tsMs : 0

  return (
    <div
      className={`border-b border-[var(--border)] ${className}`}
      data-testid="turn-timeline"
    >
      <div className="flex items-center justify-between px-4 py-2 text-xs font-mono text-[var(--muted-foreground)]">
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-2 hover:text-[var(--foreground)] transition-colors"
          aria-expanded={expanded}
        >
          <Clock size={12} className="text-[var(--neural-blue)]" />
          <span>TURN TIMELINE</span>
          <span>({turns.length})</span>
        </button>
        <div className="flex items-center gap-1">
          <button
            data-testid="turn-timeline-layout-horizontal"
            aria-label="Horizontal scroll layout"
            aria-pressed={layout === "horizontal"}
            onClick={() => setLayout("horizontal")}
            className={`p-1 rounded transition-colors ${
              layout === "horizontal"
                ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            }`}
            title="Horizontal scroll"
          >
            <MoveHorizontal size={12} />
          </button>
          <button
            data-testid="turn-timeline-layout-vertical"
            aria-label="Vertical stack layout"
            aria-pressed={layout === "vertical"}
            onClick={() => setLayout("vertical")}
            className={`p-1 rounded transition-colors ${
              layout === "vertical"
                ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            }`}
            title="Vertical stack"
          >
            <Rows3 size={12} />
          </button>
          <button
            onClick={() => setExpanded(!expanded)}
            className="p-1 rounded text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
            aria-label={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          </button>
        </div>
      </div>

      {expanded && turns.length === 0 && (
        <div
          className="px-4 pb-3 font-mono text-[10px] text-[var(--muted-foreground)]"
          data-testid="turn-timeline-empty"
        >
          No turns yet — waiting for LLM activity.
        </div>
      )}

      {expanded && turns.length > 0 && (
        <div
          className={
            layout === "horizontal"
              ? "px-3 pb-3 flex gap-2 overflow-x-auto"
              : "px-3 pb-3 flex flex-col gap-2 overflow-y-auto max-h-[480px]"
          }
          data-testid={`turn-timeline-body-${layout}`}
        >
          {turns.map(t => (
            <TurnCard
              key={t.turnNumber}
              turn={t}
              anchorMs={anchorMs}
              compact={layout === "horizontal"}
              onClick={() => setSelectedTurnNumber(t.turnNumber)}
            />
          ))}
        </div>
      )}

      {selectedTurn && (
        <TurnDetailDrawer turn={selectedTurn} onClose={closeDrawer} />
      )}
    </div>
  )
}

function TurnCard({
  turn,
  anchorMs,
  compact,
  onClick,
}: {
  turn: TurnCardData
  anchorMs: number
  compact: boolean
  onClick?: () => void
}) {
  const modelInfo = getModelInfo(turn.model)
  const ctxPct = turn.contextUsagePct
  const ctxBarColor = ctxPct === null
    ? "var(--muted-foreground)"
    : ctxPct >= 90
      ? "var(--critical-red)"
      : ctxPct >= 75
        ? "var(--hardware-orange)"
        : "var(--validation-emerald)"
  const hasToolStats = turn.toolCallCount !== null
  const toolFail = turn.toolFailureCount ?? 0
  const cacheRead = turn.cacheReadTokens
  const hasCacheSignal = cacheRead !== null && cacheRead > 0
  const rawSummary = turn.summary ?? ""
  const displaySummary = rawSummary
    ? rawSummary.length > LINE5_MAX_CHARS
      ? rawSummary.slice(0, LINE5_MAX_CHARS) + "…"
      : rawSummary
    : `${modelInfo.shortLabel || turn.model} · ${formatTokens(turn.tokensUsed)} tokens / ${turn.latencyMs}ms`

  return (
    <div
      data-testid="turn-card"
      data-turn-number={turn.turnNumber}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      aria-label={onClick ? `Turn ${turn.turnNumber} detail` : undefined}
      onClick={onClick}
      onKeyDown={onClick ? (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          onClick()
        }
      } : undefined}
      className={`rounded-md bg-[var(--secondary)] p-2 border border-[var(--border)]/50 ${
        onClick ? "cursor-pointer hover:border-[var(--neural-blue)]/60 hover:bg-[var(--secondary)]/80 focus:outline-none focus-visible:outline-none focus:ring-1 focus-visible:ring-1 focus:ring-[var(--neural-blue)]/60 focus-visible:ring-[var(--neural-blue)]/60" : ""
      } ${
        compact ? "min-w-[280px] max-w-[300px] shrink-0" : "w-full"
      }`}
    >
      {/* Line 1 — turn # + relative timestamp + agent subtype / model label */}
      <div className="flex items-center gap-2 font-mono text-[10px] text-[var(--muted-foreground)]">
        <span
          className="font-semibold text-[var(--foreground)]"
          data-testid="turn-card-number"
        >
          #{turn.turnNumber}
        </span>
        <span className="tabular-nums" data-testid="turn-card-timestamp">
          {formatRelativeTs(turn.tsMs, anchorMs)}
        </span>
        <span
          data-testid="turn-card-subtype"
          className="px-1 rounded truncate font-mono"
          style={{
            backgroundColor: `color-mix(in srgb, ${modelInfo.color} 15%, transparent)`,
            color: modelInfo.color,
          }}
          title={turn.model}
        >
          {turn.agentSubtype || modelInfo.shortLabel || "agent"}
        </span>
      </div>

      {/* Line 2 — cost + token breakdown badges (in / out / cache) */}
      <div className="flex items-center gap-1.5 mt-1 font-mono text-[10px] flex-wrap">
        <span
          data-testid="turn-card-cost"
          className="text-[var(--hardware-orange)] font-semibold tabular-nums"
          title="Estimated cost (will be replaced by backend-authoritative value when turn.complete ships)"
        >
          {formatCost(turn.costUsd)}
        </span>
        <span
          data-testid="turn-card-tokens-in"
          className="px-1 rounded bg-[var(--background)] text-[var(--neural-blue)]"
        >
          in {formatTokens(turn.inputTokens)}
        </span>
        <span
          data-testid="turn-card-tokens-out"
          className="px-1 rounded bg-[var(--background)] text-[var(--validation-emerald)]"
        >
          out {formatTokens(turn.outputTokens)}
        </span>
        {hasCacheSignal && (
          <span
            data-testid="turn-card-tokens-cache"
            className="px-1 rounded bg-[var(--background)] text-[var(--artifact-purple)]"
          >
            cache {formatTokens(cacheRead as number)}
          </span>
        )}
      </div>

      {/* Line 3 — context window bar (ZZ.A2) + inter-turn gap (ZZ.A3) */}
      <div className="flex items-center gap-2 mt-1">
        <div className="flex-1 min-w-0 flex items-center gap-1">
          <div
            className="h-1 flex-1 rounded-full bg-[var(--border)] overflow-hidden"
            data-testid="turn-card-ctx-bar"
          >
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: ctxPct !== null ? `${Math.min(ctxPct, 100)}%` : "0%",
                backgroundColor: ctxBarColor,
              }}
            />
          </div>
          <span
            className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums shrink-0"
            data-testid="turn-card-ctx-pct"
          >
            {ctxPct !== null ? `${ctxPct.toFixed(0)}%` : "—"}
          </span>
        </div>
        <span
          className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums shrink-0"
          data-testid="turn-card-gap"
          title="Inter-turn gap (time between consecutive LLM calls minus this turn's latency)"
        >
          gap {turn.gapMs !== null ? `${turn.gapMs}ms` : "—"}
        </span>
      </div>

      {/* Line 4 — tool calls (used / failed) + cache hit badge */}
      <div className="flex items-center gap-2 mt-1 font-mono text-[10px]">
        <span
          className="text-[var(--muted-foreground)]"
          data-testid="turn-card-tools"
          title={
            hasToolStats
              ? turn.failedTools.length > 0
                ? `Failed tools: ${turn.failedTools.join(", ")}`
                : "No tool failures"
              : "no turn_tool_stats seen for this turn yet"
          }
        >
          tools {hasToolStats ? turn.toolCallCount : "—"}
          <span
            data-testid="turn-card-tools-failed"
            className={`ml-1 ${
              hasToolStats && toolFail > 0
                ? "text-[var(--critical-red)] font-semibold"
                : "text-[var(--muted-foreground)]"
            }`}
          >
            / failed {hasToolStats ? toolFail : "—"}
          </span>
        </span>
        <span
          className="ml-auto text-[var(--artifact-purple)]"
          data-testid="turn-card-cache-hit"
          title={
            turn.cacheHitRatio !== null
              ? `cache_read=${turn.cacheReadTokens ?? 0} cache_create=${turn.cacheCreateTokens ?? 0}`
              : "no cache data"
          }
        >
          cache {turn.cacheHitRatio !== null ? `${(turn.cacheHitRatio * 100).toFixed(0)}%` : "—"}
        </span>
      </div>

      {/* Line 5 — first 80 chars of agent response or tool summary.
          ``turn.summary`` will populate when the dedicated ``turn.complete``
          SSE event ships (separate ZZ.B1 checkbox). Until then we show a
          one-line digest derived from what ``turn_metrics`` already carries
          so the card has a fifth visible line on day one. */}
      <div
        className="mt-1 font-mono text-[10px] text-[var(--muted-foreground)] truncate"
        data-testid="turn-card-summary"
        title={rawSummary || displaySummary}
      >
        {displaySummary}
      </div>
    </div>
  )
}

/* ────────────────────────────────────────────────────────────────────
 * ZZ.B1 checkbox 2 — <TurnDetailDrawer>
 * Modal-style drawer opened when an operator clicks a TurnCard. Renders
 * full LLM messages (system / user / assistant / tool) with per-message
 * prompt-token breakdown, plus a tool-call detail list. When the
 * ``turn.complete`` SSE event hasn't landed for this turn yet (checkbox
 * 3 work), the body sections degrade to informative placeholders so the
 * operator understands the "empty" state isn't a bug — it's "we don't
 * have that detail for this turn (yet)".
 * ──────────────────────────────────────────────────────────────────── */

const ROLE_STYLES: Record<TurnMessagePart["role"], { label: string; color: string; bg: string }> = {
  system:    { label: "SYSTEM",    color: "var(--muted-foreground)",    bg: "color-mix(in srgb, var(--muted-foreground) 12%, transparent)" },
  user:      { label: "USER",      color: "var(--neural-blue)",         bg: "color-mix(in srgb, var(--neural-blue) 12%, transparent)" },
  assistant: { label: "ASSISTANT", color: "var(--validation-emerald)",  bg: "color-mix(in srgb, var(--validation-emerald) 12%, transparent)" },
  tool:      { label: "TOOL",      color: "var(--artifact-purple)",     bg: "color-mix(in srgb, var(--artifact-purple) 12%, transparent)" },
}

function stringifyArgs(args: TurnToolCallDetail["args"]): string {
  if (args === null || args === undefined) return ""
  try {
    return JSON.stringify(args, null, 2)
  } catch {
    return String(args)
  }
}

function TurnDetailDrawer({
  turn,
  onClose,
}: {
  turn: TurnCardData
  onClose: () => void
}) {
  const modelInfo = getModelInfo(turn.model)

  // ESC to close. Registered once per open; removed on unmount.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [onClose])

  const messages = turn.messages
  const hasMessages = Array.isArray(messages)
  const messagesEmpty = hasMessages && messages!.length === 0

  // Tool call details: prefer explicit structured list; else fall back
  // to the aggregates carried by ``turn_tool_stats`` so the section is
  // not empty even before ``turn.complete`` ships.
  const toolDetails = turn.toolCallDetails
  const hasExplicitTools = Array.isArray(toolDetails)
  const fallbackToolEntries: TurnToolCallDetail[] =
    !hasExplicitTools && turn.failedTools.length > 0
      ? turn.failedTools.map(n => ({ name: n, success: false }))
      : []
  const renderedTools: TurnToolCallDetail[] = hasExplicitTools
    ? (toolDetails as TurnToolCallDetail[])
    : fallbackToolEntries

  // Summed prompt tokens across messages (for the breakdown footer's
  // sanity check — should roughly equal inputTokens + cacheRead +
  // cacheCreate, minus any output-side attribution).
  const perMsgTokenSum = hasMessages
    ? messages!.reduce((acc, m) => acc + (m.tokens ?? 0), 0)
    : 0

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Turn ${turn.turnNumber} detail`}
      data-testid="turn-detail-drawer"
      className="fixed inset-0 z-[70] flex items-stretch justify-end"
    >
      <div
        data-testid="turn-detail-drawer-backdrop"
        className="absolute inset-0 bg-[var(--deep-space-start,#010409)]/70 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />
      <div
        data-testid="turn-detail-drawer-panel"
        className="relative w-[min(640px,calc(100vw-2rem))] h-full bg-[var(--background)] border-l border-[var(--border)] shadow-2xl overflow-y-auto"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 bg-[var(--background)] border-b border-[var(--border)] px-4 py-3 flex items-center gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 font-mono text-xs text-[var(--muted-foreground)]">
              <span className="font-semibold text-[var(--foreground)]" data-testid="turn-detail-turn-number">
                Turn #{turn.turnNumber}
              </span>
              <span
                data-testid="turn-detail-model"
                className="px-1 rounded truncate"
                style={{ backgroundColor: `color-mix(in srgb, ${modelInfo.color} 15%, transparent)`, color: modelInfo.color }}
                title={turn.model}
              >
                {turn.agentSubtype || modelInfo.shortLabel || turn.model}
              </span>
              <span className="tabular-nums" data-testid="turn-detail-timestamp">
                {turn.timestamp}
              </span>
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close detail drawer"
            data-testid="turn-detail-close"
            className="p-1 rounded hover:bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
          >
            <X size={14} />
          </button>
        </div>

        {/* Metadata strip — always available from turn_metrics */}
        <div
          data-testid="turn-detail-metadata"
          className="px-4 py-3 grid grid-cols-2 gap-x-4 gap-y-2 font-mono text-[10px] text-[var(--muted-foreground)] border-b border-[var(--border)]"
        >
          <div>
            <span className="text-[var(--foreground)] font-semibold">{formatCost(turn.costUsd)}</span>
            <span className="ml-2">cost (est.)</span>
          </div>
          <div>
            <span className="text-[var(--foreground)] tabular-nums">{turn.latencyMs}ms</span>
            <span className="ml-2">latency</span>
          </div>
          <div>
            <span className="text-[var(--foreground)] tabular-nums">{formatTokens(turn.inputTokens)}</span>
            <span className="mx-1">in /</span>
            <span className="text-[var(--foreground)] tabular-nums">{formatTokens(turn.outputTokens)}</span>
            <span className="ml-1">out</span>
          </div>
          <div>
            <span className="text-[var(--foreground)] tabular-nums">
              {turn.contextUsagePct !== null ? `${turn.contextUsagePct.toFixed(0)}%` : "—"}
            </span>
            <span className="ml-2">context</span>
          </div>
          <div>
            <span className="text-[var(--foreground)] tabular-nums">
              {turn.cacheReadTokens !== null ? formatTokens(turn.cacheReadTokens) : "—"}
            </span>
            <span className="mx-1">read /</span>
            <span className="text-[var(--foreground)] tabular-nums">
              {turn.cacheCreateTokens !== null ? formatTokens(turn.cacheCreateTokens) : "—"}
            </span>
            <span className="ml-1">write</span>
          </div>
          <div>
            <span className="text-[var(--foreground)] tabular-nums">
              {turn.gapMs !== null ? `${turn.gapMs}ms` : "—"}
            </span>
            <span className="ml-2">gap</span>
          </div>
        </div>

        {/* LLM messages */}
        <div className="px-4 py-3 border-b border-[var(--border)]" data-testid="turn-detail-messages-section">
          <div className="flex items-center justify-between mb-2 font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wide">
            <span>LLM Messages</span>
            {hasMessages && !messagesEmpty && (
              <span className="tabular-nums" data-testid="turn-detail-messages-count">
                {messages!.length} message{messages!.length === 1 ? "" : "s"}
              </span>
            )}
          </div>

          {!hasMessages && (
            <Block
              data-testid="turn-detail-messages-placeholder"
              kind="turn.message.placeholder"
              status="waiting"
              className="rounded-sm border-[var(--border)]/60 bg-[var(--secondary)]/40 px-3 py-4 font-mono text-[10px] text-[var(--muted-foreground)]"
            >
              Waiting for <code className="text-[var(--foreground)]">turn.complete</code> event — the
              backend checkbox that carries full message bodies + per-message token attribution has not shipped
              yet. Once it does, this section will render system / user / assistant / tool messages for this turn.
            </Block>
          )}

          {messagesEmpty && (
            <Block
              data-testid="turn-detail-messages-empty"
              kind="turn.message.empty"
              status="empty"
              className="rounded-sm border-[var(--border)]/60 bg-[var(--secondary)]/40 px-3 py-4 font-mono text-[10px] text-[var(--muted-foreground)]"
            >
              No messages recorded for this turn.
            </Block>
          )}

          {hasMessages && !messagesEmpty && (
            <div className="space-y-2">
              {messages!.map((msg, idx) => {
                const style = ROLE_STYLES[msg.role]
                return (
                  <Block
                    key={`${msg.role}-${idx}`}
                    data-testid="turn-detail-message"
                    data-role={msg.role}
                    kind="turn.message"
                    status={msg.role}
                    className="rounded-sm border-[var(--border)]/60 bg-[var(--secondary)]/40 p-0"
                  >
                    <div className="flex items-center justify-between px-2 py-1 border-b border-[var(--border)]/40">
                      <span
                        data-testid="turn-detail-message-role"
                        className="font-mono text-[9px] font-semibold px-1.5 py-0.5 rounded"
                        style={{ color: style.color, backgroundColor: style.bg }}
                      >
                        {style.label}
                      </span>
                      <span
                        data-testid="turn-detail-message-tokens"
                        className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums"
                        title="Prompt token contribution for this message (null when unattributed)"
                      >
                        {typeof msg.tokens === "number"
                          ? `${formatTokens(msg.tokens)} tokens`
                          : "— tokens"}
                      </span>
                    </div>
                    {msg.toolName && (
                      <div
                        data-testid="turn-detail-message-tool-name"
                        className="px-2 pt-1 font-mono text-[9px] text-[var(--artifact-purple)]"
                      >
                        tool: {msg.toolName}
                      </div>
                    )}
                    <pre
                      data-testid="turn-detail-message-content"
                      className="px-2 py-2 font-mono text-[10px] text-[var(--foreground)] whitespace-pre-wrap break-words"
                    >
                      {msg.content}
                    </pre>
                  </Block>
                )
              })}
              <div
                data-testid="turn-detail-messages-sum"
                className="text-right font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums pt-1"
              >
                sum across messages: {formatTokens(perMsgTokenSum)} tokens
              </div>
            </div>
          )}
        </div>

        {/* Used tools detail */}
        <div className="px-4 py-3" data-testid="turn-detail-tools-section">
          <div className="flex items-center gap-2 mb-2 font-mono text-[10px] text-[var(--muted-foreground)] uppercase tracking-wide">
            <Wrench size={10} />
            <span>Used Tools</span>
            {turn.toolCallCount !== null && (
              <span className="ml-auto tabular-nums" data-testid="turn-detail-tools-count">
                {turn.toolCallCount} call{turn.toolCallCount === 1 ? "" : "s"}
                {turn.toolFailureCount !== null && turn.toolFailureCount > 0 && (
                  <span className="ml-1 text-[var(--critical-red)] font-semibold">
                    · {turn.toolFailureCount} failed
                  </span>
                )}
              </span>
            )}
          </div>

          {renderedTools.length === 0 && (
            <Block
              data-testid="turn-detail-tools-empty"
              kind="turn.tool.empty"
              status={turn.toolCallCount === 0 ? "empty" : "waiting"}
              className="rounded-sm border-[var(--border)]/60 bg-[var(--secondary)]/40 px-3 py-3 font-mono text-[10px] text-[var(--muted-foreground)]"
            >
              {turn.toolCallCount === 0
                ? "No tools invoked on this turn."
                : hasExplicitTools
                  ? "No tool call details recorded."
                  : "Waiting for turn.complete event — tool call arguments and results will appear here once the backend emits them."}
            </Block>
          )}

          {renderedTools.length > 0 && (
            <div className="space-y-2">
              {renderedTools.map((tc, idx) => (
                <Block
                  key={`${tc.name}-${idx}`}
                  data-testid="turn-detail-tool-call"
                  data-success={tc.success ? "true" : "false"}
                  kind="turn.tool"
                  status={tc.success ? "ok" : "failed"}
                  tone={tc.success ? "success" : "danger"}
                  className="rounded-sm border-[var(--border)]/60 bg-[var(--secondary)]/40 p-0"
                >
                  <div className="flex items-center gap-2 px-2 py-1 border-b border-[var(--border)]/40">
                    {tc.success ? (
                      <CheckCircle2 size={11} className="text-[var(--validation-emerald)]" aria-hidden />
                    ) : (
                      <AlertTriangle size={11} className="text-[var(--critical-red)]" aria-hidden />
                    )}
                    <span
                      data-testid="turn-detail-tool-call-name"
                      className="font-mono text-[10px] text-[var(--foreground)]"
                    >
                      {tc.name}
                    </span>
                    <span
                      data-testid="turn-detail-tool-call-status"
                      className={`ml-auto font-mono text-[9px] ${
                        tc.success
                          ? "text-[var(--validation-emerald)]"
                          : "text-[var(--critical-red)] font-semibold"
                      }`}
                    >
                      {tc.success ? "ok" : "failed"}
                    </span>
                    {typeof tc.durationMs === "number" && (
                      <span className="font-mono text-[9px] text-[var(--muted-foreground)] tabular-nums">
                        {tc.durationMs}ms
                      </span>
                    )}
                  </div>
                  {tc.args !== null && tc.args !== undefined && (
                    <div className="px-2 pt-1 font-mono text-[9px]">
                      <span className="text-[var(--muted-foreground)]">args:</span>
                      <pre
                        data-testid="turn-detail-tool-call-args"
                        className="mt-0.5 text-[var(--foreground)] whitespace-pre-wrap break-words"
                      >
                        {stringifyArgs(tc.args)}
                      </pre>
                    </div>
                  )}
                  {tc.result && (
                    <div className="px-2 py-1 font-mono text-[9px]">
                      <span className="text-[var(--muted-foreground)]">result:</span>
                      <pre
                        data-testid="turn-detail-tool-call-result"
                        className="mt-0.5 text-[var(--foreground)] whitespace-pre-wrap break-words"
                      >
                        {tc.result}
                      </pre>
                    </div>
                  )}
                </Block>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

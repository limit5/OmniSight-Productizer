"use client"

// ZZ.B1 (#304-1, 2026-04-24): per-turn timeline cards — ccxray 招牌 UI.
//
// First checkbox of ZZ.B1: render the five-line card at the top of the
// ORCHESTRATOR AI panel with horizontal-scroll / vertical-stack layout
// modes the operator can toggle. Data source in this initial landing is
// the existing ``turn_metrics`` + ``turn_tool_stats`` SSE stream (shared
// with TokenUsageStats). The dedicated ``turn.complete`` SSE event +
// ``GET /runtime/turns`` history endpoint + drawer with full LLM
// messages are subsequent checkboxes in ZZ.B1 — when they ship, they
// upgrade Line 5's placeholder body and enable historical backfill on
// mount. For now the card renders from what today's backend already
// emits, so the UI is useful from day one instead of gated on the
// backend work.
//
// Ring buffer: last 100 turns kept in component state (LRU by turn
// arrival order). The buffer matches the spec's "前端用 ring buffer 存
// 最近 100 turn" requirement; once ``GET /runtime/turns?limit=50`` lands
// in a later checkbox we'll seed the buffer on mount.

import { useState, useEffect, useRef } from "react"
import {
  Clock,
  ChevronUp,
  ChevronDown,
  Rows3,
  MoveHorizontal,
} from "lucide-react"
import { subscribeEvents } from "@/lib/api"
import { getModelInfo } from "./agent-matrix-wall"

const RING_BUFFER_SIZE = 100
const LINE5_MAX_CHARS = 80

export interface TurnCardData {
  turnNumber: number
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

  useEffect(() => {
    if (externalTurns) {
      setTurns(externalTurns) // eslint-disable-line react-hooks/set-state-in-effect -- sync controlled prop to local state
      return
    }
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
      }
    })
    return () => handle.close()
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
            />
          ))}
        </div>
      )}
    </div>
  )
}

function TurnCard({
  turn,
  anchorMs,
  compact,
}: {
  turn: TurnCardData
  anchorMs: number
  compact: boolean
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
      className={`rounded-md bg-[var(--secondary)] p-2 border border-[var(--border)]/50 ${
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

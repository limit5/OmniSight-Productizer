"use client"

import { useState, useEffect, useRef } from "react"
import {
  ChevronDown,
  ChevronUp,
  Coins,
  TrendingUp,
  BarChart3,
  Zap,
  DollarSign,
  Clock,
  ArrowUpRight,
  ArrowDownRight,
  AlertTriangle
} from "lucide-react"
import { AI_MODEL_INFO, type AIModel } from "./agent-matrix-wall"
import { MetricSparkline } from "./host-device-panel"
import {
  subscribeEvents,
  fetchTokenBurnRate,
  type TokenBurnRatePoint,
  type TokenBurnRateWindow,
} from "@/lib/api"

// ZZ.A2 (#303-2, 2026-04-24): per-model snapshot of the *latest* turn's
// context-window usage. Backend emits ``turn_metrics`` SSE at the end of
// every LLM turn (``backend/agents/llm.py::TokenTrackingCallback``); we
// keep only the last value per model so the per-card Row 3a progress bar
// reflects the most recent turn (lifetime totals would balloon as the
// agent keeps talking and lose the "is this turn near the cap" signal).
// ``contextLimit`` / ``contextUsagePct`` are ``null`` when the YAML has
// no entry for the provider/model pair (Ollama local, OpenRouter pass-
// through, unknown provider) — UI renders "—" for nulls per the same
// NULL-vs-genuine-zero contract ZZ.A1 established for cache fields.
interface ContextSnapshot {
  tokensUsed: number
  contextLimit: number | null
  contextUsagePct: number | null
}

// Token usage data per model
export interface ModelTokenUsage {
  model: AIModel
  inputTokens: number
  outputTokens: number
  totalTokens: number
  cost: number
  requestCount: number
  avgLatency: number // ms
  lastUsed: string
  // ZZ.A1 (#303-2, 2026-04-24): prompt-cache observability. ``null``
  // on pre-ZZ rows (backend legacy payloads that predate the cache
  // columns) so the UI can render "—" to distinguish "no data" from
  // a real zero hit-rate. ZZ.A2 renders the CACHE HIT % + tooltip.
  cacheReadTokens: number | null
  cacheCreateTokens: number | null
  cacheHitRatio: number | null
}

// Empty state — no LLM calls made yet
function emptyUsage(): ModelTokenUsage[] {
  return []
}

// Format large numbers with K/M suffix
function formatTokens(num: number): string {
  if (num >= 1000000) {
    return (num / 1000000).toFixed(2) + "M"
  }
  if (num >= 1000) {
    return (num / 1000).toFixed(1) + "K"
  }
  return num.toString()
}

// Format cost with appropriate precision
function formatCost(cost: number): string {
  if (cost >= 1) {
    return "$" + cost.toFixed(2)
  }
  return "$" + cost.toFixed(3)
}

export interface TokenBudgetInfo {
  budget: number
  usage: number
  ratio: number
  frozen: boolean
  level: string
  warn_threshold: number
  downgrade_threshold: number
  freeze_threshold: number
  fallback_provider: string
  fallback_model: string
}

interface TokenUsageStatsProps {
  className?: string
  externalUsage?: ModelTokenUsage[]
  // 2026-04-21: when the operator has credentials wired for a
  // provider (e.g. Anthropic API key set) but hasn't invoked it yet,
  // ``externalUsage`` is empty for that model — previously the panel
  // simply didn't render a card for it, which looked like the provider
  // was missing entirely (``"I configured Anthropic but the stats only
  // shows gemma4:e4b"``). Passing the configured-providers list lets
  // us synthesise 0-count placeholder cards so every wired model is
  // visible even before the first invocation. Once a call lands, the
  // placeholder is replaced by the real usage row.
  configuredProviders?: { id: string; name: string; models: string[]; configured: boolean }[]
  budgetInfo?: TokenBudgetInfo | null
  onResetFreeze?: () => void
  onUpdateBudget?: (updates: Record<string, number | string>) => void
}

export function TokenUsageStats({ className = "", externalUsage, configuredProviders, budgetInfo, onResetFreeze, onUpdateBudget }: TokenUsageStatsProps) {
  const [expanded, setExpanded] = useState(true)
  const [usageData, setUsageData] = useState<ModelTokenUsage[]>(externalUsage ?? emptyUsage())
  const [selectedModel, setSelectedModel] = useState<AIModel | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [localWarn, setLocalWarn] = useState(budgetInfo?.warn_threshold ?? 0.8)
  const [localDegrade, setLocalDegrade] = useState(budgetInfo?.downgrade_threshold ?? 0.9)

  // Sync local slider values when props change (from polling)
  useEffect(() => {
    if (budgetInfo) {
      setLocalWarn(budgetInfo.warn_threshold)
      setLocalDegrade(budgetInfo.downgrade_threshold)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-sync when threshold values change, not the whole budgetInfo object
  }, [budgetInfo?.warn_threshold, budgetInfo?.downgrade_threshold])

  // Sync from backend when available
  useEffect(() => {
    if (externalUsage && externalUsage.length > 0) {
      setUsageData(externalUsage)
    }
  }, [externalUsage])

  // ZZ.A2 (#303-2, 2026-04-24): subscribe to ``turn_metrics`` SSE so the
  // per-card Row 3a context-window bar reflects the *latest* turn's
  // ``tokens_used / context_limit`` for that model. Keyed by lowercased
  // model id to match the dedup convention used elsewhere in this file
  // (``usedModels`` set, line ~137). Self-contained — no use-engine
  // wiring needed; the shared ``EventSource`` deduplicates underneath.
  const [contextByModel, setContextByModel] = useState<Record<string, ContextSnapshot>>({})
  // ZZ.A3 (#303-3, 2026-04-24): per-turn mini-stats for Row 3 right-
  // aligned "avg gap Xms | tools N / failed M" columns.
  //
  // ``turnHistoryByModel``: rolling buffer of the last HISTORY_DEPTH
  // ``turn_metrics`` events per model (timestamps parsed as epoch ms +
  // each turn's ``latency_ms``). Used to compute:
  //     gap_i = (t[i].ts - t[i-1].ts) - t[i].latency_ms
  // which is the spec's "tool 執行 + event bus 排程 + 等 context gather"
  // time between LLM calls (ZZ.A3 checkbox 1). Average over the buffer
  // yields "avg gap" — needs ≥2 turns to produce a value, otherwise
  // renders "—".
  //
  // ``toolStatsByModel``: latest ``turn_tool_stats`` per model. The SSE
  // event itself carries no model field (summarizer doesn't know which
  // LLM node ran above it), so we attribute each emission to the model
  // of the most-recently-seen ``turn_metrics`` via ``lastMetricsModelRef``.
  // Summarizer fires after ``on_llm_end`` in the same graph run so the
  // ordering holds; with concurrent agents this is a best-effort
  // heuristic — good enough for initial UX, documented here for future
  // tightening (would need backend to add ``model`` to SSETurnToolStats).
  const HISTORY_DEPTH = 10
  const [turnHistoryByModel, setTurnHistoryByModel] = useState<Record<string, { ts: number; latency: number }[]>>({})
  const [toolStatsByModel, setToolStatsByModel] = useState<Record<string, { callCount: number; failureCount: number; failedTools: string[] }>>({})
  const lastMetricsModelRef = useRef<string | null>(null)
  // ZZ.B3 #304-3 checkbox 2 (2026-04-24): burn-rate sparkline + current-
  // rate badge on the Row 1 right side. ``burnWindow`` drives the fetch
  // and the "which tab is selected" highlight; ``burnPoints`` is the
  // 60 s-bucketed series from ``GET /runtime/tokens/burn-rate``. Hover
  // over the sparkline reveals the 15m / 1h / 24h tab row — click a tab
  // to re-fetch for that window. Empty points → badge renders "$—/hr",
  // sparkline degrades to MetricSparkline's <2-point empty state.
  const [burnWindow, setBurnWindow] = useState<TokenBurnRateWindow>("1h")
  const [burnPoints, setBurnPoints] = useState<TokenBurnRatePoint[]>([])
  const [burnHover, setBurnHover] = useState(false)
  useEffect(() => {
    const handle = subscribeEvents((event) => {
      if (event.event === "turn_metrics") {
        const d = event.data
        if (!d.model) return
        const modelKey = d.model.toLowerCase()
        setContextByModel(prev => ({
          ...prev,
          [modelKey]: {
            tokensUsed: d.tokens_used,
            contextLimit: d.context_limit,
            contextUsagePct: d.context_usage_pct,
          },
        }))
        const parsed = d.timestamp ? Date.parse(d.timestamp) : NaN
        const ts = Number.isFinite(parsed) ? parsed : Date.now()
        setTurnHistoryByModel(prev => {
          const prior = prev[modelKey] ?? []
          const next = [...prior, { ts, latency: d.latency_ms ?? 0 }].slice(-HISTORY_DEPTH)
          return { ...prev, [modelKey]: next }
        })
        lastMetricsModelRef.current = modelKey
      } else if (event.event === "turn_tool_stats") {
        const d = event.data
        const modelKey = lastMetricsModelRef.current
        if (!modelKey) return
        setToolStatsByModel(prev => ({
          ...prev,
          [modelKey]: {
            callCount: d.tool_call_count,
            failureCount: d.tool_failure_count,
            failedTools: d.failed_tools ?? [],
          },
        }))
      }
    })
    return () => handle.close()
  }, [])

  // ZZ.B3 #304-3 checkbox 2: fetch the burn-rate series for the current
  // ``burnWindow`` on mount and whenever the operator flips tabs. Poll
  // every 30 s so the sparkline + ``$/hr`` badge track the bucket stream
  // without hammering the endpoint (one 60 s bucket → 2 refresh chances
  // before it's stale). ``cancelled`` guards against the component
  // unmounting mid-fetch — same pattern ``<TurnTimeline>`` uses for
  // ``fetchTurnHistory``.
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const resp = await fetchTokenBurnRate(burnWindow)
        if (!cancelled) setBurnPoints(resp.points ?? [])
      } catch {
        // Endpoint down or tenant has zero turns — treat as empty
        // series so the sparkline falls back to its <2-point empty
        // state and the badge renders "$—/hr". Core chat flow is
        // unaffected, so a silent degrade is the right policy here.
        if (!cancelled) setBurnPoints([])
      }
    }
    load()
    const interval = setInterval(load, 30_000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [burnWindow])

  // Calculate totals FROM REAL USAGE ONLY (exclude placeholder cards).
  const totals = usageData.reduce((acc, item) => ({
    inputTokens: acc.inputTokens + item.inputTokens,
    outputTokens: acc.outputTokens + item.outputTokens,
    totalTokens: acc.totalTokens + item.totalTokens,
    cost: acc.cost + item.cost,
    requestCount: acc.requestCount + item.requestCount,
  }), { inputTokens: 0, outputTokens: 0, totalTokens: 0, cost: 0, requestCount: 0 })

  // 2026-04-21: Merge configured-but-unused provider models as 0-count
  // placeholder cards so the panel shows every wired credential, not
  // just the ones that happen to have been invoked. Keyed by model
  // string (dedup case-insensitively against the real usage rows).
  const usedModels = new Set(usageData.map(u => u.model.toLowerCase()))
  const placeholderRows: ModelTokenUsage[] = []
  if (configuredProviders) {
    for (const p of configuredProviders) {
      if (!p.configured) continue
      for (const m of p.models) {
        if (!m || usedModels.has(m.toLowerCase())) continue
        placeholderRows.push({
          model: m,
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 0,
          cost: 0,
          requestCount: 0,
          avgLatency: 0,
          lastUsed: "",
          // Synthesised placeholder: no backend row, treat as pre-ZZ
          // "no data" (null) so the cache UI renders "—" instead of
          // a misleading 0% hit rate.
          cacheReadTokens: null,
          cacheCreateTokens: null,
          cacheHitRatio: null,
        })
      }
    }
  }
  // Sort real usage by total tokens DESC; placeholders grouped after.
  const sortedData = [
    ...[...usageData].sort((a, b) => b.totalTokens - a.totalTokens),
    ...placeholderRows,
  ]

  // ZZ.A2 (#303-2, 2026-04-24): card-top warning icon. If ANY recent
  // ``turn_metrics`` snapshot has ``context_usage_pct >= 90`` we surface
  // an AlertTriangle next to the "TOKEN USAGE" header so the operator
  // sees the signal even when the panel is collapsed. The per-card Row
  // 3a bar already pulses red at >=90 — the header icon is the
  // always-visible analogue for when the panel has been folded up.
  // NULL degradation: ``contextUsagePct === null`` (unknown limit /
  // Ollama without env override / no SSE yet) is explicitly excluded
  // so "no data" never fires the alarm — same NULL-vs-genuine-zero
  // contract ZZ.A1 established for cache fields.
  const criticalContextModels = Object.entries(contextByModel)
    .filter(([, snap]) => snap.contextUsagePct !== null && snap.contextUsagePct !== undefined && snap.contextUsagePct >= 90)
    .map(([model]) => model)
  const hasCriticalContext = criticalContextModels.length > 0

  return (
    <div className={`border-b border-[var(--border)] ${className}`}>
      {/* Header (ZZ.B3 #304-3 checkbox 2 (2026-04-24): the outer wrapper
          is a <div> — NOT a <button> — because the Row 1 right side now
          hosts a nested <BurnRateBadge> with its own click-capable tabs
          (15m / 1h / 24h). Nesting <button> inside <button> is invalid
          HTML and gets flattened by the browser, which would make the
          sparkline group collapse/expand the whole card on every click.
          The expand toggle moved into a child <button> covering only the
          left block (icon + title + totals); the burn-rate group sits as
          a sibling so its hover/click state never bubbles up. */}
      <div className="w-full px-4 py-2 flex items-center justify-between text-xs font-mono text-[var(--muted-foreground)] hover:bg-[var(--secondary)]/50 transition-colors">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-2 flex-1 min-w-0 text-left"
          aria-expanded={expanded}
          data-testid="token-usage-expand-toggle"
        >
          <Coins size={12} className="text-[var(--hardware-orange)] shrink-0" />
          <span>TOKEN USAGE</span>
          {hasCriticalContext && (
            <span
              className="inline-flex items-center"
              title={`Context 接近上限，agent 可能 truncate — ${criticalContextModels.join(", ")}`}
              aria-label="Context window approaching limit — agent may be truncated"
              data-testid="context-critical-warning"
            >
              <AlertTriangle
                size={12}
                className="text-[var(--critical-red)] animate-pulse"
              />
            </span>
          )}
          <span className="ml-auto flex items-center gap-3 shrink-0">
            <span className="text-[var(--validation-emerald)]">{formatTokens(totals.totalTokens)} tokens</span>
            <span className="text-[var(--hardware-orange)]">{formatCost(totals.cost)}</span>
          </span>
        </button>
        {/* Burn-rate sparkline + current-rate badge (ZZ.B3 #304-3
            checkbox 2, 2026-04-24). Hover surfaces the 15m / 1h / 24h
            tab row so the operator can flip windows without leaving
            Row 1. The outer group stops click propagation so tab clicks
            don't bubble into the expand toggle above it. */}
        <div
          className="ml-3 flex items-center gap-2 shrink-0 relative"
          onMouseEnter={() => setBurnHover(true)}
          onMouseLeave={() => setBurnHover(false)}
          onFocus={() => setBurnHover(true)}
          onBlur={(e) => {
            // Keep tabs visible while focus stays inside the group —
            // only collapse when focus leaves it entirely.
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
              setBurnHover(false)
            }
          }}
          data-testid="burn-rate-group"
          data-burn-hover={burnHover ? "true" : "false"}
        >
          {(() => {
            // Latest bucket drives the "current rate" badge; MetricSparkline
            // renders the full series. Empty series → badge "$—/hr" and
            // sparkline's <2-point empty state takes over.
            const hasPoints = burnPoints.length > 0
            const latest = hasPoints ? burnPoints[burnPoints.length - 1] : null
            const currentRate = latest?.cost_per_hour ?? 0
            // Format: < $1 → 3 decimal places (so "$0.12/hr" is readable);
            // >= $1 → 2 decimal places ("$12.50/hr"). Matches the
            // ``formatCost`` helper's reasoning for readability.
            const rateLabel = latest === null
              ? "$—/hr"
              : currentRate >= 1
                ? `$${currentRate.toFixed(2)}/hr`
                : `$${currentRate.toFixed(3)}/hr`
            const rateTooltip = latest === null
              ? `No burn-rate data for ${burnWindow} yet`
              : `Current burn rate over the latest 60 s bucket (window: ${burnWindow})`
            return (
              <>
                <MetricSparkline
                  values={burnPoints.map((p) => p.cost_per_hour)}
                  color="var(--hardware-orange)"
                  domainMax={null}
                  width={64}
                  height={14}
                  testId="burn-rate-sparkline"
                />
                <span
                  className="font-mono text-[10px] font-semibold text-[var(--hardware-orange)] px-1.5 py-0.5 rounded bg-[var(--hardware-orange)]/10"
                  title={rateTooltip}
                  data-testid="burn-rate-badge"
                >
                  {rateLabel}
                </span>
              </>
            )
          })()}
          {burnHover && (
            <div
              className="absolute top-full right-0 mt-1 flex items-center gap-1 p-1 rounded bg-[var(--secondary)] border border-[var(--border)] shadow-lg z-10"
              data-testid="burn-rate-tabs"
            >
              {(["15m", "1h", "24h"] as TokenBurnRateWindow[]).map((w) => (
                <button
                  key={w}
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    setBurnWindow(w)
                  }}
                  className={`px-1.5 py-0.5 rounded font-mono text-[10px] transition-colors ${
                    burnWindow === w
                      ? "bg-[var(--hardware-orange)]/20 text-[var(--hardware-orange)]"
                      : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                  }`}
                  data-testid={`burn-rate-tab-${w}`}
                  aria-pressed={burnWindow === w}
                >
                  {w}
                </button>
              ))}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="ml-2 shrink-0 text-[var(--muted-foreground)]"
          aria-label={expanded ? "Collapse token usage" : "Expand token usage"}
          data-testid="token-usage-expand-chevron"
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </button>
      </div>
      
      {expanded && (
        <div className="px-3 pb-3">
          {/* Budget Bar */}
          {budgetInfo && (
            <div className="mb-3">
              {/* Frozen Banner */}
              {budgetInfo.frozen && (
                <div className="mb-2 p-2 rounded bg-[var(--critical-red)]/20 border border-[var(--critical-red)]/50 flex items-center justify-between animate-pulse">
                  <span className="font-mono text-[10px] text-[var(--critical-red)] font-semibold">
                    TOKEN BUDGET EXHAUSTED — LLM FROZEN
                  </span>
                  {onResetFreeze && (
                    <button
                      onClick={onResetFreeze}
                      className="px-2 py-0.5 rounded text-[9px] font-mono font-semibold bg-[var(--critical-red)]/30 hover:bg-[var(--critical-red)]/50 text-[var(--critical-red)] transition-colors"
                    >
                      RESET
                    </button>
                  )}
                </div>
              )}
              {/* Budget Progress */}
              <div className="flex items-center justify-between mb-1">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">DAILY BUDGET</span>
                <span className={`font-mono text-[10px] font-semibold ${
                  budgetInfo.level === "frozen" ? "text-[var(--critical-red)]" :
                  budgetInfo.level === "downgrade" ? "text-[var(--hardware-orange)]" :
                  budgetInfo.level === "warn" ? "text-yellow-500" :
                  "text-[var(--validation-emerald)]"
                }`}>
                  ${budgetInfo.usage.toFixed(4)} / {budgetInfo.budget > 0 ? `$${budgetInfo.budget.toFixed(2)}` : "Unlimited"}
                </span>
              </div>
              {budgetInfo.budget > 0 && (
                <div className="h-2.5 rounded-full bg-[var(--border)] overflow-hidden relative">
                  {/* Threshold markers */}
                  <div className="absolute top-0 bottom-0 border-r border-yellow-500/50" style={{ left: `${budgetInfo.warn_threshold * 100}%` }} />
                  <div className="absolute top-0 bottom-0 border-r border-[var(--hardware-orange)]/50" style={{ left: `${budgetInfo.downgrade_threshold * 100}%` }} />
                  {/* Fill */}
                  <div
                    className={`h-full rounded-full transition-all duration-500 ${
                      budgetInfo.level === "frozen" ? "bg-[var(--critical-red)]" :
                      budgetInfo.level === "downgrade" ? "bg-[var(--hardware-orange)]" :
                      budgetInfo.level === "warn" ? "bg-yellow-500" :
                      "bg-[var(--validation-emerald)]"
                    }`}
                    style={{ width: `${Math.min(budgetInfo.ratio * 100, 100)}%` }}
                  />
                </div>
              )}
              <div className="flex items-center justify-between mt-1">
                <span className="font-mono text-[9px] text-[var(--muted-foreground)]">
                  {budgetInfo.budget > 0 ? `${(budgetInfo.ratio * 100).toFixed(1)}% used` : "No limit set"}
                </span>
                <button
                  onClick={() => setShowSettings(!showSettings)}
                  className="font-mono text-[9px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
                >
                  {showSettings ? "▲ HIDE SETTINGS" : "▼ SETTINGS"}
                </button>
              </div>
              {/* Settings Panel */}
              {showSettings && onUpdateBudget && (
                <div className="mt-2 p-2 rounded bg-[var(--secondary)] space-y-2.5 overflow-hidden">
                  {/* Budget input — preset buttons */}
                  <div>
                    <label className="font-mono text-[9px] text-[var(--muted-foreground)] mb-1 block">$/day</label>
                    <div className="flex flex-wrap gap-1">
                      {[0, 1, 5, 10, 50, 100].map(val => (
                        <button
                          key={val}
                          onClick={() => onUpdateBudget({ budget: val })}
                          className={`px-2 py-0.5 rounded font-mono text-[9px] transition-colors ${
                            budgetInfo.budget === val
                              ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]"
                              : "bg-[var(--background)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                          }`}
                        >
                          {val === 0 ? "∞" : `$${val}`}
                        </button>
                      ))}
                    </div>
                  </div>
                  {/* Threshold sliders — local state for smooth drag, commit on release */}
                  <div className="flex items-center gap-1.5">
                    <label className="font-mono text-[9px] text-yellow-500 w-16 shrink-0">Warn</label>
                    <input
                      type="range" min="0.5" max={localDegrade} step="0.05"
                      value={localWarn}
                      className="flex-1 h-1 accent-yellow-500 min-w-0"
                      onChange={(e) => setLocalWarn(Math.min(parseFloat(e.target.value), localDegrade))}
                      onMouseUp={() => onUpdateBudget({ warn_threshold: localWarn })}
                      onTouchEnd={() => onUpdateBudget({ warn_threshold: localWarn })}
                    />
                    <span className="font-mono text-[9px] text-[var(--muted-foreground)] w-7 text-right shrink-0">{(localWarn * 100).toFixed(0)}%</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <label className="font-mono text-[9px] text-[var(--hardware-orange)] w-16 shrink-0">Degrade</label>
                    <input
                      type="range" min={localWarn} max="1" step="0.05"
                      value={localDegrade}
                      className="flex-1 h-1 accent-[var(--hardware-orange)] min-w-0"
                      onChange={(e) => setLocalDegrade(Math.max(parseFloat(e.target.value), localWarn))}
                      onMouseUp={() => onUpdateBudget({ downgrade_threshold: localDegrade })}
                      onTouchEnd={() => onUpdateBudget({ downgrade_threshold: localDegrade })}
                    />
                    <span className="font-mono text-[9px] text-[var(--muted-foreground)] w-7 text-right shrink-0">{(localDegrade * 100).toFixed(0)}%</span>
                  </div>
                  {/* Fallback info */}
                  <div className="flex items-center gap-1.5 pt-1 border-t border-[var(--border)]/50">
                    <label className="font-mono text-[9px] text-[var(--muted-foreground)] w-16 shrink-0">Fallback</label>
                    <span className="font-mono text-[9px] text-[var(--foreground)] truncate">{budgetInfo.fallback_provider} / {budgetInfo.fallback_model}</span>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Summary Stats - Vertical Stack */}
          <div className="space-y-2 mb-3">
            {/* Total Tokens */}
            <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <BarChart3 size={14} className="text-[var(--neural-blue)] shrink-0" />
                  <span className="font-mono text-xs text-[var(--muted-foreground)]">Total Tokens</span>
                </div>
                <p className="font-mono text-sm font-semibold text-[var(--foreground)]">{formatTokens(totals.totalTokens)}</p>
              </div>
              <div className="flex items-center justify-between mt-1.5 pl-6">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Input: <span className="text-[var(--validation-emerald)]">{formatTokens(totals.inputTokens)}</span>
                </span>
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Output: <span className="text-[var(--neural-blue)]">{formatTokens(totals.outputTokens)}</span>
                </span>
              </div>
            </div>
            
            {/* Total Cost */}
            <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <DollarSign size={14} className="text-[var(--hardware-orange)] shrink-0" />
                  <span className="font-mono text-xs text-[var(--muted-foreground)]">Total Cost</span>
                </div>
                <p className="font-mono text-sm font-semibold text-[var(--hardware-orange)]">{formatCost(totals.cost)}</p>
              </div>
              <div className="flex items-center justify-between mt-1.5 pl-6">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                  Avg per Request: <span className="text-[var(--foreground)]">{formatCost(totals.cost / totals.requestCount)}</span>
                </span>
              </div>
            </div>
            
            {/* Requests + Active Models Row */}
            <div className="grid grid-cols-2 gap-2">
              <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Zap size={14} className="text-[var(--artifact-purple)] shrink-0" />
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Requests</span>
                  </div>
                  <p className="font-mono text-sm font-semibold text-[var(--foreground)]">{totals.requestCount}</p>
                </div>
              </div>
              <div className="p-2.5 rounded-lg bg-[var(--secondary)]">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <TrendingUp size={14} className="text-[var(--validation-emerald)] shrink-0" />
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Models</span>
                  </div>
                  <p className="font-mono text-sm font-semibold text-[var(--foreground)]">{usageData.length}</p>
                </div>
              </div>
            </div>
          </div>
          
          {/* Per-Model Usage */}
          <div className="space-y-2">
            {sortedData.map(item => {
              const modelInfo = AI_MODEL_INFO[item.model]
              const isSelected = selectedModel === item.model
              // Guard against 0/0 NaN for placeholder rows (configured
              // provider, no usage yet). Treat no-usage as 0%.
              const usagePercent = totals.totalTokens > 0
                ? (item.totalTokens / totals.totalTokens) * 100
                : 0
              const isPlaceholder = item.requestCount === 0 && item.totalTokens === 0

              return (
                <button
                  key={item.model}
                  onClick={() => setSelectedModel(isSelected ? null : item.model)}
                  className={`w-full text-left p-3 rounded-lg transition-all ${
                    isSelected
                      ? "bg-[var(--artifact-purple)]/20 ring-1 ring-[var(--artifact-purple)]"
                      : isPlaceholder
                        ? "bg-[var(--secondary)]/40 hover:bg-[var(--secondary)]/60 opacity-70"
                        : "bg-[var(--secondary)] hover:bg-[var(--secondary)]/80"
                  }`}
                >
                  {/* Row 1: Model Name + Provider + Cost
                      2026-04-22: ``flex-wrap`` + ``gap-y-1`` so the
                      cost badge wraps onto a second line ONLY when
                      the model name is long enough to force it
                      (e.g. OpenRouter's ``nvidia/llama-3.1-nemotron-
                      ultra-253b``). Model name stays single-line
                      by default — ``break-words`` only breaks when
                      the word itself can't fit on one line, so
                      short names still render inline with the cost.
                      Cost has ``shrink-0`` + ``ml-auto`` so when
                      it DOES stay on row 1 it's pinned to the
                      right edge, and when it wraps to row 2 it
                      sits flush-left under the model block. */}
                  <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 mb-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <div
                        className="w-3 h-3 rounded-full shrink-0"
                        style={{ backgroundColor: modelInfo.color }}
                      />
                      <span className="font-mono text-xs font-medium text-[var(--foreground)] break-words">
                        {modelInfo.label}
                      </span>
                      {isPlaceholder && (
                        <span
                          className="font-mono text-[8px] px-1 py-0.5 rounded border shrink-0"
                          style={{
                            borderColor: `color-mix(in srgb, ${modelInfo.color} 40%, transparent)`,
                            color: modelInfo.color,
                          }}
                          title="Provider is wired up but no request has landed yet"
                        >
                          READY
                        </span>
                      )}
                    </div>
                    <span
                      className="font-mono text-xs font-semibold px-2 py-0.5 rounded shrink-0"
                      style={{
                        backgroundColor: `color-mix(in srgb, ${modelInfo.color} 20%, transparent)`,
                        color: modelInfo.color
                      }}
                    >
                      {formatCost(item.cost)}
                    </span>
                  </div>

                  {/* Row 2: Provider + Usage Percentage — same
                      flex-wrap treatment so "Provider: <name>" and
                      "<n>% of total usage" can stack vertically on
                      narrow cards. */}
                  <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5 mb-2">
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] min-w-0 break-words">
                      Provider: {modelInfo.provider}
                    </span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] shrink-0">
                      {usagePercent.toFixed(1)}% of total usage
                    </span>
                  </div>
                  
                  {/* Row 3: Usage Bar */}
                  <div className="h-2 rounded-full bg-[var(--border)] overflow-hidden mb-1.5">
                    <div
                      className="h-full rounded-full transition-all duration-500"
                      style={{
                        width: `${usagePercent}%`,
                        backgroundColor: modelInfo.color
                      }}
                    />
                  </div>

                  {/* Row 3 mini-stats (ZZ.A3 #303-3, 2026-04-24):
                      right-aligned "avg gap Xms" + "tools N / failed M"
                      companions to the Row 3 usage bar. ``avg gap`` is
                      averaged across up to HISTORY_DEPTH recent turns
                      of this model (see turn_metrics subscriber above);
                      needs ≥2 turns to compute, otherwise renders "—".
                      ``tools / failed`` comes from the latest
                      turn_tool_stats attributed to this model — the
                      "failed N" digit goes red + bold when ``N > 0`` so
                      a glance at the card reveals retry-loop turns.
                      When no turn_tool_stats has landed for this model
                      yet, render "tools — / failed —" muted. */}
                  {(() => {
                    const modelKey = item.model.toLowerCase()
                    const hist = turnHistoryByModel[modelKey] ?? []
                    const gaps: number[] = []
                    for (let i = 1; i < hist.length; i++) {
                      const delta = hist[i].ts - hist[i - 1].ts
                      const g = delta - hist[i].latency
                      if (Number.isFinite(g) && g >= 0) gaps.push(g)
                    }
                    const avgGap = gaps.length > 0
                      ? Math.round(gaps.reduce((a, b) => a + b, 0) / gaps.length)
                      : null
                    const tstats = toolStatsByModel[modelKey]
                    const hasToolStats = !!tstats
                    const failureCount = tstats?.failureCount ?? 0
                    const callCount = tstats?.callCount ?? 0
                    const failureRed = hasToolStats && failureCount > 0
                    // ZZ.A3 #303-3 (2026-04-24) checkbox 4: p95 outlier
                    // highlight. Compute p95 from *prior* turns only
                    // (exclude the latest) so ``latest > p95`` is a
                    // non-tautological outlier test — otherwise the
                    // latest turn is always at-or-below p95 of the set
                    // it belongs to, and the check degenerates into
                    // ``latest > max - epsilon``. Require ≥4 prior
                    // samples for the baseline to be meaningful (with
                    // HISTORY_DEPTH=10, the signal first lights up on
                    // turn 5). Nearest-rank p95 (``ceil(n * 0.95) - 1``)
                    // — for small n (<20) this approximates max(prior),
                    // which is the right "exceeds 95% of recent" signal
                    // at this sample depth.
                    const latestLatency = hist.length > 0
                      ? hist[hist.length - 1].latency
                      : null
                    const priorLatencies = hist
                      .slice(0, -1)
                      .map((h) => h.latency)
                      .filter((l) => Number.isFinite(l) && l > 0)
                    const MIN_PRIOR = 4
                    let p95Latency: number | null = null
                    let isOutlier = false
                    if (
                      priorLatencies.length >= MIN_PRIOR &&
                      latestLatency !== null &&
                      latestLatency > 0
                    ) {
                      const sorted = [...priorLatencies].sort((a, b) => a - b)
                      const idx = Math.min(
                        sorted.length - 1,
                        Math.max(0, Math.ceil(sorted.length * 0.95) - 1),
                      )
                      p95Latency = sorted[idx]
                      isOutlier = latestLatency > p95Latency
                    }
                    return (
                      <div
                        className="flex items-center justify-end gap-2 mb-3"
                        data-testid="turn-mini-stats"
                      >
                        {isOutlier && (
                          <span
                            className="inline-flex items-center gap-0.5 font-mono text-[9px] font-semibold text-[var(--hardware-orange)] animate-pulse"
                            title={`這 turn 異常慢 — latency ${latestLatency}ms 超過 p95 (${p95Latency}ms) of 最近 ${priorLatencies.length} turns`}
                            aria-label="This turn is abnormally slow — latency exceeds p95 of recent turns"
                            data-testid="turn-p95-outlier"
                          >
                            <AlertTriangle size={10} />
                            SLOW
                          </span>
                        )}
                        <span
                          className="font-mono text-[9px] text-[var(--muted-foreground)]"
                          title="Average gap between LLM turns (tool exec + event bus + context gather)"
                          data-testid="turn-avg-gap"
                        >
                          avg gap {avgGap !== null ? `${avgGap}ms` : "—"}
                        </span>
                        <span className="font-mono text-[9px] text-[var(--muted-foreground)]/60">|</span>
                        <span
                          className="font-mono text-[9px] text-[var(--muted-foreground)]"
                          title={
                            hasToolStats
                              ? failureCount > 0
                                ? `tools ${callCount} / failed ${failureCount} — ${(tstats?.failedTools ?? []).join(", ") || "(no tool names reported)"}`
                                : `tools ${callCount} / failed ${failureCount}`
                              : "no turn_tool_stats seen for this model yet"
                          }
                          data-testid="turn-tool-count"
                        >
                          tools {hasToolStats ? callCount : "—"} / failed{" "}
                          <span
                            className={
                              failureRed
                                ? "text-[var(--critical-red)] font-semibold"
                                : ""
                            }
                            data-testid="turn-failed-count"
                          >
                            {hasToolStats ? failureCount : "—"}
                          </span>
                        </span>
                      </div>
                    )
                  })()}

                  {/* Row 3a (context window): per-turn ``tokens_used /
                      context_limit`` progress bar. ZZ.A2 #303-2 (2026-
                      04-24). Bands per spec: <50 green / 50-75 yellow /
                      75-90 orange / >90 red+pulse. Boundary semantics:
                      strict ``<`` upper / inclusive ``>=`` lower so 50.0
                      lands on yellow, 75.0 on orange, 90.0 on red — same
                      "right edge moves you up a band" reading the cache
                      bar uses. Data source is the latest ``turn_metrics``
                      SSE for this model (snapshot, not lifetime — a long
                      conversation otherwise saturates the bar trivially
                      and loses the "is THIS turn near the cap" signal).
                      NULL degradation: no SSE seen yet, or backend YAML
                      has no entry for the provider/model pair (Ollama,
                      OpenRouter, unknown) → render "—" + empty rail in
                      muted-foreground, distinct from a real 0%. */}
                  {(() => {
                    const ctx = contextByModel[item.model.toLowerCase()]
                    const hasCtx = !!ctx && ctx.contextUsagePct !== null && ctx.contextUsagePct !== undefined
                    const hasLimit = !!ctx && ctx.contextLimit !== null && ctx.contextLimit !== undefined
                    const pct = hasCtx ? (ctx!.contextUsagePct as number) : 0
                    const tokensUsed = ctx?.tokensUsed ?? 0
                    const limit = hasLimit ? (ctx!.contextLimit as number) : 0
                    const ctxColor = !hasCtx
                      ? "var(--muted-foreground)"
                      : pct < 50
                        ? "var(--validation-emerald)"
                        : pct < 75
                          ? "#eab308"
                          : pct < 90
                            ? "var(--hardware-orange)"
                            : "var(--critical-red)"
                    const isCritical = hasCtx && pct >= 90
                    // Hover tooltip — "123k / 200k tokens" per spec.
                    // ``hasLimit`` may be true with ``hasCtx`` true (the
                    // common ZZ.A2 happy path); when ``hasLimit`` is
                    // false we still want to show the raw token count
                    // for the latest turn so operators see the model
                    // is at least talking. ``no context data`` only
                    // fires when no turn_metrics SSE has landed yet
                    // (or this card is a configured-provider placeholder).
                    const ctxTooltip = !ctx
                      ? "no context data — no turn_metrics seen yet"
                      : hasLimit
                        ? `${formatTokens(tokensUsed)} / ${formatTokens(limit)} tokens`
                        : `${formatTokens(tokensUsed)} tokens used (context limit unknown for this provider/model)`
                    return (
                      <div className="mb-3" data-testid="context-usage-section">
                        <div className="flex items-center justify-between mb-1">
                          <span className="font-mono text-[10px] text-[var(--muted-foreground)] tracking-wider">
                            CONTEXT
                          </span>
                          <span
                            className="font-mono text-[10px] font-semibold"
                            style={{ color: ctxColor }}
                            data-testid="context-usage-pct"
                          >
                            {hasCtx ? `${pct.toFixed(0)}%` : "—"}
                          </span>
                        </div>
                        <div
                          className="h-1.5 rounded-full bg-[var(--border)] overflow-hidden"
                          title={ctxTooltip}
                          data-testid="context-usage-bar-rail"
                        >
                          <div
                            className={`h-full rounded-full transition-all duration-500${isCritical ? " animate-pulse" : ""}`}
                            style={{
                              width: hasCtx ? `${Math.min(pct, 100)}%` : "0%",
                              backgroundColor: ctxColor,
                            }}
                            data-testid="context-usage-bar"
                          />
                        </div>
                      </div>
                    )
                  })()}

                  {/* Row 3b (cache): CACHE HIT ratio + CACHE WRITE bars.
                      ZZ.A1 #303-1 (2026-04-24): prompt-cache observability.
                      Hit ratio band: green > 50 / yellow 20-50 / red < 20.
                      NULL on pre-ZZ rows (legacy payloads) or synthesised
                      placeholder cards → render "—" with an empty rail so
                      "no data" stays visually distinct from a real 0% hit
                      rate. Tooltip on each bar shows the raw read/write
                      counts so operators can tell apart "0% because nothing
                      cached" from "0% because this turn was all writes". */}
                  {(() => {
                    const ratio = item.cacheHitRatio
                    const read = item.cacheReadTokens
                    const write = item.cacheCreateTokens
                    const hasRatio = ratio !== null && ratio !== undefined
                    const hasRead = read !== null && read !== undefined
                    const hasWrite = write !== null && write !== undefined
                    const hasAny = hasRatio || hasRead || hasWrite
                    const ratioPct = hasRatio ? (ratio as number) * 100 : 0
                    // Band thresholds per ZZ.A1 spec (#303-1): > 50 green,
                    // 20-50 yellow, < 20 red. Boundary semantics: 50.0 is
                    // yellow (strict >), 20.0 is yellow (>= 20 inclusive),
                    // matches "green > 50 / yellow 20-50 / red < 20".
                    const ratioColor = !hasRatio
                      ? "var(--muted-foreground)"
                      : ratioPct > 50
                        ? "var(--validation-emerald)"
                        : ratioPct >= 20
                          ? "#eab308"
                          : "var(--critical-red)"
                    // CACHE WRITE bar: % of total prompt traffic that was
                    // new cache-creation (i.e. "overhead paid today so a
                    // future turn can hit"). Denominator is input + read +
                    // write so the bar caps at 100 and degrades cleanly
                    // when the provider response had no cache fields.
                    const writeDenom =
                      (item.inputTokens || 0) +
                      (hasRead ? (read as number) : 0) +
                      (hasWrite ? (write as number) : 0)
                    const writePct =
                      hasWrite && writeDenom > 0
                        ? ((write as number) / writeDenom) * 100
                        : 0
                    const tooltipText = hasAny
                      ? `read: ${hasRead ? formatTokens(read as number) : "—"} / write: ${hasWrite ? formatTokens(write as number) : "—"}`
                      : "no cache data"
                    // Raw numbers go on the container `title` so a hover
                    // anywhere on the block surfaces the exact counts
                    // ("raw 數字放 hover" per spec). Per-bar `title` adds
                    // the K-formatted summary so quick scans don't need
                    // to parse 7-digit integers.
                    const rawTitle = hasAny
                      ? `cache_read_tokens=${hasRead ? (read as number).toLocaleString() : "null"} cache_create_tokens=${hasWrite ? (write as number).toLocaleString() : "null"} cache_hit_ratio=${hasRatio ? (ratio as number).toFixed(4) : "null"}`
                      : "no cache data (pre-ZZ row or placeholder)"
                    return (
                      <div
                        className="mb-3"
                        title={rawTitle}
                        data-testid="cache-hit-section"
                      >
                        {/* CACHE HIT bar (primary, colour-coded) */}
                        <div className="flex items-center justify-between mb-1">
                          <span className="font-mono text-[10px] text-[var(--muted-foreground)] tracking-wider">
                            CACHE HIT
                          </span>
                          <span
                            className="font-mono text-[10px] font-semibold"
                            style={{ color: ratioColor }}
                            data-testid="cache-hit-pct"
                          >
                            {hasRatio ? `${ratioPct.toFixed(0)}%` : "—"}
                          </span>
                        </div>
                        <div
                          className="h-1.5 rounded-full bg-[var(--border)] overflow-hidden"
                          title={tooltipText}
                          data-testid="cache-hit-bar-rail"
                        >
                          <div
                            className="h-full rounded-full transition-all duration-500"
                            style={{
                              width: hasRatio ? `${ratioPct}%` : "0%",
                              backgroundColor: ratioColor,
                            }}
                            data-testid="cache-hit-bar"
                          />
                        </div>
                        {/* CACHE WRITE bar (secondary, neutral) */}
                        <div className="flex items-center justify-between mt-1.5 mb-1">
                          <span className="font-mono text-[10px] text-[var(--muted-foreground)] tracking-wider">
                            CACHE WRITE
                          </span>
                          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                            {hasWrite ? formatTokens(write as number) : "—"}
                          </span>
                        </div>
                        <div
                          className="h-1.5 rounded-full bg-[var(--border)] overflow-hidden"
                          title={tooltipText}
                          data-testid="cache-write-bar-rail"
                        >
                          <div
                            className="h-full rounded-full bg-[var(--hardware-orange)]/70 transition-all duration-500"
                            style={{
                              width: hasWrite ? `${Math.min(writePct, 100)}%` : "0%",
                            }}
                            data-testid="cache-write-bar"
                          />
                        </div>
                      </div>
                    )
                  })()}

                  {/* Row 4: Input/Output Tokens — flex-wrap so the
                      output cluster drops to a second line on
                      narrow cards when token counts are big
                      ("1.2M" vs "1,234,567"). Each cluster is
                      ``shrink-0`` so its inner label + number
                      never break mid-group. */}
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 mb-2">
                    <div className="flex items-center gap-1.5 shrink-0">
                      <ArrowDownRight size={12} className="text-[var(--validation-emerald)]" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Input Tokens:</span>
                      <span className="font-mono text-[11px] font-medium text-[var(--validation-emerald)]">
                        {formatTokens(item.inputTokens)}
                      </span>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <ArrowUpRight size={12} className="text-[var(--neural-blue)]" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Output Tokens:</span>
                      <span className="font-mono text-[11px] font-medium text-[var(--neural-blue)]">
                        {formatTokens(item.outputTokens)}
                      </span>
                    </div>
                  </div>
                  
                  {/* Row 5: Total Tokens */}
                  <div className="flex items-center justify-between mb-2 pb-2 border-b border-[var(--border)]/50">
                    <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Total Tokens:</span>
                    <span className="font-mono text-[11px] font-medium text-[var(--foreground)]">
                      {formatTokens(item.totalTokens)}
                    </span>
                  </div>
                  
                  {/* Row 6: Request Count */}
                  <div className="flex items-center justify-between mb-1.5">
                    <div className="flex items-center gap-1.5">
                      <Zap size={11} className="text-[var(--artifact-purple)] shrink-0" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Requests:</span>
                    </div>
                    <span className="font-mono text-[11px] font-medium text-[var(--foreground)]">
                      {item.requestCount}
                    </span>
                  </div>
                  
                  {/* Row 7: Average Latency */}
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-1.5">
                      <Clock size={11} className="text-[var(--hardware-orange)] shrink-0" />
                      <span className="font-mono text-[11px] text-[var(--muted-foreground)]">Avg Latency:</span>
                    </div>
                    <span className="font-mono text-[11px] font-medium text-[var(--hardware-orange)]">
                      {item.avgLatency}ms
                    </span>
                  </div>
                  
                  {/* Row 8: Last Used */}
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Last Used:</span>
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                      {item.lastUsed}
                    </span>
                  </div>
                  
                  {/* Expanded Details */}
                  {isSelected && item.requestCount > 0 && (
                    <div className="mt-3 pt-3 border-t border-[var(--border)]">
                      <p className="font-mono text-[10px] text-[var(--muted-foreground)] mb-2 uppercase tracking-wider">Details</p>
                      <div className="grid grid-cols-1 gap-1.5 text-[11px] font-mono">
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--muted-foreground)]">Average Tokens per Request:</span>
                          <span className="text-[var(--foreground)]">{Math.round(item.totalTokens / item.requestCount).toLocaleString()}</span>
                        </div>
                        <div className="flex items-center justify-between">
                          <span className="text-[var(--muted-foreground)]">Average Cost per Request:</span>
                          <span className="text-[var(--foreground)]">{formatCost(item.cost / item.requestCount)}</span>
                        </div>
                        {item.outputTokens > 0 && (
                          <div className="flex items-center justify-between">
                            <span className="text-[var(--muted-foreground)]">Input/Output Ratio:</span>
                            <span className="text-[var(--foreground)]">{(item.inputTokens / item.outputTokens).toFixed(2)}:1</span>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

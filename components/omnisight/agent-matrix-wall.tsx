"use client"

import { useState, useEffect, useCallback } from "react"
import { ChevronDown, ChevronUp, AlertTriangle, Check, X, Loader2, Plus, Trash2, Clock, ThumbsUp, ThumbsDown, RotateCcw, Cpu, Code, TestTube, FileBarChart, Sparkles, Zap, Shield, Settings, Eye } from "lucide-react"
import { PanelHelp } from "@/components/omnisight/panel-help"

export type AgentStatus = "idle" | "running" | "success" | "error" | "warning" | "booting" | "awaiting_confirmation" | "materializing"

export interface SubTask {
  id: string
  name: string
  status: "pending" | "running" | "done" | "error"
  duration?: string
}

export interface AgentMessage {
  id: string
  type: "info" | "warning" | "error" | "success" | "action"
  message: string
  timestamp: string
  details?: string
  /** R3 (#309): true when the LLM response was stitched together after
   * hitting ``stop_reason=max_tokens``. The UI renders a small
   * "↩ auto-continued" tag so the operator knows the message spans
   * more than one provider call. */
  autoContinued?: boolean
  /** R3 (#309): how many continuation rounds it took. Optional label
   * next to the tag so a 1-round stitch doesn't shout as loud as a
   * 4-round one. */
  continuationRounds?: number
}

export interface AgentHistoryEntry {
  id: string
  action: string
  result: "success" | "error" | "warning" | "pending"
  timestamp: string
  duration?: string
  output?: string
}

export type MaterializationPhase = "idle" | "ejection" | "wireframe" | "components" | "bootup" | "complete"

// AI model display — supports both known models and dynamic strings from backend
export type AIModel = string

interface ModelDisplayInfo { label: string; shortLabel: string; provider: string; color: string }

const KNOWN_MODELS: Record<string, ModelDisplayInfo> = {
  "claude-opus":   { label: "Claude Opus",   shortLabel: "Opus",    provider: "Anthropic", color: "#d97706" },
  "claude-sonnet": { label: "Claude Sonnet", shortLabel: "Sonnet",  provider: "Anthropic", color: "#f59e0b" },
  "claude-mythos": { label: "Claude Mythos", shortLabel: "Mythos",  provider: "Anthropic", color: "#b45309" },
  "claude-haiku":  { label: "Claude Haiku",  shortLabel: "Haiku",   provider: "Anthropic", color: "#fbbf24" },
  "gpt-5.4":       { label: "GPT-5.4",       shortLabel: "GPT-5.4", provider: "OpenAI",    color: "#10b981" },
  "gpt-5.3":       { label: "GPT-5.3",       shortLabel: "GPT-5.3", provider: "OpenAI",    color: "#34d399" },
  "gpt-5.2":       { label: "GPT-5.2",       shortLabel: "GPT-5.2", provider: "OpenAI",    color: "#6ee7b7" },
  "gpt-4o":        { label: "GPT-4o",        shortLabel: "GPT-4o",  provider: "OpenAI",    color: "#059669" },
  "gemini-3.1-pro":      { label: "Gemini 3.1 Pro",      shortLabel: "Gemini Pro",   provider: "Google", color: "#3b82f6" },
  "gemini-3.1-thinking": { label: "Gemini 3.1 Thinking", shortLabel: "Gemini Think", provider: "Google", color: "#2563eb" },
  "gemini-3.1-fast":     { label: "Gemini 3.1 Fast",     shortLabel: "Gemini Fast",  provider: "Google", color: "#60a5fa" },
  "gemini-1.5-pro":      { label: "Gemini 1.5 Pro",      shortLabel: "Gemini 1.5",   provider: "Google", color: "#93c5fd" },
  "grok-3":        { label: "Grok 3",        shortLabel: "Grok",    provider: "xAI",      color: "#ec4899" },
  "grok-3-mini":   { label: "Grok 3 Mini",   shortLabel: "Grok-m",  provider: "xAI",      color: "#f472b6" },
  "mistral-large": { label: "Mistral Large",  shortLabel: "Mistral", provider: "Mistral",  color: "#f97316" },
  "llama-3":       { label: "Llama 3",        shortLabel: "Llama",   provider: "Meta",     color: "#8b5cf6" },
  "deepseek-chat": { label: "DeepSeek Chat",  shortLabel: "DeepSeek", provider: "DeepSeek", color: "#06b6d4" },
  "ollama":        { label: "Ollama (Local)",  shortLabel: "Ollama",  provider: "Local",    color: "#a3a3a3" },
}

// 2026-04-21: helper that turns a model suffix (everything after the
// matched prefix) into a compact display token. Earlier version
// returned only the base shortLabel ("Opus") for any ``claude-opus-*``
// which made ``claude-opus-4-6`` and ``claude-opus-4-7`` visually
// identical in the LLM MODEL selector. Operator reported confusion
// when 2× Opus or 3× Llama chips all shared the same label with no
// way to pick between versions. We now surface the suffix so the two
// Opus versions render as ``Opus 4.7`` / ``Opus 4.6`` and three
// Llama variants stay distinct.
function _suffixFromModelString(model: string, knownPrefix: string): string {
  // ``claude-opus-4-7`` with prefix ``claude-opus`` → ``4-7`` → ``4.7``
  // ``llama-3.3-70b`` with prefix ``llama-3`` → ``.3-70b`` → ``3.70b`` (ugly; see below)
  // ``llama-3-70b`` with prefix ``llama-3`` → ``-70b`` → ``70b``
  // Remove leading separators; replace hyphens between digits with dots
  // (version-ish), but keep hyphens between letters (``-70b`` stays).
  let s = model.slice(knownPrefix.length).replace(/^[-_:/.]+/, "")
  if (!s) return ""
  // Heuristic: if ALL hyphens are between digits, treat as version → dots.
  // Otherwise (mix of digits + letters) leave hyphens alone.
  if (/^[\d.-]+$/.test(s)) {
    s = s.replace(/-/g, ".")
  }
  return s
}

/** Resolve display info for any model string — fuzzy matches known models, falls back to generic. */
export function getModelInfo(model: unknown): ModelDisplayInfo {
  if (!model || typeof model !== "string") return { label: "", shortLabel: "", provider: "", color: "#737373" }
  const lower = model.toLowerCase()
  // Exact match — canonical shortLabel, no suffix needed.
  if (KNOWN_MODELS[lower]) return KNOWN_MODELS[lower]
  // Prefix match (e.g. ``claude-opus-4-7`` → ``claude-opus``). Longer
  // prefixes tried first so ``claude-opus`` beats ``claude``.
  const sorted = Object.keys(KNOWN_MODELS).sort((a, b) => b.length - a.length)
  for (const key of sorted) {
    if (lower.startsWith(key)) {
      const info = KNOWN_MODELS[key]
      const suffix = _suffixFromModelString(lower, key)
      if (!suffix) return info
      // Preserve the full model string as ``label`` (tooltip / long form)
      // while the chip renders ``shortLabel + ' ' + suffix``.
      return { ...info, label: model, shortLabel: `${info.shortLabel} ${suffix}` }
    }
  }
  // Provider detection from string — unknown vendor model. Fall through
  // to the most-specific inference on the lower-cased string, then
  // preserve enough of the original to disambiguate multiple variants.
  const providerFallbacks: { match: string; shortLabel: string; provider: string; color: string }[] = [
    { match: "claude",   shortLabel: "Claude",   provider: "Anthropic", color: "#f59e0b" },
    { match: "gpt",      shortLabel: "GPT",      provider: "OpenAI",    color: "#10b981" },
    { match: "gemini",   shortLabel: "Gemini",   provider: "Google",    color: "#3b82f6" },
    { match: "grok",     shortLabel: "Grok",     provider: "xAI",       color: "#ec4899" },
    { match: "llama",    shortLabel: "Llama",    provider: "Meta",      color: "#8b5cf6" },
    { match: "deepseek", shortLabel: "DeepSeek", provider: "DeepSeek",  color: "#06b6d4" },
    { match: "mistral",  shortLabel: "Mistral",  provider: "Mistral",   color: "#f97316" },
    { match: "gemma",    shortLabel: "Gemma",    provider: "Google",    color: "#2563eb" },
  ]
  for (const fb of providerFallbacks) {
    if (lower.includes(fb.match)) {
      const suffix = _suffixFromModelString(lower, fb.match)
      const shortLabel = suffix ? `${fb.shortLabel} ${suffix}` : fb.shortLabel
      return { label: model, shortLabel, provider: fb.provider, color: fb.color }
    }
  }
  // Unknown model — show the raw string so it's at least pickable.
  return { label: model, shortLabel: model, provider: "", color: "#737373" }
}

// Backwards compat — old code references AI_MODEL_INFO[agent.aiModel]
export const AI_MODEL_INFO = new Proxy({} as Record<string, ModelDisplayInfo>, {
  get: (_target, prop) => {
    if (typeof prop !== "string") return undefined
    return getModelInfo(prop)
  },
})

/** R2 (#308) — Cognitive Health signal block for the Agent Matrix Wall. */
export interface AgentCognitiveHealth {
  /** Rolling-window pairwise cosine-similarity mean (0..1). Higher = more repetitive. */
  entropyScore: number
  /** Classifier verdict derived from the score + thresholds. */
  verdict: "ok" | "warning" | "deadlock"
  /** Warn / deadlock thresholds echoed from the backend so the UI stays in sync. */
  thresholdWarn: number
  thresholdDeadlock: number
  /** Last-N entropy scores — drives the sparkline. */
  sparkline: number[]
  /** ReAct loop counter (loop N / max M). */
  loopCount: number
  loopMax: number
  /** Last ~5 recent outputs (truncated) — powers the popover. */
  recentOutputs?: string[]
  /** ISO timestamp of the most recent measurement. */
  lastUpdated?: string
}

/** R3 (#309) — Scratchpad Progress Indicator signal. */
export interface AgentScratchpadSummary {
  /** Latest saved turn number. */
  turn: number
  /** Denominator for the progress bar — usually ``loopMax`` or the planned total. */
  totalTurns: number
  /** Non-empty sections (out of 5) — drives the mini dot-strip. */
  sectionsCount: number
  /** Encrypted-on-disk size in bytes; rendered as a human-readable chip. */
  sizeBytes: number
  /** What kicked off the most recent save. */
  trigger?: string
  /** Optional sub-task label that was active when the save happened. */
  subtask?: string | null
  /** Seconds since the last successful save — powers the "2 min ago" label. */
  ageSeconds?: number | null
  /** ISO timestamp of the last save. */
  updatedAtIso?: string | null
  /** When true the agent has a saved scratchpad that survives a crash. */
  recoverable?: boolean
}

export interface Agent {
  id: string
  name: string
  type: "firmware" | "software" | "reporter" | "validator" | "reviewer" | "custom"
  subType?: string
  status: AgentStatus
  progress: { current: number; total: number }
  thoughtChain: string
  aiModel?: AIModel
  subTasks?: SubTask[]
  history?: AgentHistoryEntry[]
  messages?: AgentMessage[]
  requiresConfirmation?: boolean
  materializationPhase?: MaterializationPhase
  /** R2 (#308): Semantic Entropy Monitor signal — optional until first measurement. */
  cognitive?: AgentCognitiveHealth
  /** R3 (#309): Scratchpad offload signal — optional until first save. */
  scratchpad?: AgentScratchpadSummary
}

// Agent type configurations
export const AGENT_TYPES = {
  firmware: { 
    icon: Cpu, 
    label: "FIRMWARE", 
    color: "var(--hardware-orange)",
    description: "Hardware drivers & embedded systems",
    tools: ["Compiler", "Flasher", "Debugger"]
  },
  software: { 
    icon: Code, 
    label: "SOFTWARE", 
    color: "var(--neural-blue)",
    description: "Application code & algorithms",
    tools: ["Builder", "Optimizer", "Profiler"]
  },
  validator: { 
    icon: TestTube, 
    label: "VALIDATOR", 
    color: "var(--validation-emerald)",
    description: "Testing & quality assurance",
    tools: ["Tester", "Analyzer", "Reporter"]
  },
  reporter: { 
    icon: FileBarChart, 
    label: "REPORTER", 
    color: "var(--artifact-purple)",
    description: "Documentation & reporting",
    tools: ["Generator", "Formatter", "Publisher"]
  },
  reviewer: {
    icon: Eye,
    label: "REVIEWER",
    color: "#f472b6",
    description: "Code review & quality gate",
    tools: ["Diff", "Comment", "Score"]
  },
  custom: {
    icon: Settings,
    label: "CUSTOM",
    color: "var(--muted-foreground)",
    description: "User-defined agent type",
    tools: ["Configurable"]
  }
} as const

// Empty default — real agents come from backend via useEngine hook
export const defaultAgents: Agent[] = []

function getStatusColor(status: AgentStatus): string {
  switch (status) {
    case "running": return "var(--neural-blue)"
    case "success": return "var(--validation-emerald)"
    case "error": return "var(--critical-red)"
    case "warning": return "var(--hardware-orange)"
    case "booting": return "var(--artifact-purple)"
    case "awaiting_confirmation": return "var(--artifact-purple)"
    case "materializing": return "var(--artifact-purple)"
    default: return "var(--muted-foreground)"
  }
}

function getResultColor(result: AgentHistoryEntry["result"]): string {
  switch (result) {
    case "success": return "var(--validation-emerald)"
    case "error": return "var(--critical-red)"
    case "warning": return "var(--hardware-orange)"
    default: return "var(--muted-foreground)"
  }
}

function getAgentBorderClass(type: string, status: AgentStatus, cognitive?: AgentCognitiveHealth): string {
  // R2 (#308): a cognitive deadlock overrides the nominal status colour —
  // an agent can be status="running" and still be semantically stuck.
  if (cognitive?.verdict === "deadlock") return "border-[var(--critical-red)]"
  if (status === "warning") return "border-[var(--hardware-orange)]"
  if (status === "error") return "border-[var(--critical-red)]"
  if (status === "awaiting_confirmation") return "border-[var(--artifact-purple)]"
  if (status === "running") {
    switch (type) {
      case "firmware": return "border-[var(--hardware-orange)]"
      case "reporter": return "border-[var(--artifact-purple)]"
      default: return "border-[var(--neural-blue)]"
    }
  }
  if (status === "success") return "border-[var(--validation-emerald)]"
  return "border-[var(--border)]"
}

function getAgentPulseClass(status: AgentStatus, type: string, cognitive?: AgentCognitiveHealth): string {
  // R2 (#308): deadlock → red pulse with FUI scan-line vibe. Takes
  // precedence so the operator can spot a spinning agent at a glance.
  if (cognitive?.verdict === "deadlock") return "pulse-red entropy-scan"
  if (status === "running") {
    switch (type) {
      case "firmware": return "pulse-orange"
      case "reporter": return "pulse-purple"
      default: return "pulse-blue"
    }
  }
  if (status === "warning") return "pulse-orange"
  if (status === "error") return "pulse-red"
  if (status === "success") return "pulse-emerald"
  if (status === "awaiting_confirmation") return "pulse-purple"
  return ""
}

function StatusIcon({ status }: { status: AgentStatus }) {
  switch (status) {
    case "running":
    case "booting":
      return <Loader2 size={14} className="animate-spin" />
    case "success":
      return <Check size={14} />
    case "error":
      return <X size={14} />
    case "warning":
      return <AlertTriangle size={14} />
    case "awaiting_confirmation":
      return <Clock size={14} className="animate-pulse" />
    case "materializing":
      return <Sparkles size={14} className="animate-pulse" />
    default:
      return <div className="w-2 h-2 rounded-full bg-current" />
  }
}

function TaskDots({ progress, status }: { progress: { current: number; total: number }; status: AgentStatus }) {
  return (
    <div className="flex items-center gap-1">
      {Array.from({ length: progress.total }).map((_, i) => {
        const isDone = i < progress.current
        const isCurrent = i === progress.current && status === "running"
        return (
          <div
            key={i}
            className={`w-2 h-2 rounded-full transition-all ${
              isDone 
                ? "bg-[var(--validation-emerald)]" 
                : isCurrent 
                  ? "bg-[var(--neural-blue)] dot-jump" 
                  : "bg-[var(--muted-foreground)] opacity-30"
            }`}
          />
        )
      })}
    </div>
  )
}

// R2 (#308): Cognitive Health section — entropy sparkline + verdict badge +
// ReAct loop counter. Kept inline (no extra file) so each Agent card stays
// self-contained and the popover can share the card's click context.
function EntropySparkline({ values, verdict }: { values: number[]; verdict: AgentCognitiveHealth["verdict"] }) {
  const W = 72
  const H = 18
  const data = values.slice(-20)
  if (data.length < 2) {
    return <div className="w-[72px] h-[18px] opacity-40 font-mono text-[9px] flex items-center justify-center">—</div>
  }
  const min = Math.min(0, ...data)
  const max = Math.max(1, ...data)
  const range = Math.max(0.001, max - min)
  const stepX = data.length > 1 ? W / (data.length - 1) : W
  const pts = data.map((v, i) => {
    const x = i * stepX
    const y = H - ((v - min) / range) * H
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(" ")
  const stroke = verdict === "deadlock"
    ? "var(--critical-red,#ef4444)"
    : verdict === "warning"
      ? "var(--fui-orange,#f59e0b)"
      : "var(--validation-emerald,#10b981)"
  return (
    <svg width={W} height={H} className="shrink-0" aria-hidden>
      <polyline
        fill="none"
        stroke={stroke}
        strokeWidth={1.2}
        points={pts}
      />
    </svg>
  )
}

function VerdictBadge({ verdict, score, warnThreshold, deadThreshold }: {
  verdict: AgentCognitiveHealth["verdict"]
  score: number
  warnThreshold: number
  deadThreshold: number
}) {
  const icon = verdict === "deadlock" ? "🔴" : verdict === "warning" ? "⚠️" : "✅"
  const color = verdict === "deadlock"
    ? "var(--critical-red,#ef4444)"
    : verdict === "warning"
      ? "var(--fui-orange,#f59e0b)"
      : "var(--validation-emerald,#10b981)"
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9px] font-mono tabular-nums"
      style={{
        color,
        backgroundColor: `color-mix(in srgb, ${color} 18%, transparent)`,
      }}
      title={`Warn ≥ ${warnThreshold.toFixed(2)}, Deadlock ≥ ${deadThreshold.toFixed(2)}`}
    >
      <span aria-hidden>{icon}</span>
      {score.toFixed(2)}
    </span>
  )
}

function CognitiveHealthSection({ cognitive }: { cognitive: AgentCognitiveHealth }) {
  const [showPopover, setShowPopover] = useState(false)
  const atMax = cognitive.loopCount >= cognitive.loopMax
  return (
    <div className="mt-2 border-t border-[var(--border)]/60 pt-2">
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="font-mono text-[9px] tracking-[0.16em] text-[var(--muted-foreground)]">
          COGNITIVE HEALTH
        </span>
        <span
          className={`font-mono text-[9px] tabular-nums ${atMax ? "text-[var(--critical-red,#ef4444)]" : "text-[var(--muted-foreground)]"}`}
          title={atMax ? "ReAct loop at max — auto-escalate" : "ReAct loop counter"}
        >
          loop {cognitive.loopCount}/{cognitive.loopMax}
        </span>
      </div>
      <div className="flex items-center gap-2 relative">
        <button
          type="button"
          aria-label="Show recent outputs"
          onClick={(e) => {
            e.stopPropagation()
            setShowPopover(p => !p)
          }}
          className="rounded hover:bg-[var(--secondary)]/60 transition-colors"
        >
          <EntropySparkline values={cognitive.sparkline} verdict={cognitive.verdict} />
        </button>
        <VerdictBadge
          verdict={cognitive.verdict}
          score={cognitive.entropyScore}
          warnThreshold={cognitive.thresholdWarn}
          deadThreshold={cognitive.thresholdDeadlock}
        />
        {showPopover && cognitive.recentOutputs && cognitive.recentOutputs.length > 0 && (
          <div
            className="absolute z-50 top-full left-0 mt-1 p-2 rounded border border-[var(--border)] bg-[var(--background)] shadow-lg w-64 max-h-48 overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="font-mono text-[9px] uppercase tracking-widest text-[var(--muted-foreground)] mb-1">
              Last {cognitive.recentOutputs.length} outputs
            </div>
            <ol className="space-y-1">
              {cognitive.recentOutputs.map((out, i) => (
                <li key={i} className="font-mono text-[10px] text-[var(--foreground)] leading-relaxed break-words">
                  <span className="text-[var(--muted-foreground)] mr-1">{i + 1}.</span>
                  {out}
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </div>
  )
}

// R3 (#309): Scratchpad Progress Indicator. Renders a progress bar + the
// relative save age, plus a "Recoverable ●" badge when the agent errored
// but has a saved scratchpad we can hot-resume from. Tucked into the
// Cognitive Health card so the operator sees cognition + persistence
// state in one glance.
function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`
  return `${(n / (1024 * 1024)).toFixed(1)}MB`
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return "—"
  if (seconds < 5) return "just now"
  if (seconds < 60) return `${Math.floor(seconds)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86400)} d ago`
}

function ScratchpadIndicator({
  agentId,
  summary,
  status,
}: {
  agentId: string
  summary: AgentScratchpadSummary
  status: AgentStatus
}) {
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewText, setPreviewText] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)

  const denom = Math.max(summary.totalTurns || 1, summary.turn || 1, 1)
  const pct = Math.max(0, Math.min(100, Math.round((summary.turn / denom) * 100)))
  const recoverable = !!summary.recoverable && (status === "error" || status === "warning")

  const loadPreview = useCallback(async () => {
    if (previewText !== null || previewLoading) return
    setPreviewLoading(true)
    setPreviewError(null)
    try {
      const res = await fetch(`/api/v1/scratchpad/agents/${encodeURIComponent(agentId)}/preview`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setPreviewText(typeof data?.markdown === "string" ? data.markdown : "")
    } catch (err) {
      setPreviewError(String((err as Error).message || err))
    } finally {
      setPreviewLoading(false)
    }
  }, [agentId, previewLoading, previewText])

  const togglePreview = useCallback(() => {
    setPreviewOpen(prev => {
      const next = !prev
      if (next) void loadPreview()
      return next
    })
  }, [loadPreview])

  return (
    <div className="mt-2">
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="font-mono text-[9px] tracking-[0.16em] text-[var(--muted-foreground)]">
          SCRATCHPAD
        </span>
        <div className="flex items-center gap-1.5">
          {recoverable && (
            <span
              title="Agent has a recoverable scratchpad — can be hot-resumed"
              className="inline-flex items-center gap-1 px-1 py-0.5 rounded text-[9px] font-mono uppercase"
              style={{
                color: "var(--validation-emerald,#10b981)",
                backgroundColor: "color-mix(in srgb, var(--validation-emerald,#10b981) 18%, transparent)",
              }}
            >
              <span aria-hidden>●</span>
              Recoverable
            </span>
          )}
          <span
            className="font-mono text-[9px] tabular-nums text-[var(--muted-foreground)]"
            title={summary.updatedAtIso || "no save yet"}
          >
            {formatAge(summary.ageSeconds)}
          </span>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            togglePreview()
          }}
          className="flex-1 group/bar"
          aria-label="Open scratchpad preview"
        >
          <div className="h-1.5 w-full bg-[var(--secondary)]/60 rounded-full overflow-hidden relative">
            <div
              className="absolute inset-y-0 left-0 rounded-full transition-all"
              style={{
                width: `${pct}%`,
                background:
                  "linear-gradient(90deg, var(--neural-blue,#3b82f6) 0%, var(--artifact-purple,#a855f7) 100%)",
              }}
            />
          </div>
          <div className="flex items-center justify-between mt-1 gap-2">
            <span className="font-mono text-[9px] tabular-nums text-[var(--muted-foreground)]">
              turn {summary.turn}/{denom}
            </span>
            <span className="font-mono text-[9px] tabular-nums text-[var(--muted-foreground)]">
              {summary.sectionsCount}/5 sections · {formatBytes(summary.sizeBytes)}
            </span>
          </div>
        </button>
      </div>
      {previewOpen && (
        <div
          onClick={(e) => e.stopPropagation()}
          className="mt-2 rounded border border-[var(--border)] bg-[var(--background)] p-2 max-h-56 overflow-y-auto"
        >
          {previewLoading && (
            <div className="font-mono text-[10px] text-[var(--muted-foreground)]">Loading…</div>
          )}
          {previewError && (
            <div className="font-mono text-[10px] text-[var(--critical-red)]">{previewError}</div>
          )}
          {!previewLoading && !previewError && previewText && (
            <pre className="font-mono text-[10px] leading-snug whitespace-pre-wrap break-words text-[var(--foreground)]">
              {previewText}
            </pre>
          )}
          {!previewLoading && !previewError && !previewText && (
            <div className="font-mono text-[10px] text-[var(--muted-foreground)]">(empty)</div>
          )}
        </div>
      )}
    </div>
  )
}

interface AgentCardProps {
  agent: Agent
  onRemove?: (id: string) => void
  onConfirm?: (id: string) => void
  onReject?: (id: string) => void
  onRetry?: (id: string) => void
}

function AgentCard({ agent, onRemove, onConfirm, onReject, onRetry }: AgentCardProps) {
  const [expanded, setExpanded] = useState(agent.status === "awaiting_confirmation" || agent.status === "success" || agent.status === "error")
  
  const hasContent = agent.subTasks?.length || agent.history?.length || agent.messages?.length
  
  return (
    <div
      className={`holo-glass-simple rounded transition-all duration-300 ${getAgentBorderClass(agent.type, agent.status, agent.cognitive)} ${getAgentPulseClass(agent.status, agent.type, agent.cognitive)} group relative overflow-hidden glitch-hover corner-brackets`}
    >
      {/* Header - Always visible */}
      <div 
        className="p-3 cursor-pointer"
        onClick={() => hasContent && setExpanded(!expanded)}
      >
        {/* Row 1: Status + Name + Progress */}
        <div className="flex items-center gap-2 mb-2">
          <span 
            className="flex items-center justify-center w-5 h-5 rounded shrink-0"
            style={{ color: getStatusColor(agent.status), backgroundColor: `color-mix(in srgb, ${getStatusColor(agent.status)} 20%, transparent)` }}
          >
            <StatusIcon status={agent.status} />
          </span>
          <span
            className="font-mono text-xs font-semibold flex-1 min-w-0 truncate"
            style={{ color: getStatusColor(agent.status) }}
          >
            {agent.name}
          </span>
          <span className="font-mono text-xs text-[var(--muted-foreground)] shrink-0">
            {agent.progress.current}/{agent.progress.total}
          </span>
          {hasContent && (
            <span className="shrink-0">
              {expanded ? <ChevronUp size={12} className="text-[var(--muted-foreground)]" /> : <ChevronDown size={12} className="text-[var(--muted-foreground)]" />}
            </span>
          )}
        </div>
        
        {/* Row 2: Role (subType) + AI Model */}
        {(agent.subType || agent.aiModel) && (
          <div className="flex items-center gap-1.5 mb-2 flex-wrap">
            {agent.subType && (
              <span
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono uppercase"
                style={{
                  backgroundColor: `color-mix(in srgb, ${AGENT_TYPES[agent.type]?.color || 'var(--muted-foreground)'} 15%, transparent)`,
                  color: AGENT_TYPES[agent.type]?.color || 'var(--muted-foreground)'
                }}
              >
                <Shield size={8} />
                {agent.subType}
              </span>
            )}
            {agent.aiModel && (() => {
              const info = getModelInfo(agent.aiModel)
              return (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono"
                  style={{
                    backgroundColor: `color-mix(in srgb, ${info.color} 20%, transparent)`,
                    color: info.color
                  }}
                >
                  <Sparkles size={8} />
                  {info.shortLabel}
                </span>
              )
            })()}
          </div>
        )}
        
        {/* Row 3: Progress Dots */}
        <TaskDots progress={agent.progress} status={agent.status} />
        
        {/* Row 4: Current Status Text */}
        <p className="font-mono text-xs text-[var(--muted-foreground)] mt-2 leading-relaxed line-clamp-3 break-all">
          {agent.thoughtChain}
        </p>

        {/* Row 5 (R2 #308): Cognitive Health — rendered inline so the
            verdict and sparkline stay visible without expanding the card. */}
        {agent.cognitive && (
          <CognitiveHealthSection cognitive={agent.cognitive} />
        )}

        {/* Row 6 (R3 #309): Scratchpad Progress Indicator. */}
        {agent.scratchpad && (
          <ScratchpadIndicator
            agentId={agent.id}
            summary={agent.scratchpad}
            status={agent.status}
          />
        )}

        {/* Remove Button - Positioned in header */}
        {onRemove && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onRemove(agent.id)
            }}
            className="absolute top-2 right-2 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity bg-[var(--critical-red)]/20 hover:bg-[var(--critical-red)]/40 text-[var(--critical-red)]"
            title="Remove agent"
          >
            <Trash2 size={12} />
          </button>
        )}
      </div>
      
      {/* Expanded Content */}
      {expanded && hasContent && (
        <div className="border-t border-[var(--border)] bg-[var(--secondary)]/30">
          {/* Recent Activity - Simplified view instead of tabs */}
          <div className="p-3 max-h-40 overflow-y-auto">
            {agent.history && agent.history.length > 0 && (
              <div className="space-y-2">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase">Recent Activity</span>
                {agent.history.slice(-3).map(entry => (
                  <div key={entry.id} className="flex items-start gap-2">
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] shrink-0 w-14">{entry.timestamp}</span>
                    <div 
                      className="w-1.5 h-1.5 rounded-full mt-1 shrink-0"
                      style={{ backgroundColor: getResultColor(entry.result) }}
                    />
                    <span className="font-mono text-xs text-[var(--foreground)] flex-1 leading-relaxed">
                      {entry.action}
                    </span>
                    {entry.duration && (
                      <span className="font-mono text-[10px] text-[var(--muted-foreground)] shrink-0">{entry.duration}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
            
            {agent.messages && agent.messages.length > 0 && (
              <div className="space-y-2 mb-2">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase">Messages</span>
                {agent.messages.slice(-5).map(msg => (
                  <div key={msg.id} className="flex items-start gap-2">
                    <span className="font-mono text-[10px] text-[var(--muted-foreground)] shrink-0 w-14">{msg.timestamp}</span>
                    <div className="flex-1 min-w-0">
                      <span className="font-mono text-xs text-[var(--foreground)] leading-relaxed break-words">
                        {msg.message}
                      </span>
                      {msg.autoContinued && (
                        <span
                          className="ml-1.5 inline-flex items-center gap-0.5 px-1 py-0.5 rounded text-[9px] font-mono"
                          title={
                            msg.continuationRounds
                              ? `Stitched over ${msg.continuationRounds} continuation round(s) after stop_reason=max_tokens`
                              : "Stitched after stop_reason=max_tokens"
                          }
                          style={{
                            color: "var(--artifact-purple,#a855f7)",
                            backgroundColor: "color-mix(in srgb, var(--artifact-purple,#a855f7) 18%, transparent)",
                          }}
                        >
                          <span aria-hidden>↩</span>
                          auto-continued{msg.continuationRounds ? ` ×${msg.continuationRounds}` : ""}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {agent.subTasks && agent.subTasks.length > 0 && !agent.history?.length && (
              <div className="space-y-2">
                <span className="font-mono text-[10px] text-[var(--muted-foreground)] uppercase">Tasks</span>
                {agent.subTasks.map(task => (
                  <div key={task.id} className="flex items-center gap-2">
                    {/* Fix-C C3: status conveyed by colour alone is inaccessible
                        to colour-blind / screen-reader users. sr-only label
                        mirrors the semantic meaning. */}
                    <div
                      role="img"
                      aria-label={`Status: ${task.status}`}
                      className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                        task.status === "done" ? "bg-[var(--validation-emerald)]" :
                        task.status === "running" ? "bg-[var(--neural-blue)] animate-pulse" :
                        task.status === "error" ? "bg-[var(--critical-red)]" :
                        "bg-[var(--muted-foreground)] opacity-30"
                      }`}
                    />
                    <span className="font-mono text-xs text-[var(--foreground)] flex-1">{task.name}</span>
                    {task.duration && (
                      <span className="font-mono text-[10px] text-[var(--validation-emerald)]">{task.duration}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      
      {/* Confirmation Actions - Fixed at bottom */}
      {agent.requiresConfirmation && agent.status === "awaiting_confirmation" && (
        <div className="p-3 border-t border-[var(--border)] bg-[var(--artifact-purple)]/5">
          <span className="font-mono text-[10px] text-[var(--artifact-purple)] block mb-2">
            Awaiting User Confirmation
          </span>
          <div className="flex gap-2">
            {onConfirm && (
              <button
                onClick={(e) => { e.stopPropagation(); onConfirm(agent.id) }}
                className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded text-xs font-mono bg-[var(--validation-emerald)]/20 hover:bg-[var(--validation-emerald)]/40 text-[var(--validation-emerald)] transition-colors"
              >
                <ThumbsUp size={12} />
                <span>Approve</span>
              </button>
            )}
            {onReject && (
              <button
                onClick={(e) => { e.stopPropagation(); onReject(agent.id) }}
                className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded text-xs font-mono bg-[var(--critical-red)]/20 hover:bg-[var(--critical-red)]/40 text-[var(--critical-red)] transition-colors"
              >
                <ThumbsDown size={12} />
                <span>Reject</span>
              </button>
            )}
          </div>
        </div>
      )}
      
      {/* Retry Action for Errors */}
      {agent.status === "error" && onRetry && (
        <div className="p-3 border-t border-[var(--border)] bg-[var(--critical-red)]/5">
          <span className="font-mono text-[10px] text-[var(--critical-red)] block mb-2">
            Agent Halted - Error State
          </span>
          <button
            onClick={(e) => { e.stopPropagation(); onRetry(agent.id) }}
            className="w-full flex items-center justify-center gap-1.5 py-2 rounded text-xs font-mono bg-[var(--neural-blue)]/20 hover:bg-[var(--neural-blue)]/40 text-[var(--neural-blue)] transition-colors"
          >
            <RotateCcw size={12} />
            <span>Retry</span>
          </button>
        </div>
      )}
    </div>
  )
}

// Shadow Node - Placeholder for spawning new agents
interface ShadowNodeProps {
  onSpawn: (type: Agent["type"], tools?: string[], subType?: string, aiModel?: string) => void
  disabled?: boolean
}

// Role and model options for the spawn menu
interface RoleOption { role_id: string; category: string; label: string }

// Provider colors for model selection UI
const PROVIDER_COLORS: Record<string, string> = {
  anthropic: "#f59e0b", openai: "#10b981", google: "#3b82f6",
  xai: "#ec4899", groq: "#f97316", deepseek: "#06b6d4",
  together: "#8b5cf6", openrouter: "#a855f7", ollama: "#737373",
}

// Static fallback — overridden by dynamic list when providers are fetched
const MODEL_OPTIONS_STATIC = [
  { id: "", label: "Default (System)", color: "#737373" },
]

function buildModelOptions(providers: { id: string; name: string; configured: boolean; models: string[]; default_model: string }[]) {
  const options: { id: string; label: string; color: string }[] = [
    { id: "", label: "Default (System)", color: "#737373" },
  ]
  for (const p of providers) {
    if (!p.configured) continue  // Only show providers with API keys
    const color = PROVIDER_COLORS[p.id] || "#737373"
    // Show top 2 models per provider to keep list manageable
    const models = p.models.slice(0, 2)
    for (const m of models) {
      const shortName = m.includes("/") ? m.split("/").pop()! : m
      options.push({
        id: `${p.id}:${m}`,  // provider:model format for _parse_model_spec
        label: `${shortName} (${p.name})`,
        color,
      })
    }
  }
  return options
}

// Static role options grouped by category (mirrors configs/roles/)
const ROLE_OPTIONS: Record<string, RoleOption[]> = {
  firmware:  [
    { role_id: "", category: "firmware", label: "General Firmware" },
    { role_id: "bsp", category: "firmware", label: "BSP 平台工程師" },
    { role_id: "isp", category: "firmware", label: "ISP/3A 調優" },
    { role_id: "hal", category: "firmware", label: "HAL 抽象層" },
  ],
  software: [
    { role_id: "", category: "software", label: "General Software" },
    { role_id: "algorithm", category: "software", label: "影像演算法" },
    { role_id: "ai-deploy", category: "software", label: "AI 部署優化" },
    { role_id: "middleware", category: "software", label: "通訊中間件" },
  ],
  validator: [
    { role_id: "", category: "validator", label: "General Validator" },
    { role_id: "sdet", category: "validator", label: "自動化測試 SDET" },
    { role_id: "security", category: "validator", label: "資安防護" },
  ],
  reporter: [
    { role_id: "", category: "reporter", label: "General Reporter" },
    { role_id: "compliance", category: "reporter", label: "合規認證" },
    { role_id: "documentation", category: "reporter", label: "技術文件" },
  ],
  reviewer: [
    { role_id: "", category: "reviewer", label: "General Reviewer" },
    { role_id: "code-review", category: "reviewer", label: "程式碼審查" },
  ],
  custom: [
    { role_id: "", category: "custom", label: "Custom Agent" },
  ],
}

function ShadowNode({ onSpawn, disabled = false }: ShadowNodeProps) {
  const [isHovered, setIsHovered] = useState(false)
  const [showMenu, setShowMenu] = useState(false)
  const [selectedType, setSelectedType] = useState<Agent["type"] | null>(null)
  const [selectedRole, setSelectedRole] = useState("")
  const [selectedModel, setSelectedModel] = useState("")
  const [modelOptions, setModelOptions] = useState(MODEL_OPTIONS_STATIC)

  // Fetch available providers to build dynamic model options
  useEffect(() => {
    if (!showMenu) return
    let cancelled = false
    import("@/lib/api").then(api =>
      api.getProviders().then(r => { if (!cancelled) setModelOptions(buildModelOptions(r.providers)) })
    ).catch(() => {})
    return () => { cancelled = true }
  }, [showMenu])

  const handleSelectType = (type: Agent["type"]) => {
    if (disabled) return
    setSelectedType(type)
    setSelectedRole("")
    setSelectedModel("")
  }

  const handleSpawn = () => {
    if (!selectedType) return
    // Each AGENT_TYPES entry ships a readonly tuple literal for
    // `tools`; onSpawn accepts the mutable string[] contract so a
    // shallow copy widens the type without changing the value.
    onSpawn(selectedType, [...AGENT_TYPES[selectedType].tools], selectedRole || undefined, selectedModel || undefined)
    setShowMenu(false)
    setSelectedType(null)
    setSelectedRole("")
    setSelectedModel("")
  }

  const handleCancel = () => {
    setShowMenu(false)
    setSelectedType(null)
    setSelectedRole("")
    setSelectedModel("")
  }
  
  // Disabled state - max agents reached
  if (disabled) {
    return (
      <div className="p-3 rounded border border-dashed border-[var(--border)] opacity-50">
        <div className="flex items-center justify-center gap-2">
          <div className="w-8 h-8 rounded-full bg-[var(--secondary)]/30 flex items-center justify-center">
            <Shield size={14} className="text-[var(--muted-foreground)]" />
          </div>
          <div className="text-left">
            <p className="font-mono text-[10px] text-[var(--muted-foreground)]">
              MAX CAPACITY
            </p>
            <p className="font-mono text-[10px] text-[var(--muted-foreground)]/60">
              128/128 agents active
            </p>
          </div>
        </div>
      </div>
    )
  }
  
  return (
    <div className="relative">
      {/* Shadow Node Card */}
      <button
        onClick={() => setShowMenu(!showMenu)}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => { setIsHovered(false); if (!showMenu) setSelectedType(null) }}
        className={`w-full p-3 rounded transition-all duration-500 ${
          showMenu 
            ? "bg-[var(--artifact-purple)]/10 border-2 border-[var(--artifact-purple)] shadow-lg"
            : "shadow-node"
        }`}
        style={showMenu ? { boxShadow: '0 0 30px var(--artifact-purple-dim), inset 0 0 20px var(--artifact-purple-dim)' } : {}}
      >
        <div className="flex items-center justify-center gap-2">
          <div className={`relative w-8 h-8 rounded-full flex items-center justify-center transition-all duration-300 ${
            isHovered || showMenu
              ? "bg-[var(--artifact-purple)]/30 border-2 border-[var(--artifact-purple)]"
              : "bg-[var(--secondary)]/50 border-2 border-dashed border-[var(--muted-foreground)]/30"
          }`}>
            {/* Inner rings when active */}
            {(isHovered || showMenu) && (
              <>
                <div className="absolute inset-0.5 rounded-full border border-[var(--artifact-purple)] border-dashed ring-spin opacity-50" />
                <div className="absolute inset-1.5 rounded-full border border-[var(--neural-blue)] ring-spin-reverse opacity-30" />
              </>
            )}
            <Plus size={16} className={`transition-all duration-300 ${
              isHovered || showMenu ? "text-[var(--artifact-purple)]" : "text-[var(--muted-foreground)]/50"
            }`} />
          </div>
          <div className="text-left">
            <p className={`font-mono text-[10px] transition-colors duration-300 ${
              isHovered || showMenu ? "text-[var(--artifact-purple)]" : "text-[var(--muted-foreground)]/50"
            }`}>
              {showMenu ? "SELECT TYPE" : "SPAWN AGENT"}
            </p>
            <p className={`font-mono text-[10px] transition-colors duration-300 ${
              isHovered || showMenu ? "text-[var(--muted-foreground)]" : "text-[var(--muted-foreground)]/30"
            }`}>
              Click to materialize
            </p>
          </div>
        </div>
      </button>
      
      {/* Holographic Spawn Menu */}
      {showMenu && (
        <div className="absolute left-0 right-0 top-full mt-1 z-50 holo-menu-appear">
          <div className="holo-glass-simple rounded overflow-hidden border border-[var(--artifact-purple)]/50">

            {/* Step 1: Type selection (or show selected type header) */}
            {!selectedType ? (
              <>
                <div className="px-2 py-1.5 bg-[var(--artifact-purple)]/10 border-b border-[var(--border)]">
                  <p className="font-mono text-[10px] text-[var(--artifact-purple)]">SELECT AGENT TYPE</p>
                </div>
                <div className="p-1.5 space-y-0.5">
                  {(Object.entries(AGENT_TYPES) as [Agent["type"], typeof AGENT_TYPES[Agent["type"]]][]).map(([type, config]) => {
                    const IconComponent = config.icon
                    return (
                      <button
                        key={type}
                        onClick={() => handleSelectType(type)}
                        className="w-full flex items-center gap-2 p-1.5 rounded hover:bg-[var(--secondary)] transition-all group"
                      >
                        <div
                          className="w-6 h-6 rounded flex items-center justify-center transition-all group-hover:scale-110 shrink-0"
                          style={{
                            backgroundColor: `color-mix(in srgb, ${config.color} 20%, transparent)`,
                            color: config.color
                          }}
                        >
                          <IconComponent size={12} />
                        </div>
                        <div className="flex-1 text-left min-w-0">
                          <p className="font-mono text-[10px] font-semibold truncate" style={{ color: config.color }}>
                            {config.label}
                          </p>
                        </div>
                      </button>
                    )
                  })}
                </div>
              </>
            ) : (
              <>
                {/* Selected type header */}
                <div className="px-2 py-1.5 border-b border-[var(--border)] flex items-center gap-2"
                  style={{ backgroundColor: `color-mix(in srgb, ${AGENT_TYPES[selectedType].color} 10%, transparent)` }}
                >
                  <button onClick={() => setSelectedType(null)} className="text-[var(--muted-foreground)] hover:text-[var(--foreground)]">
                    <ChevronUp size={12} />
                  </button>
                  <span className="font-mono text-[10px] font-semibold" style={{ color: AGENT_TYPES[selectedType].color }}>
                    {AGENT_TYPES[selectedType].label}
                  </span>
                </div>

                <div className="p-2 space-y-2">
                  {/* Role selection */}
                  <div>
                    <p className="font-mono text-[9px] text-[var(--muted-foreground)] mb-1 uppercase tracking-wider">Role</p>
                    <div className="space-y-0.5">
                      {(ROLE_OPTIONS[selectedType] || []).map(role => (
                        <button
                          key={role.role_id}
                          onClick={() => setSelectedRole(role.role_id)}
                          className={`w-full text-left px-2 py-1 rounded text-[10px] font-mono transition-all ${
                            selectedRole === role.role_id
                              ? "bg-[var(--artifact-purple)]/20 text-[var(--foreground)]"
                              : "text-[var(--muted-foreground)] hover:bg-[var(--secondary)]"
                          }`}
                        >
                          {role.role_id ? (
                            <span className="flex items-center gap-1.5">
                              <Shield size={9} style={{ color: AGENT_TYPES[selectedType].color }} />
                              <span className="uppercase">{role.role_id}</span>
                              <span className="text-[var(--muted-foreground)] text-[9px]">{role.label}</span>
                            </span>
                          ) : (
                            <span className="text-[var(--muted-foreground)]">{role.label}</span>
                          )}
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Model selection */}
                  <div>
                    <p className="font-mono text-[9px] text-[var(--muted-foreground)] mb-1 uppercase tracking-wider">AI Model</p>
                    <div className="space-y-0.5">
                      {modelOptions.map(m => (
                        <button
                          key={m.id}
                          onClick={() => setSelectedModel(m.id)}
                          className={`w-full text-left px-2 py-1 rounded text-[10px] font-mono transition-all ${
                            selectedModel === m.id
                              ? "bg-[var(--artifact-purple)]/20 text-[var(--foreground)]"
                              : "text-[var(--muted-foreground)] hover:bg-[var(--secondary)]"
                          }`}
                        >
                          <span className="flex items-center gap-1.5">
                            <Sparkles size={9} style={{ color: m.color }} />
                            <span>{m.label}</span>
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Spawn button */}
                  <div className="flex gap-1.5 pt-1 border-t border-[var(--border)]">
                    <button
                      onClick={handleCancel}
                      className="flex-1 py-1.5 rounded text-[10px] font-mono text-[var(--muted-foreground)] hover:bg-[var(--secondary)] transition-colors"
                    >
                      CANCEL
                    </button>
                    <button
                      onClick={handleSpawn}
                      className="flex-1 py-1.5 rounded text-[10px] font-mono font-semibold transition-all"
                      style={{
                        backgroundColor: `color-mix(in srgb, ${AGENT_TYPES[selectedType].color} 25%, transparent)`,
                        color: AGENT_TYPES[selectedType].color
                      }}
                    >
                      SPAWN
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// Materializing Agent Card - Shows the assembly ritual
interface MaterializingAgentProps {
  agent: Agent
  onComplete: () => void
}

function MaterializingAgentCard({ agent, onComplete }: MaterializingAgentProps) {
  const [phase, setPhase] = useState<MaterializationPhase>("ejection")
  const config = AGENT_TYPES[agent.type]
  const IconComponent = config.icon
  
  useEffect(() => {
    // Phase sequence: ejection -> wireframe -> components -> bootup -> complete
    const timings = {
      ejection: 600,
      wireframe: 800,
      components: 1000,
      bootup: 800
    }
    
    const sequence = async () => {
      setPhase("ejection")
      await new Promise(r => setTimeout(r, timings.ejection))
      setPhase("wireframe")
      await new Promise(r => setTimeout(r, timings.wireframe))
      setPhase("components")
      await new Promise(r => setTimeout(r, timings.components))
      setPhase("bootup")
      await new Promise(r => setTimeout(r, timings.bootup))
      setPhase("complete")
      onComplete()
    }
    
    sequence()
  }, [onComplete])
  
  return (
    <div className="relative">
      {/* Gravity Beam (Ejection Phase) */}
      {phase === "ejection" && (
        <div className="absolute -bottom-20 left-1/2 -translate-x-1/2 w-8 h-20 gravity-beam z-0" />
      )}
      
      {/* Main Card */}
      <div 
        className={`holo-glass-simple rounded transition-all duration-500 overflow-hidden ${
          phase === "complete" ? "materialize" : ""
        }`}
        style={{
          borderColor: config.color,
          boxShadow: phase === "components" || phase === "bootup" 
            ? `0 0 30px ${config.color}, inset 0 0 20px color-mix(in srgb, ${config.color} 20%, transparent)` 
            : undefined
        }}
      >
        <div className="p-4 relative">
          {/* Wireframe Phase - SVG overlay */}
          {phase === "wireframe" && (
            <svg className="absolute inset-0 w-full h-full pointer-events-none wireframe-glow" preserveAspectRatio="none">
              <rect 
                x="2" y="2" 
                width="calc(100% - 4px)" height="calc(100% - 4px)" 
                fill="none" 
                stroke={config.color}
                strokeWidth="1"
                className="wireframe-construct"
                rx="4"
              />
              <line x1="10%" y1="30%" x2="90%" y2="30%" stroke={config.color} strokeWidth="0.5" className="wireframe-construct" style={{ animationDelay: "0.2s" }} />
              <line x1="10%" y1="50%" x2="70%" y2="50%" stroke={config.color} strokeWidth="0.5" className="wireframe-construct" style={{ animationDelay: "0.3s" }} />
              <line x1="10%" y1="70%" x2="50%" y2="70%" stroke={config.color} strokeWidth="0.5" className="wireframe-construct" style={{ animationDelay: "0.4s" }} />
            </svg>
          )}
          
          {/* Component Fragments (Components Phase) */}
          {phase === "components" && (
            <div className="absolute inset-0 pointer-events-none overflow-hidden">
              <div className="absolute top-2 left-2 fragment-left">
                <div className="px-2 py-1 rounded text-xs font-mono bg-[var(--secondary)]" style={{ color: config.color }}>
                  {config.tools[0]}
                </div>
              </div>
              <div className="absolute top-2 right-2 fragment-right" style={{ animationDelay: "0.1s" }}>
                <div className="px-2 py-1 rounded text-xs font-mono bg-[var(--secondary)]" style={{ color: config.color }}>
                  {config.tools[1] || "Module"}
                </div>
              </div>
              <div className="absolute bottom-2 left-1/2 -translate-x-1/2 fragment-bottom" style={{ animationDelay: "0.2s" }}>
                <Shield size={16} style={{ color: config.color }} />
              </div>
            </div>
          )}
          
          {/* Agent Info */}
          <div className={`flex items-center gap-3 ${phase === "wireframe" ? "opacity-30" : phase === "ejection" ? "opacity-0" : "opacity-100"} transition-opacity duration-300`}>
            <div 
              className={`w-10 h-10 rounded-full flex items-center justify-center ${phase === "components" ? "snap-flash" : ""}`}
              style={{ 
                backgroundColor: `color-mix(in srgb, ${config.color} 20%, transparent)`,
                color: config.color 
              }}
            >
              <IconComponent size={20} />
            </div>
            <div className="flex-1 min-w-0">
              <p className="font-mono text-xs font-semibold truncate" style={{ color: config.color }}>
                {agent.name}
              </p>
              <p className="font-mono text-xs text-[var(--muted-foreground)]">
                {phase === "ejection" && "EJECTING..."}
                {phase === "wireframe" && "CONSTRUCTING WIREFRAME..."}
                {phase === "components" && "ATTACHING COMPONENTS..."}
                {phase === "bootup" && "BOOTING... ONLINE"}
                {phase === "complete" && agent.thoughtChain}
              </p>
            </div>
          </div>
          
          {/* Boot-up Progress Bar */}
          {(phase === "bootup" || phase === "complete") && (
            <div className="mt-3 h-1 rounded-full bg-[var(--secondary)] overflow-hidden">
              <div 
                className="h-full rounded-full transition-all duration-700 progress-shimmer"
                style={{ 
                  width: phase === "complete" ? "100%" : "60%",
                  backgroundColor: config.color
                }}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

interface AgentMatrixWallProps {
  agents: Agent[]
  onAddAgent?: (type?: Agent["type"], tools?: string[], subType?: string, aiModel?: string) => void
  onRemoveAgent?: (id: string) => void
  onConfirmAgent?: (id: string) => void
  onRejectAgent?: (id: string) => void
  onRetryAgent?: (id: string) => void
  onMaterializeAgent?: (type: Agent["type"]) => string // Returns new agent ID
  onMaterializeComplete?: (agentId: string) => void
}

export function AgentMatrixWall({ 
  agents, 
  onAddAgent, 
  onRemoveAgent, 
  onConfirmAgent, 
  onRejectAgent, 
  onRetryAgent,
  onMaterializeAgent,
  onMaterializeComplete
}: AgentMatrixWallProps) {
  const [materializingAgents, setMaterializingAgents] = useState<Agent[]>([])
  
  const runningCount = agents.filter(a => a.status === "running").length
  const successCount = agents.filter(a => a.status === "success").length
  const awaitingCount = agents.filter(a => a.status === "awaiting_confirmation").length
  const materializingCount = materializingAgents.length
  
  // Handle spawning a new agent via Shadow Node
  const handleSpawnAgent = useCallback((type: Agent["type"], tools?: string[], subType?: string, aiModel?: string) => {
    if (onMaterializeAgent) {
      // Use parent's materialization handler
      const newId = onMaterializeAgent(type)
      const newAgent: Agent = {
        id: newId,
        name: `${type.toUpperCase()}_AGENT_${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`,
        type,
        subType,
        status: "materializing",
        progress: { current: 0, total: Math.floor(Math.random() * 6) + 4 },
        thoughtChain: "Initializing neural pathways...",
        aiModel: aiModel || undefined,
        materializationPhase: "ejection"
      }
      setMaterializingAgents(prev => [...prev, newAgent])
    } else if (onAddAgent) {
      // Fallback to simple add
      onAddAgent(type, tools, subType, aiModel)
    }
  }, [onMaterializeAgent, onAddAgent])
  
  // Handle materialization complete
  const handleMaterializeComplete = useCallback((agentId: string) => {
    setMaterializingAgents(prev => prev.filter(a => a.id !== agentId))
    onMaterializeComplete?.(agentId)
  }, [onMaterializeComplete])
  
  // Track total agents (max 128)
  const totalAgents = agents.length + materializingAgents.length
  
  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-3 py-2.5 holo-glass-simple mb-3 corner-brackets relative">
        {/* Scan line effect on header */}
        <div className="absolute inset-0 line-scan pointer-events-none opacity-30" />
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[var(--neural-blue)] pulse-blue relative pulse-ring" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--neural-blue)]">
              AGENT MATRIX
            </h2>
            <PanelHelp doc="panels-overview" />
          </div>
          <div className="flex items-center gap-2">
            {materializingCount > 0 && (
              <div className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-[var(--artifact-purple)]/20">
                <Zap size={10} className="text-[var(--artifact-purple)] animate-pulse" />
                <span className="font-mono text-[10px] text-[var(--artifact-purple)]">
                  {materializingCount}
                </span>
              </div>
            )}
            <span className="font-mono text-xs text-[var(--muted-foreground)]">
              {totalAgents}/128
            </span>
          </div>
        </div>
        
        {/* Status indicators - compact row */}
        {totalAgents > 0 && (
          <div className="flex items-center gap-2 mt-1.5">
            {runningCount > 0 && (
              <span className="flex items-center gap-1 font-mono text-[10px] text-[var(--neural-blue)]">
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--neural-blue)] animate-pulse" />
                {runningCount}
              </span>
            )}
            {successCount > 0 && (
              <span className="flex items-center gap-1 font-mono text-[10px] text-[var(--validation-emerald)]">
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--validation-emerald)]" />
                {successCount}
              </span>
            )}
            {awaitingCount > 0 && (
              <span className="flex items-center gap-1 font-mono text-[10px] text-[var(--artifact-purple)]">
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--artifact-purple)] animate-pulse" />
                {awaitingCount}
              </span>
            )}
          </div>
        )}
      </div>
      
      {/* Agent Grid/List */}
      <div className="flex-1 overflow-auto pr-1">
        {/* Empty State */}
        {totalAgents === 0 && (
          <div className="flex flex-col items-center justify-center h-full min-h-[200px] text-center px-4">
            <div className="w-16 h-16 rounded-full border-2 border-dashed border-[var(--border)] flex items-center justify-center mb-4">
              <Plus size={24} className="text-[var(--muted-foreground)] opacity-50" />
            </div>
            <p className="font-mono text-xs text-[var(--muted-foreground)] mb-1">
              No agents active
            </p>
            <p className="font-mono text-[10px] text-[var(--muted-foreground)] opacity-60">
              Spawn an agent below to begin
            </p>
          </div>
        )}
        
        {/* Agent Cards List */}
        {totalAgents > 0 && (
          <div className="space-y-2">
            {/* Existing Agents */}
            {agents.map(agent => (
              <AgentCard 
                key={agent.id} 
                agent={agent} 
                onRemove={onRemoveAgent}
                onConfirm={onConfirmAgent}
                onReject={onRejectAgent}
                onRetry={onRetryAgent}
              />
            ))}
            
            {/* Materializing Agents */}
            {materializingAgents.map(agent => (
              <MaterializingAgentCard
                key={agent.id}
                agent={agent}
                onComplete={() => handleMaterializeComplete(agent.id)}
              />
            ))}
          </div>
        )}
        
        {/* Shadow Node - Always visible at bottom */}
        <div className={`${totalAgents > 0 ? "mt-3" : ""}`}>
          <ShadowNode onSpawn={handleSpawnAgent} disabled={totalAgents >= 128} />
        </div>
      </div>
    </div>
  )
}

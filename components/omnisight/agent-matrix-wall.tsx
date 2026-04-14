"use client"

import { useState, useEffect, useCallback } from "react"
import { ChevronDown, ChevronUp, AlertTriangle, Check, X, Loader2, Plus, Trash2, MessageSquare, CheckCircle2, XCircle, Clock, FileText, ThumbsUp, ThumbsDown, RotateCcw, Cpu, Code, TestTube, FileBarChart, Sparkles, Zap, Shield, Settings, Eye } from "lucide-react"
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

/** Resolve display info for any model string — fuzzy matches known models, falls back to generic. */
export function getModelInfo(model: unknown): ModelDisplayInfo {
  if (!model || typeof model !== "string") return { label: "", shortLabel: "", provider: "", color: "#737373" }
  const lower = model.toLowerCase()
  // Exact match
  if (KNOWN_MODELS[lower]) return KNOWN_MODELS[lower]
  // Prefix match (e.g. "claude-sonnet-4-20250514" → "claude-sonnet")
  const sorted = Object.keys(KNOWN_MODELS).sort((a, b) => b.length - a.length)
  for (const key of sorted) {
    if (lower.startsWith(key)) return KNOWN_MODELS[key]
  }
  // Provider detection from string
  if (lower.includes("claude")) return { label: model, shortLabel: "Claude", provider: "Anthropic", color: "#f59e0b" }
  if (lower.includes("gpt")) return { label: model, shortLabel: "GPT", provider: "OpenAI", color: "#10b981" }
  if (lower.includes("gemini")) return { label: model, shortLabel: "Gemini", provider: "Google", color: "#3b82f6" }
  if (lower.includes("grok")) return { label: model, shortLabel: "Grok", provider: "xAI", color: "#ec4899" }
  if (lower.includes("llama")) return { label: model, shortLabel: "Llama", provider: "Meta", color: "#8b5cf6" }
  if (lower.includes("deepseek")) return { label: model, shortLabel: "DeepSeek", provider: "DeepSeek", color: "#06b6d4" }
  // Unknown model — generic display
  return { label: model, shortLabel: model.split("-")[0], provider: "", color: "#737373" }
}

// Backwards compat — old code references AI_MODEL_INFO[agent.aiModel]
export const AI_MODEL_INFO = new Proxy({} as Record<string, ModelDisplayInfo>, {
  get: (_target, prop) => {
    if (typeof prop !== "string") return undefined
    return getModelInfo(prop)
  },
})

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

function getMessageIcon(type: AgentMessage["type"]) {
  switch (type) {
    case "success": return <CheckCircle2 size={12} className="text-[var(--validation-emerald)]" />
    case "error": return <XCircle size={12} className="text-[var(--critical-red)]" />
    case "warning": return <AlertTriangle size={12} className="text-[var(--hardware-orange)]" />
    case "action": return <FileText size={12} className="text-[var(--artifact-purple)]" />
    default: return <MessageSquare size={12} className="text-[var(--neural-blue)]" />
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

function getAgentBorderClass(type: string, status: AgentStatus): string {
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

function getAgentPulseClass(status: AgentStatus, type: string): string {
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
  
  // Get the latest history entry for display
  const latestHistory = agent.history?.[agent.history.length - 1]
  
  return (
    <div 
      className={`holo-glass-simple rounded transition-all duration-300 ${getAgentBorderClass(agent.type, agent.status)} ${getAgentPulseClass(agent.status, agent.type)} group relative overflow-hidden glitch-hover corner-brackets`}
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

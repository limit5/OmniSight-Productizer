"use client"

import { useState, useRef, useEffect, useCallback } from "react"
import { PanelHelp } from "@/components/omnisight/panel-help"
import {
  Send,
  Bot, 
  User, 
  Sparkles,
  Zap,
  Check,
  X,
  Target,
  Cpu,
  Code,
  TestTube,
  FileBarChart,
  Settings,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  Crown,
  Shield,
  History
} from "lucide-react"
import type { Agent, AgentStatus } from "./agent-matrix-wall"
import type { Task } from "./task-backlog"
import { AGENT_TYPES, getModelInfo } from "./agent-matrix-wall"
import { HandoffTimeline } from "./handoff-timeline"
import { matchCommands as slashMatchCommands, CATEGORY_COLORS as slashCategoryColors, type SlashCommand } from "@/lib/slash-commands"
import { TokenUsageStats } from "./token-usage-stats"
import { TurnTimeline } from "./turn-timeline"
import { PromptVersionDrawer } from "./prompt-version-drawer"
import { listEffectiveSkills, type EffectiveSkill } from "@/lib/api"

// Orchestrator message types
export interface OrchestratorMessage {
  id: string
  role: "user" | "orchestrator" | "system"
  content: string
  timestamp: string
  suggestion?: AISuggestion
}

// Helper to format time consistently (avoids hydration mismatch from locale differences)
function formatTime(): string {
  const date = new Date()
  const hours = date.getHours().toString().padStart(2, "0")
  const minutes = date.getMinutes().toString().padStart(2, "0")
  const seconds = date.getSeconds().toString().padStart(2, "0")
  return `${hours}:${minutes}:${seconds}`
}

function currentSkillMentionQuery(value: string): string | null {
  const token = value.split(/\s/).at(-1) ?? ""
  if (!token.startsWith("@")) return null
  return token.slice(1).toLowerCase()
}

function replaceCurrentSkillMention(value: string, skillName: string): string {
  const parts = value.split(/(\s+)/)
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    if (parts[i].startsWith("@")) {
      parts[i] = `@${skillName}`
      return `${parts.join("")} `
    }
  }
  return `${value}${value && !value.endsWith(" ") ? " " : ""}@${skillName} `
}

export interface AISuggestion {
  id: string
  type: "assign" | "spawn" | "alert" | "complete" | "reassign"
  title: string
  description: string
  taskId?: string
  agentId?: string
  agentType?: Agent["type"]
  priority: "high" | "medium" | "low"
  status: "pending" | "accepted" | "rejected"
}

interface OrchestratorAIProps {
  agents: Agent[]
  tasks: Task[]
  onAssignTask: (taskId: string, agentId: string) => void
  onSpawnAgent: (type: Agent["type"]) => void
  onForceAssign: (taskId: string, agentId: string) => void
  onUpdateAgentStatus?: (agentId: string, status: AgentStatus) => void
  onCompleteTask?: (taskId: string) => void
  externalMessages?: OrchestratorMessage[]
  onSendCommand?: (command: string) => void
  tokenUsage?: import("./token-usage-stats").ModelTokenUsage[]
  tokenBudget?: import("./token-usage-stats").TokenBudgetInfo | null
  /** Z.4 #293 checkbox 5 — per-provider balance envelopes polled on a
   *  dedicated 60 s cadence by ``useEngine``. Forwarded verbatim to
   *  ``<TokenUsageStats>`` which mounts the badge + expansion slots. */
  providerBalances?: import("@/lib/api").ProviderBalanceEnvelope[] | null
  onResetFreeze?: () => void
  onUpdateBudget?: (updates: Record<string, number | string>) => void
  onRefresh?: () => void
  compressionStats?: { total_original_bytes: number; total_compressed_bytes: number; compression_count: number; total_lines_removed: number; avg_ratio: number; estimated_tokens_saved: number } | null
  activeProvider?: string
  activeModel?: string
  providers?: { id: string; name: string; models: string[]; configured: boolean }[]
  providerHealth?: { chain: string[]; health: { id: string; name: string; configured: boolean; is_active: boolean; cooldown_remaining: number; status: string }[] } | null
  onSwitchProvider?: (provider: string, model?: string) => void
  onUpdateFallbackChain?: (chain: string[]) => void
  handoffs?: { task_id: string; agent_id: string; created_at: string }[]
  onLoadHandoffs?: () => void
}

// Helper to get agent icon component
function getAgentIcon(type: Agent["type"]) {
  switch (type) {
    case "firmware": return Cpu
    case "software": return Code
    case "validator": return TestTube
    case "reporter": return FileBarChart
    default: return Settings
  }
}

// Helper to get status color
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

// Helper to get status label
function getStatusLabel(status: AgentStatus): string {
  switch (status) {
    case "running": return "ACTIVE"
    case "success": return "DONE"
    case "error": return "ERROR"
    case "warning": return "HALT"
    case "booting": return "BOOT"
    case "awaiting_confirmation": return "WAIT"
    case "materializing": return "SPAWN"
    default: return "IDLE"
  }
}

export function OrchestratorAI({
  agents,
  tasks,
  onAssignTask,
  onSpawnAgent,
  onForceAssign,
  onUpdateAgentStatus: _onUpdateAgentStatus,
  onCompleteTask: _onCompleteTask,
  externalMessages = [],
  onSendCommand,
  tokenUsage,
  tokenBudget,
  providerBalances,
  onResetFreeze,
  onUpdateBudget,
  onRefresh,
  compressionStats,
  activeProvider,
  activeModel,
  providers,
  providerHealth,
  onSwitchProvider,
  onUpdateFallbackChain,
  handoffs,
  onLoadHandoffs,
}: OrchestratorAIProps) {
  const [messages, setMessages] = useState<OrchestratorMessage[]>([
    {
      id: "sys-init",
      role: "system",
      content: "ORCHESTRATOR ONLINE. Monitoring all agents and tasks.",
      timestamp: formatTime()
    },
    {
      id: "ai-status",
      role: "orchestrator",
      content: `System initialized. ${agents.length} agents registered, ${tasks.filter(t => t.status === "backlog").length} tasks pending assignment.`,
      timestamp: formatTime()
    }
  ])
  
  // Merge external messages into local state
  useEffect(() => {
    if (externalMessages.length > 0) {
      const lastExternal = externalMessages[externalMessages.length - 1]
      // Check if this message is already in our state
      if (!messages.find(m => m.id === lastExternal.id)) {
        setMessages(prev => [...prev, lastExternal]) // eslint-disable-line react-hooks/set-state-in-effect -- syncing external prop to local state
      }
    }
  }, [externalMessages, messages])
  
  const [suggestions, setSuggestions] = useState<AISuggestion[]>([])
  const [inputValue, setInputValue] = useState("")
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const [showForceAssign, setShowForceAssign] = useState(false)
  const [forceAssignTask, setForceAssignTask] = useState<string | null>(null)
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [slashSuggestions, setSlashSuggestions] = useState<SlashCommand[]>([])
  const [slashSelectedIdx, setSlashSelectedIdx] = useState(0)
  const [skills, setSkills] = useState<EffectiveSkill[]>([])
  const [skillSuggestions, setSkillSuggestions] = useState<EffectiveSkill[]>([])
  const [skillSelectedIdx, setSkillSelectedIdx] = useState(0)
  const [showAgentGrid, setShowAgentGrid] = useState(true)
  // ZZ.C1 #305-1 checkbox 3 (2026-04-24): system-prompt version drawer
  // — opens from the LLM MODEL section as a "System Prompt Versions"
  // button. Drawer is portal-positioned (fixed inset-0), so it overlays
  // the rest of the panel without disturbing scroll state.
  const [showPromptDrawer, setShowPromptDrawer] = useState(false)
  
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let cancelled = false
    listEffectiveSkills()
      .then((res) => {
        if (!cancelled) setSkills(res.items)
      })
      .catch(() => {
        if (!cancelled) setSkills([])
      })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (typeof window === "undefined") return
    const onInsert = (event: Event) => {
      const detail = (event as CustomEvent<{ text?: string }>).detail
      const text = detail?.text
      if (!text) return
      setInputValue((prev) => `${prev}${prev && !prev.endsWith(" ") ? " " : ""}${text}`)
      setSkillSuggestions([])
      setSlashSuggestions([])
      setTimeout(() => inputRef.current?.focus(), 0)
    }
    window.addEventListener("omnisight:chat-insert-text", onInsert as EventListener)
    return () => window.removeEventListener("omnisight:chat-insert-text", onInsert as EventListener)
  }, [])

  const updateSkillSuggestions = useCallback((value: string) => {
    const query = currentSkillMentionQuery(value)
    if (query === null) {
      setSkillSuggestions([])
      return
    }
    const matches = skills.filter((skill) => {
      const haystack = [
        skill.name,
        skill.description,
        skill.scope,
        ...skill.keywords,
      ].join(" ").toLowerCase()
      return haystack.includes(query)
    })
    setSkillSuggestions(matches.slice(0, 6))
    setSkillSelectedIdx(0)
  }, [skills])

  const pickSkillMention = useCallback((skillName: string) => {
    setInputValue((prev) => replaceCurrentSkillMention(prev, skillName))
    setSkillSuggestions([])
  }, [])
  
  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])
  
  // Generate AI suggestions based on current state
  const generateSuggestions = useCallback(() => {
    const newSuggestions: AISuggestion[] = []
    
    // Find unassigned tasks and idle agents
    const unassignedTasks = tasks.filter(t => t.status === "backlog" && !t.assignedAgentId)
    const idleAgents = agents.filter(a => a.status === "idle")
    
    // Suggest assignments for unassigned tasks
    unassignedTasks.forEach(task => {
      // Find best matching agent by type
      const suggestedType = task.suggestedAgentType
      const matchingIdleAgent = idleAgents.find(a => a.type === suggestedType) || idleAgents[0]
      
      if (matchingIdleAgent) {
        newSuggestions.push({
          id: `suggest-assign-${task.id}`,
          type: "assign",
          title: `Assign "${task.title}"`,
          description: `Recommend assigning to ${matchingIdleAgent.name}`,
          taskId: task.id,
          agentId: matchingIdleAgent.id,
          priority: task.priority === "critical" ? "high" : task.priority === "high" ? "high" : "medium",
          status: "pending"
        })
      } else if (suggestedType) {
        // No idle agent of the right type - suggest spawning
        newSuggestions.push({
          id: `suggest-spawn-${task.id}`,
          type: "spawn",
          title: `Spawn ${AGENT_TYPES[suggestedType].label} Agent`,
          description: `No idle ${suggestedType} agent for "${task.title}"`,
          taskId: task.id,
          agentType: suggestedType,
          priority: task.priority === "critical" ? "high" : "medium",
          status: "pending"
        })
      }
    })
    
    // Alert for stuck agents
    agents.filter(a => a.status === "error" || a.status === "warning").forEach(agent => {
      newSuggestions.push({
        id: `alert-${agent.id}`,
        type: "alert",
        title: `${agent.name} requires attention`,
        description: agent.thoughtChain,
        agentId: agent.id,
        priority: "high",
        status: "pending"
      })
    })
    
    setSuggestions(newSuggestions.slice(0, 5)) // Limit to 5 suggestions
  }, [agents, tasks])

  // Generate suggestions on state changes
  useEffect(() => {
    generateSuggestions() // eslint-disable-line react-hooks/set-state-in-effect -- derived state recomputed when deps change
  }, [generateSuggestions])
  
  // Handle user commands
  const processCommand = useCallback((command: string) => {
    const cmd = command.toLowerCase().trim()
    let response = ""
    let action: OrchestratorMessage["suggestion"] | undefined
    
    // Status command
    if (cmd === "status" || cmd === "report") {
      const running = agents.filter(a => a.status === "running").length
      const idle = agents.filter(a => a.status === "idle").length
      const errors = agents.filter(a => a.status === "error").length
      const pending = tasks.filter(t => t.status === "backlog").length
      const inProgress = tasks.filter(t => t.status === "in_progress").length
      
      response = `SYSTEM STATUS:\n- Agents: ${running} running, ${idle} idle, ${errors} errors\n- Tasks: ${pending} pending, ${inProgress} in progress\n- Suggestions: ${suggestions.filter(s => s.status === "pending").length} pending actions`
    }
    // Assign command
    else if (cmd.startsWith("assign ")) {
      const parts = cmd.replace("assign ", "").split(" to ")
      if (parts.length === 2) {
        const taskSearch = parts[0].trim()
        const agentSearch = parts[1].trim()
        
        const task = tasks.find(t => t.title.toLowerCase().includes(taskSearch) || t.id.includes(taskSearch))
        const agent = agents.find(a => a.name.toLowerCase().includes(agentSearch) || a.id.includes(agentSearch))
        
        if (task && agent) {
          onAssignTask(task.id, agent.id)
          response = `Assigned "${task.title}" to ${agent.name}`
        } else {
          response = `Could not find matching task or agent. Available agents: ${agents.map(a => a.name).join(", ")}`
        }
      } else {
        response = "Usage: assign [task name] to [agent name]"
      }
    }
    // Spawn command
    else if (cmd.startsWith("spawn ") || cmd.startsWith("create ")) {
      const typePart = cmd.replace("spawn ", "").replace("create ", "").trim()
      const validTypes: Agent["type"][] = ["firmware", "software", "validator", "reporter", "custom"]
      const matchedType = validTypes.find(t => typePart.includes(t))
      
      if (matchedType) {
        onSpawnAgent(matchedType)
        response = `Initiating materialization sequence for ${AGENT_TYPES[matchedType].label} agent...`
      } else {
        response = `Unknown agent type. Available types: ${validTypes.join(", ")}`
      }
    }
    // Help command
    else if (cmd === "help" || cmd === "?") {
      response = `ORCHESTRATOR COMMANDS:\n- status: View system overview\n- assign [task] to [agent]: Assign task\n- spawn [type]: Create new agent\n- analyze: Generate new suggestions\n- clear: Clear chat history`
    }
    // Analyze command
    else if (cmd === "analyze" || cmd === "scan") {
      setIsAnalyzing(true)
      generateSuggestions()
      setTimeout(() => setIsAnalyzing(false), 1500)
      response = "Analyzing system state and generating recommendations..."
    }
    // Clear command
    else if (cmd === "clear") {
      setMessages([{
        id: `sys-${Date.now()}`,
        role: "system",
        content: "Chat history cleared.",
        timestamp: formatTime()
      }])
      return
    }
    // Unknown command - try to interpret
    else {
      response = `Command not recognized. Type "help" for available commands. Analyzing intent...`
      
      // Try to understand intent
      if (cmd.includes("idle") || cmd.includes("available")) {
        const idleAgents = agents.filter(a => a.status === "idle")
        response = idleAgents.length > 0 
          ? `Idle agents: ${idleAgents.map(a => a.name).join(", ")}`
          : "No idle agents available. Consider spawning a new agent."
      } else if (cmd.includes("task") && cmd.includes("pending")) {
        const pendingTasks = tasks.filter(t => t.status === "backlog")
        response = pendingTasks.length > 0
          ? `Pending tasks: ${pendingTasks.map(t => t.title).join(", ")}`
          : "No pending tasks in backlog."
      }
    }
    
    // Add user message
    setMessages(prev => [...prev, {
      id: `user-${Date.now()}`,
      role: "user",
      content: command,
      timestamp: formatTime()
    }])
    
    // Add orchestrator response
    setTimeout(() => {
      setMessages(prev => [...prev, {
        id: `ai-${Date.now()}`,
        role: "orchestrator",
        content: response,
        timestamp: formatTime(),
        suggestion: action
      }])
    }, 300)
  }, [agents, tasks, suggestions, onAssignTask, onSpawnAgent, generateSuggestions])
  
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!inputValue.trim()) return

    // Clear autocomplete immediately
    setSlashSuggestions([])
    setSkillSuggestions([])

    const cmd = inputValue.toLowerCase().trim()

    // Fast local-only commands (no LLM needed)
    if (cmd === "help" || cmd === "?") {
      processCommand(inputValue)
    }
    // All / commands and everything else → send to backend (slash handler or LLM)
    else if (onSendCommand) {
      onSendCommand(inputValue)
    } else {
      processCommand(inputValue)
    }
    setInputValue("")
  }
  
  // Handle suggestion actions
  const handleAcceptSuggestion = (suggestion: AISuggestion) => {
    if (suggestion.type === "assign" && suggestion.taskId && suggestion.agentId) {
      onAssignTask(suggestion.taskId, suggestion.agentId)
      setMessages(prev => [...prev, {
        id: `sys-${Date.now()}`,
        role: "system",
        content: `Accepted: ${suggestion.title}`,
        timestamp: formatTime()
      }])
    } else if (suggestion.type === "spawn" && suggestion.agentType) {
      // Pin the agentType into a local so TS can see the
      // narrowed-non-undefined type inside the closure below.
      const agentType = suggestion.agentType
      onSpawnAgent(agentType)
      setMessages(prev => [...prev, {
        id: `sys-${Date.now()}`,
        role: "system",
        content: `Spawning ${AGENT_TYPES[agentType].label} agent...`,
        timestamp: formatTime()
      }])
    }
    
    setSuggestions(prev => prev.map(s => 
      s.id === suggestion.id ? { ...s, status: "accepted" as const } : s
    ))
  }
  
  const handleRejectSuggestion = (suggestion: AISuggestion) => {
    setSuggestions(prev => prev.map(s => 
      s.id === suggestion.id ? { ...s, status: "rejected" as const } : s
    ))
  }
  
  const handleReassign = (suggestion: AISuggestion) => {
    if (suggestion.taskId) {
      setForceAssignTask(suggestion.taskId)
      setShowForceAssign(true)
    }
  }
  
  const handleForceAssign = (agentId: string) => {
    if (forceAssignTask) {
      onForceAssign(forceAssignTask, agentId)
      setMessages(prev => [...prev, {
        id: `sys-${Date.now()}`,
        role: "system",
        content: `Force assigned task to ${agents.find(a => a.id === agentId)?.name || agentId}`,
        timestamp: formatTime()
      }])
    }
    setShowForceAssign(false)
    setForceAssignTask(null)
  }
  
  const pendingSuggestions = suggestions.filter(s => s.status === "pending")
  
  return (
    <div
      className="holo-glass h-full flex flex-col min-h-0 overflow-hidden corner-brackets-full holo-flicker"
      data-tour="orchestrator"
    >
      {/* Header */}
      <div className="px-3 py-2 border-b border-[var(--border)] relative">
        {/* Subtle holographic shimmer */}
        <div className="absolute inset-0 holo-shimmer opacity-20 pointer-events-none" />
        <div className="flex items-center justify-between gap-1 relative z-10">
          <div className="flex items-center gap-1.5 min-w-0">
            <div className="w-2 h-2 rounded-full bg-[var(--artifact-purple)] pulse-purple pulse-ring shrink-0" />
            <Crown size={12} className="text-[var(--artifact-purple)] text-glow-purple shrink-0" />
            <h2 className="font-sans text-xs font-semibold tracking-fui text-[var(--artifact-purple)] truncate">
              ORCHESTRATOR
            </h2>
            <PanelHelp doc="panels-overview" />
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); setIsAnalyzing(true); generateSuggestions(); onRefresh?.(); setTimeout(() => setIsAnalyzing(false), 1000); }}
            className={`relative z-20 p-1.5 rounded transition-colors cursor-pointer shrink-0 ${isAnalyzing ? "bg-[var(--artifact-purple)]/40 text-[var(--artifact-purple)]" : "bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--artifact-purple)]"}`}
            title="Analyze and suggest"
          >
            <RefreshCw size={12} className={isAnalyzing ? "animate-spin" : ""} />
          </button>
        </div>
        <p className="font-mono text-[10px] text-[var(--muted-foreground)] mt-0.5">
          Central AI Coordinator
        </p>
      </div>
      
      {/* Scrollable Content Area */}
      <div className="flex-1 min-h-0 overflow-y-auto">

      {/* ZZ.B1 #304-1 (2026-04-24): per-turn timeline cards — the
          "ccxray" signature UI. Mounted at the very top of the
          scrollable content area, above the Agent Status Grid /
          TokenUsageStats / LLM MODEL provider selector so it's the
          first thing the operator sees when the panel opens. The
          component self-subscribes to ``turn_metrics`` + ``turn_tool_stats``
          SSE (shared EventSource), so no wiring needed here. */}
      <TurnTimeline />

      {/* Agent Status Grid */}
      <div className="border-b border-[var(--border)]">
        <button
          onClick={() => setShowAgentGrid(!showAgentGrid)}
          className="w-full px-4 py-2 flex items-center justify-between text-xs font-mono text-[var(--muted-foreground)] hover:bg-[var(--secondary)]/50 transition-colors"
        >
          <span>AGENT STATUS ({agents.length})</span>
          {showAgentGrid ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
        </button>
        {showAgentGrid && (
          <div className="px-3 pb-3 space-y-2">
            {agents.map(agent => {
              const IconComponent = getAgentIcon(agent.type)
              const statusColor = getStatusColor(agent.status)
              const isSelected = selectedAgent === agent.id
              const agentTypeLabel = AGENT_TYPES[agent.type]?.label || agent.type
              
              return (
                <button
                  key={agent.id}
                  onClick={() => setSelectedAgent(isSelected ? null : agent.id)}
                  className={`w-full flex items-center gap-3 p-2 rounded-lg transition-all text-left ${
                    isSelected 
                      ? "bg-[var(--artifact-purple)]/20 ring-1 ring-[var(--artifact-purple)]" 
                      : "bg-[var(--secondary)] hover:bg-[var(--secondary)]/80"
                  }`}
                >
                  {/* Agent Icon */}
                  <div 
                    className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
                    style={{ 
                      backgroundColor: `color-mix(in srgb, ${AGENT_TYPES[agent.type].color} 20%, transparent)`,
                      color: AGENT_TYPES[agent.type].color
                    }}
                  >
                    <IconComponent size={18} />
                  </div>
                  
                  {/* Agent Info */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="font-mono text-xs font-medium text-[var(--foreground)] truncate">
                        {agentTypeLabel}
                      </p>
                      {/* Status Badge */}
                      <span 
                        className="shrink-0 px-1.5 py-0.5 rounded text-[10px] font-mono font-medium"
                        style={{ 
                          backgroundColor: `color-mix(in srgb, ${statusColor} 20%, transparent)`,
                          color: statusColor
                        }}
                      >
                        {getStatusLabel(agent.status)}
                      </span>
                    </div>
                    
                    {/* Progress */}
                    <div className="flex items-center gap-1.5 mt-1">
                      <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                        {agent.progress.current}/{agent.progress.total} tasks
                      </span>
                    </div>
                    {/* Role + AI Model (separate row) */}
                    {(agent.subType || agent.aiModel) && (
                      <div className="flex items-center gap-1.5 mt-1">
                        {agent.subType && (
                          <span
                            className="font-mono text-[10px] px-1 py-0.5 rounded uppercase"
                            style={{
                              backgroundColor: `color-mix(in srgb, ${AGENT_TYPES[agent.type]?.color || 'var(--muted-foreground)'} 12%, transparent)`,
                              color: AGENT_TYPES[agent.type]?.color || 'var(--muted-foreground)'
                            }}
                          >
                            {agent.subType}
                          </span>
                        )}
                        {agent.aiModel && (() => {
                          const info = getModelInfo(agent.aiModel)
                          return (
                            <span
                              className="font-mono text-[10px] px-1 py-0.5 rounded"
                              style={{
                                backgroundColor: `color-mix(in srgb, ${info.color} 15%, transparent)`,
                                color: info.color
                              }}
                            >
                              {info.shortLabel}
                            </span>
                          )
                        })()}
                      </div>
                    )}
                  </div>
                  
                  {/* Status Indicator Dot */}
                  <div 
                    className={`w-2.5 h-2.5 rounded-full shrink-0 ${agent.status === "running" ? "animate-pulse" : ""}`}
                    style={{ backgroundColor: statusColor }}
                  />
                </button>
              )
            })}
          </div>
        )}
      </div>
      
      {/* Token Usage Statistics — passes ``providers`` so that any
          configured-but-unused model (e.g. Anthropic wired via env
          but no call has landed yet) shows a 0-count placeholder
          card instead of being invisible. Addresses operator
          2026-04-21 report: "API key 接上但卡片只顯示 gemma4:e4b". */}
      <TokenUsageStats
        externalUsage={tokenUsage}
        configuredProviders={providers}
        budgetInfo={tokenBudget}
        providerBalances={providerBalances}
        onResetFreeze={onResetFreeze}
        onUpdateBudget={onUpdateBudget}
      />

      {/* RTK Compression Stats */}
      {compressionStats && compressionStats.compression_count > 0 && (
        <div className="border-b border-[var(--border)] px-4 py-2">
          <p className="font-mono text-[10px] text-[var(--muted-foreground)] mb-1.5 uppercase tracking-wider flex items-center gap-1.5">
            <Zap size={10} className="text-[var(--validation-emerald)]" />
            OUTPUT COMPRESSION
          </p>
          <div className="grid grid-cols-2 gap-1.5">
            <div className="p-1.5 rounded bg-[var(--secondary)]">
              <span className="font-mono text-[9px] text-[var(--muted-foreground)] block">Tokens Saved</span>
              <span className="font-mono text-sm font-semibold text-[var(--validation-emerald)]">
                {compressionStats.estimated_tokens_saved > 1000
                  ? `${(compressionStats.estimated_tokens_saved / 1000).toFixed(1)}K`
                  : compressionStats.estimated_tokens_saved}
              </span>
            </div>
            <div className="p-1.5 rounded bg-[var(--secondary)]">
              <span className="font-mono text-[9px] text-[var(--muted-foreground)] block">Avg Ratio</span>
              <span className="font-mono text-sm font-semibold text-[var(--validation-emerald)]">
                {(compressionStats.avg_ratio * 100).toFixed(0)}%
              </span>
            </div>
            <div className="p-1.5 rounded bg-[var(--secondary)]">
              <span className="font-mono text-[9px] text-[var(--muted-foreground)] block">Compressions</span>
              <span className="font-mono text-xs text-[var(--foreground)]">{compressionStats.compression_count}</span>
            </div>
            <div className="p-1.5 rounded bg-[var(--secondary)]">
              <span className="font-mono text-[9px] text-[var(--muted-foreground)] block">Lines Removed</span>
              <span className="font-mono text-xs text-[var(--foreground)]">{compressionStats.total_lines_removed}</span>
            </div>
          </div>
        </div>
      )}

      {/* LLM Provider / Model Selector — grouped per provider with a
          row label so the operator can distinguish e.g. ``Sonnet``
          served DIRECTLY via Anthropic (using ``anthropic_api_key``)
          from ``Sonnet`` served via OPENROUTER's aggregator (using
          ``openrouter_api_key`` → OpenRouter proxy → Anthropic).
          Previously all chips rendered in a single flat cluster with
          only tight vertical spacing between provider groups — with
          OpenRouter configured, the user saw "Claude" chips in two
          different rows with no hint of which row was which
          provider (reported 2026-04-22). */}
      {providers && providers.length > 0 && onSwitchProvider && (
        <div className="border-b border-[var(--border)] px-4 py-2">
          <p className="font-mono text-[10px] text-[var(--muted-foreground)] mb-1.5 uppercase tracking-wider flex items-center gap-1.5">
            <Sparkles size={10} className="text-[var(--neural-blue)]" />
            LLM MODEL
          </p>
          <div className="space-y-2">
            {providers.filter(p => p.configured).map(p => {
              const isActive = p.id === activeProvider
              return (
                <div key={p.id}>
                  {/* Row header: provider name + active-indicator
                      dot. Compact (10px) so it doesn't dominate the
                      chip row beneath it, but always visible so
                      operator never has to guess which provider a
                      chip was served by. */}
                  <div className="flex items-center gap-1.5 mb-1">
                    <span
                      className={`w-1 h-1 rounded-full shrink-0 ${isActive ? "opacity-100" : "opacity-40"}`}
                      style={{ backgroundColor: isActive ? "var(--validation-emerald)" : "var(--muted-foreground)" }}
                    />
                    <span className="font-mono text-[9px] uppercase tracking-wider text-[var(--muted-foreground)] shrink-0">
                      {p.name}
                    </span>
                    <span
                      className="h-px flex-1"
                      style={{ backgroundColor: "color-mix(in srgb, var(--muted-foreground) 20%, transparent)" }}
                    />
                  </div>
                  <div className="flex flex-wrap gap-1 pl-2.5">
                    {p.models.map(m => {
                      const isCurrent = isActive && m === activeModel
                      const info = getModelInfo(m)
                      return (
                        <button
                          key={m}
                          onClick={() => onSwitchProvider(p.id, m)}
                          title={info.label}
                          className={`px-1.5 py-0.5 rounded font-mono text-[9px] transition-all ${
                            isCurrent
                              ? "ring-1 ring-[var(--neural-blue)] text-[var(--foreground)]"
                              : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                          }`}
                          style={isCurrent ? {
                            backgroundColor: `color-mix(in srgb, ${info.color} 20%, transparent)`,
                            color: info.color,
                          } : {}}
                        >
                          {info.shortLabel}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* ZZ.C1 #305-1 checkbox 3 (2026-04-24): "System Prompt Versions"
          launcher. Mounted directly under the LLM MODEL block because
          the prompt body is the other half of the model selection — what
          the LLM sees on every call. Operator picks an agent subtype,
          scrolls the timeline of captured snapshots, and picks two rows
          for a side-by-side diff (additions green / deletions red).
          Lives outside the LLM MODEL conditional so the launcher remains
          accessible even before any provider is configured (the drawer
          itself only needs ``GET /runtime/prompts`` which is unrelated
          to provider state). */}
      <div className="border-b border-[var(--border)] px-3 py-2">
        <button
          type="button"
          data-testid="prompt-version-drawer-launcher"
          onClick={() => setShowPromptDrawer(true)}
          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 rounded font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--artifact-purple)] bg-[var(--secondary)]/40 hover:bg-[var(--secondary)] border border-[var(--border)]/60 transition-colors"
          title="Open the system-prompt version drawer"
        >
          <History size={10} />
          System Prompt Versions
          <ChevronDown size={8} className="opacity-50" />
        </button>
      </div>

      {/* Fallback Chain + Health Status */}
      {providerHealth && providerHealth.health.length > 0 && (
        <div className="border-b border-[var(--border)] px-3 py-2">
          <p className="font-mono text-[10px] text-[var(--muted-foreground)] mb-1.5 uppercase tracking-wider flex items-center gap-1.5">
            <Shield size={10} className="text-[var(--hardware-orange)]" />
            FAILOVER CHAIN
          </p>
          <div className="space-y-0.5">
            {providerHealth.health.map((h, idx) => {
              const statusColor = h.status === "active" ? "var(--validation-emerald)"
                : h.status === "available" ? "var(--neural-blue)"
                : h.status === "cooldown" ? "var(--hardware-orange)"
                : "var(--muted-foreground)"
              return (
                <div key={h.id} className="flex items-center gap-1.5 py-0.5">
                  <span className="font-mono text-[8px] text-[var(--muted-foreground)] w-3 text-right">{idx + 1}</span>
                  <div className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: statusColor }} />
                  <span className="font-mono text-[9px] flex-1 min-w-0 truncate" style={{ color: statusColor }}>{h.name}</span>
                  {h.status === "cooldown" && (
                    <span className="font-mono text-[8px] text-[var(--hardware-orange)]">{h.cooldown_remaining}s</span>
                  )}
                  {h.status === "unconfigured" && (
                    <span className="font-mono text-[8px] text-[var(--muted-foreground)]">N/A</span>
                  )}
                  {/* Move up/down buttons */}
                  {onUpdateFallbackChain && (
                    <div className="flex gap-0.5 shrink-0">
                      <button
                        disabled={idx === 0}
                        onClick={() => {
                          const chain = [...providerHealth.chain]
                          if (idx > 0) { [chain[idx - 1], chain[idx]] = [chain[idx], chain[idx - 1]] }
                          onUpdateFallbackChain(chain)
                        }}
                        className="p-0.5 rounded text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] disabled:opacity-20 transition-colors"
                        title="Move up"
                      >
                        <ChevronUp size={8} />
                      </button>
                      <button
                        disabled={idx === providerHealth.health.length - 1}
                        onClick={() => {
                          const chain = [...providerHealth.chain]
                          if (idx < chain.length - 1) { [chain[idx], chain[idx + 1]] = [chain[idx + 1], chain[idx]] }
                          onUpdateFallbackChain(chain)
                        }}
                        className="p-0.5 rounded text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] disabled:opacity-20 transition-colors"
                        title="Move down"
                      >
                        <ChevronDown size={8} />
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Handoff Chain */}
      <HandoffTimeline handoffs={handoffs} onLoadHandoffs={onLoadHandoffs} />

      {/* Suggestions Panel */}
      {pendingSuggestions.length > 0 && (
        <div className="border-b border-[var(--border)] p-3 space-y-2 max-h-48 overflow-auto">
          <p className="font-mono text-xs text-[var(--artifact-purple)] flex items-center gap-1">
            <Sparkles size={12} />
            SUGGESTIONS ({pendingSuggestions.length})
          </p>
          {pendingSuggestions.map(suggestion => (
            <div 
              key={suggestion.id}
              className={`p-2 rounded holo-glass-simple border-l-2 ${
                suggestion.priority === "high" 
                  ? "border-l-[var(--critical-red)]" 
                  : suggestion.priority === "medium"
                    ? "border-l-[var(--hardware-orange)]"
                    : "border-l-[var(--neural-blue)]"
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className="font-mono text-xs font-semibold text-[var(--foreground)] truncate">
                    {suggestion.title}
                  </p>
                  <p className="font-mono text-xs text-[var(--muted-foreground)] truncate">
                    {suggestion.description}
                  </p>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <button
                    onClick={() => handleAcceptSuggestion(suggestion)}
                    className="p-1 rounded bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/30 transition-colors"
                    title="Accept"
                  >
                    <Check size={12} />
                  </button>
                  <button
                    onClick={() => handleRejectSuggestion(suggestion)}
                    className="p-1 rounded bg-[var(--critical-red)]/20 text-[var(--critical-red)] hover:bg-[var(--critical-red)]/30 transition-colors"
                    title="Reject"
                  >
                    <X size={12} />
                  </button>
                  {suggestion.type === "assign" && (
                    <button
                      onClick={() => handleReassign(suggestion)}
                      className="p-1 rounded bg-[var(--hardware-orange)]/20 text-[var(--hardware-orange)] hover:bg-[var(--hardware-orange)]/30 transition-colors"
                      title="Force Reassign"
                    >
                      <Target size={12} />
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      
      {/* Force Assign Modal */}
      {showForceAssign && (
        <div className="border-b border-[var(--border)] p-3 bg-[var(--hardware-orange)]/10">
          <p className="font-mono text-xs text-[var(--hardware-orange)] mb-2 flex items-center gap-1">
            <Target size={12} />
            FORCE ASSIGN TO:
          </p>
          <div className="flex flex-wrap gap-1">
            {agents.map(agent => (
              <button
                key={agent.id}
                onClick={() => handleForceAssign(agent.id)}
                className="px-2 py-1 rounded text-xs font-mono bg-[var(--secondary)] hover:bg-[var(--hardware-orange)]/20 transition-colors flex items-center gap-1.5"
                style={{ color: AGENT_TYPES[agent.type].color }}
              >
                <span>{agent.name}</span>
                {agent.subType && (
                  <span className="text-[9px] px-1 py-0.5 rounded uppercase" style={{ color: AGENT_TYPES[agent.type]?.color }}>
                    {agent.subType}
                  </span>
                )}
                {agent.aiModel && (() => {
                  const info = getModelInfo(agent.aiModel)
                  return (
                    <span
                      className="text-[9px] px-1 py-0.5 rounded"
                      style={{
                        backgroundColor: `color-mix(in srgb, ${info.color} 20%, transparent)`,
                        color: info.color
                      }}
                    >
                      {info.shortLabel}
                    </span>
                  )
                })()}
              </button>
            ))}
            <button
              onClick={() => { setShowForceAssign(false); setForceAssignTask(null); }}
              className="px-2 py-1 rounded text-xs font-mono text-[var(--muted-foreground)] hover:text-[var(--critical-red)]"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      
      {/* Chat Messages */}
      <div className="p-3 space-y-2">
        {messages.map(message => (
          <div 
            key={message.id}
            className={`flex gap-2 ${message.role === "user" ? "justify-end" : "justify-start"}`}
          >
            {message.role !== "user" && (
              <div className={`w-6 h-6 rounded-full flex items-center justify-center shrink-0 ${
                message.role === "orchestrator" 
                  ? "bg-[var(--artifact-purple)]/20 text-[var(--artifact-purple)]" 
                  : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
              }`}>
                {message.role === "orchestrator" ? <Bot size={12} /> : <Zap size={12} />}
              </div>
            )}
            <div className={`max-w-[80%] ${
              message.role === "user" 
                ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]" 
                : message.role === "orchestrator"
                  ? "bg-[var(--artifact-purple)]/10 text-[var(--foreground)]"
                  : "bg-[var(--secondary)] text-[var(--muted-foreground)]"
            } rounded px-3 py-2`}>
              <p className="font-mono text-xs whitespace-pre-line">{message.content}</p>
              <p className="font-mono text-xs opacity-50 mt-1" suppressHydrationWarning>{message.timestamp}</p>
            </div>
            {message.role === "user" && (
              <div className="w-6 h-6 rounded-full flex items-center justify-center shrink-0 bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]">
                <User size={12} />
              </div>
            )}
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>
      
      </div>{/* End Scrollable Content Area */}
      
      {/* Command Input with Slash Autocomplete */}
      <div className="p-2 border-t border-[var(--border)] shrink-0 bg-[var(--background)]">
        <div className="relative">
          {/* Autocomplete dropdown (above input) */}
          {slashSuggestions.length > 0 && (
            <div className="absolute left-0 right-0 bottom-full mb-1 z-50 bg-[var(--card)] border border-[var(--border)] rounded-md shadow-lg overflow-hidden max-h-[180px] overflow-y-auto">
              {slashSuggestions.map((cmd, idx) => {
                const catColor = slashCategoryColors[cmd.category] || "var(--muted-foreground)"
                return (
                  <button
                    key={cmd.name}
                    type="button"
                    onMouseDown={(e) => { e.preventDefault(); setInputValue(`/${cmd.name} `); setSlashSuggestions([]) }}
                    className={`w-full flex items-center gap-1.5 px-2 py-1 text-left transition-colors ${
                      idx === slashSelectedIdx ? "bg-[var(--neural-blue)]/10" : "hover:bg-[var(--secondary)]"
                    }`}
                  >
                    <span className="font-mono text-[8px] px-1 rounded" style={{ color: catColor }}>{cmd.category.slice(0, 3).toUpperCase()}</span>
                    <span className="font-mono text-[10px] text-[var(--neural-blue)]">/{cmd.name}</span>
                    <span className="font-mono text-[9px] text-[var(--muted-foreground)] ml-auto truncate max-w-[50%]">{cmd.description}</span>
                  </button>
                )
              })}
            </div>
          )}
          {slashSuggestions.length === 0 && skillSuggestions.length > 0 && (
            <div className="absolute left-0 right-0 bottom-full mb-1 z-50 bg-[var(--card)] border border-[var(--border)] rounded-md shadow-lg overflow-hidden max-h-[180px] overflow-y-auto">
              {skillSuggestions.map((skill, idx) => (
                <button
                  key={skill.name}
                  type="button"
                  onMouseDown={(e) => { e.preventDefault(); pickSkillMention(skill.name) }}
                  className={`w-full flex items-center gap-1.5 px-2 py-1 text-left transition-colors ${
                    idx === skillSelectedIdx ? "bg-[var(--artifact-purple)]/10" : "hover:bg-[var(--secondary)]"
                  }`}
                >
                  <span className="font-mono text-[8px] px-1 rounded text-[var(--artifact-purple)]">{skill.scope.toUpperCase()}</span>
                  <span className="font-mono text-[10px] text-[var(--artifact-purple)]">@{skill.name}</span>
                  <span className="font-mono text-[9px] text-[var(--muted-foreground)] ml-auto truncate max-w-[50%]">{skill.description}</span>
                </button>
              ))}
            </div>
          )}
          <form onSubmit={handleSubmit} className="flex items-center gap-1.5 fui-input px-2 py-1.5">
            <span className="font-mono text-xs text-[var(--muted-foreground)] shrink-0">{">"}</span>
            <input
              ref={inputRef}
              type="text"
              value={inputValue}
              onChange={e => {
                const value = e.target.value
                setInputValue(value)
                if (value.startsWith("/")) {
                  const matches = slashMatchCommands(value)
                  setSlashSuggestions(matches.slice(0, 6))
                  setSlashSelectedIdx(0)
                  setSkillSuggestions([])
                } else {
                  setSlashSuggestions([])
                  updateSkillSuggestions(value)
                }
              }}
              onKeyDown={e => {
                if (slashSuggestions.length > 0) {
                  if (e.key === "ArrowDown") { e.preventDefault(); setSlashSelectedIdx(i => Math.min(i + 1, slashSuggestions.length - 1)) }
                  else if (e.key === "ArrowUp") { e.preventDefault(); setSlashSelectedIdx(i => Math.max(i - 1, 0)) }
                  else if (e.key === "Tab") { e.preventDefault(); setInputValue(`/${slashSuggestions[slashSelectedIdx]?.name} `); setSlashSuggestions([]) }
                  else if (e.key === "Escape") { setSlashSuggestions([]) }
                } else if (skillSuggestions.length > 0) {
                  if (e.key === "ArrowDown") { e.preventDefault(); setSkillSelectedIdx(i => Math.min(i + 1, skillSuggestions.length - 1)) }
                  else if (e.key === "ArrowUp") { e.preventDefault(); setSkillSelectedIdx(i => Math.max(i - 1, 0)) }
                  else if (e.key === "Tab") {
                    e.preventDefault()
                    const skill = skillSuggestions[skillSelectedIdx]
                    if (skill) pickSkillMention(skill.name)
                  }
                  else if (e.key === "Escape") { setSkillSuggestions([]) }
                }
              }}
              onBlur={() => setTimeout(() => { setSlashSuggestions([]); setSkillSuggestions([]) }, 150)}
              placeholder="Ask or type /command ..."
              className="flex-1 min-w-0 bg-transparent font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus-visible:outline-none"
            />
            <button
              type="submit"
              className="p-1.5 rounded bg-[var(--artifact-purple)] text-white hover:bg-[var(--artifact-purple)]/80 transition-colors shrink-0"
            >
              <Send size={12} />
            </button>
          </form>
        </div>
      </div>

      {/* ZZ.C1 #305-1 checkbox 3: System Prompt Versions drawer.
          ``fixed inset-0`` so it overlays the panel without disturbing
          its scroll state; mounted at the panel root so React doesn't
          unmount it when the operator scrolls the LLM MODEL block off-
          screen mid-edit. */}
      <PromptVersionDrawer
        open={showPromptDrawer}
        onClose={() => setShowPromptDrawer(false)}
      />
    </div>
  )
}

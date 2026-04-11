"use client"

import { useState, useRef, useEffect, useCallback } from "react"
import { 
  Send, 
  Bot, 
  User, 
  Sparkles,
  Zap,
  AlertTriangle,
  Check,
  X,
  ArrowRight,
  Target,
  Cpu,
  Code,
  TestTube,
  FileBarChart,
  Settings,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  Crown
} from "lucide-react"
import type { Agent, AgentStatus, AIModel } from "./agent-matrix-wall"
import type { Task, TaskStatus } from "./task-backlog"
import { AGENT_TYPES, AI_MODEL_INFO, getModelInfo } from "./agent-matrix-wall"
import { TokenUsageStats } from "./token-usage-stats"

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
  onResetFreeze?: () => void
  onUpdateBudget?: (updates: Record<string, number | string>) => void
  onRefresh?: () => void
  activeProvider?: string
  activeModel?: string
  providers?: { id: string; name: string; models: string[]; configured: boolean }[]
  onSwitchProvider?: (provider: string, model?: string) => void
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
  onUpdateAgentStatus,
  onCompleteTask,
  externalMessages = [],
  onSendCommand,
  tokenUsage,
  tokenBudget,
  onResetFreeze,
  onUpdateBudget,
  onRefresh,
  activeProvider,
  activeModel,
  providers,
  onSwitchProvider,
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
        setMessages(prev => [...prev, lastExternal])
      }
    }
  }, [externalMessages, messages])
  
  const [suggestions, setSuggestions] = useState<AISuggestion[]>([])
  const [inputValue, setInputValue] = useState("")
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const [showForceAssign, setShowForceAssign] = useState(false)
  const [forceAssignTask, setForceAssignTask] = useState<string | null>(null)
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [showAgentGrid, setShowAgentGrid] = useState(true)
  
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  
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
    generateSuggestions()
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
    
    // If external command handler is available, use it for agent-related commands
    const cmd = inputValue.toLowerCase().trim()
    if (onSendCommand && (
      cmd.startsWith("add ") || 
      cmd.startsWith("spawn ") || 
      cmd.startsWith("create ") ||
      cmd.startsWith("remove ") ||
      cmd.startsWith("stop ")
    )) {
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
      onSpawnAgent(suggestion.agentType)
      setMessages(prev => [...prev, {
        id: `sys-${Date.now()}`,
        role: "system",
        content: `Spawning ${AGENT_TYPES[suggestion.agentType].label} agent...`,
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
    <div className="holo-glass h-full flex flex-col min-h-0 overflow-hidden corner-brackets-full holo-flicker">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[var(--border)] relative">
        {/* Subtle holographic shimmer */}
        <div className="absolute inset-0 holo-shimmer opacity-20 pointer-events-none" />
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[var(--artifact-purple)] pulse-purple pulse-ring" />
            <Crown size={14} className="text-[var(--artifact-purple)] text-glow-purple" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--artifact-purple)]">
              ORCHESTRATOR
            </h2>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={(e) => { e.stopPropagation(); setIsAnalyzing(true); generateSuggestions(); onRefresh?.(); setTimeout(() => setIsAnalyzing(false), 1000); }}
              className={`relative z-20 p-1.5 rounded transition-colors cursor-pointer ${isAnalyzing ? "bg-[var(--artifact-purple)]/40 text-[var(--artifact-purple)]" : "bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--artifact-purple)]"}`}
              title="Analyze and suggest"
            >
              <RefreshCw size={12} className={isAnalyzing ? "animate-spin" : ""} />
            </button>
          </div>
        </div>
        <p className="font-mono text-xs text-[var(--muted-foreground)] mt-1">
          Central AI Coordinator
        </p>
      </div>
      
      {/* Scrollable Content Area */}
      <div className="flex-1 min-h-0 overflow-y-auto">
      
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
      
      {/* Token Usage Statistics */}
      <TokenUsageStats externalUsage={tokenUsage} budgetInfo={tokenBudget} onResetFreeze={onResetFreeze} onUpdateBudget={onUpdateBudget} />

      {/* LLM Provider / Model Selector */}
      {providers && providers.length > 0 && onSwitchProvider && (
        <div className="border-b border-[var(--border)] px-4 py-2">
          <p className="font-mono text-[10px] text-[var(--muted-foreground)] mb-1.5 uppercase tracking-wider flex items-center gap-1.5">
            <Sparkles size={10} className="text-[var(--neural-blue)]" />
            LLM MODEL
          </p>
          <div className="space-y-1.5">
            {providers.filter(p => p.configured).map(p => {
              const isActive = p.id === activeProvider
              return (
                <div key={p.id}>
                  <div className="flex flex-wrap gap-1">
                    {p.models.map(m => {
                      const isCurrent = isActive && m === activeModel
                      const info = getModelInfo(m)
                      return (
                        <button
                          key={m}
                          onClick={() => onSwitchProvider(p.id, m)}
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
      
      {/* Command Input */}
      <div className="p-2 border-t border-[var(--border)] shrink-0 bg-[var(--background)]">
        <form onSubmit={handleSubmit} className="flex items-center gap-1.5 fui-input px-2 py-1.5">
          <span className="font-mono text-xs text-[var(--muted-foreground)] shrink-0">{">"}</span>
          <input
            ref={inputRef}
            type="text"
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            placeholder="Ask orchestrator ..."
            className="flex-1 min-w-0 bg-transparent font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none"
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
  )
}

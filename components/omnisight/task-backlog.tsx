"use client"

import { useState, useRef, useEffect, useCallback } from "react"
import { createPortal } from "react-dom"
import { 
  Plus, 
  Send, 
  Bot, 
  User, 
  CheckCircle2, 
  Circle, 
  Clock, 
  ArrowRight, 
  Sparkles,
  Trash2,
  Play,
  Pause,
  RotateCcw,
  ChevronDown,
  ChevronUp,
  Target,
  Zap,
  AlertTriangle,
  X
} from "lucide-react"
import type { Agent, AgentStatus } from "./agent-matrix-wall"

// Task types
export type TaskPriority = "critical" | "high" | "medium" | "low"
export type TaskStatus = "backlog" | "analyzing" | "assigned" | "in_progress" | "in_review" | "completed" | "blocked"

export interface Task {
  id: string
  title: string
  description?: string
  priority: TaskPriority
  status: TaskStatus
  assignedAgentId?: string
  createdAt: string
  completedAt?: string
  aiAnalysis?: string
  suggestedAgentType?: Agent["type"]
  externalIssueId?: string
  issueUrl?: string
  acceptanceCriteria?: string
  labels?: string[]
}

// Chat message types
export interface ChatMessage {
  id: string
  role: "user" | "assistant" | "system"
  content: string
  timestamp: string
  taskReferences?: string[] // IDs of referenced tasks
  agentReferences?: string[] // IDs of referenced agents
  action?: {
    type: "assign" | "create" | "complete" | "analyze"
    taskId?: string
    agentId?: string
  }
}

interface TaskBacklogProps {
  agents: Agent[]
  tasks?: Task[]
  onAssignTask?: (taskId: string, agentId: string) => void
  onCreateAgent?: (type: Agent["type"], taskId?: string) => void
  onUpdateAgentStatus?: (agentId: string, status: AgentStatus, thoughtChain?: string) => void
  onAddTask?: (title: string, priority: string) => void
}

// Priority colors
function getPriorityColor(priority: TaskPriority): string {
  switch (priority) {
    case "critical": return "var(--critical-red)"
    case "high": return "var(--hardware-orange)"
    case "medium": return "var(--neural-blue)"
    case "low": return "var(--muted-foreground)"
  }
}

// Status colors
function getStatusColor(status: TaskStatus): string {
  switch (status) {
    case "completed": return "var(--validation-emerald)"
    case "in_review": return "#f59e0b"
    case "in_progress": return "var(--neural-blue)"
    case "assigned": return "var(--artifact-purple)"
    case "analyzing": return "var(--hardware-orange)"
    case "blocked": return "var(--critical-red)"
    default: return "var(--muted-foreground)"
  }
}

// AI Analysis simulation
function analyzeTask(task: Task, agents: Agent[]): { analysis: string; suggestedType: Agent["type"] | null; existingAgent: Agent | null } {
  const title = task.title.toLowerCase()
  const desc = (task.description || "").toLowerCase()
  const combined = `${title} ${desc}`
  
  // Determine suggested agent type based on keywords
  let suggestedType: Agent["type"] | null = null
  if (combined.includes("firmware") || combined.includes("driver") || combined.includes("boot") || combined.includes("flash")) {
    suggestedType = "firmware"
  } else if (combined.includes("codec") || combined.includes("encode") || combined.includes("compile") || combined.includes("build") || combined.includes("software")) {
    suggestedType = "software"
  } else if (combined.includes("test") || combined.includes("valid") || combined.includes("check") || combined.includes("verify")) {
    suggestedType = "validator"
  } else if (combined.includes("report") || combined.includes("log") || combined.includes("document") || combined.includes("export")) {
    suggestedType = "reporter"
  }
  
  // Check if any existing agent could handle this
  const existingAgent = agents.find(a => 
    a.type === suggestedType && 
    (a.status === "idle" || a.status === "success")
  )
  
  // Check if task might already be done
  const completedAgent = agents.find(a => 
    a.type === suggestedType && 
    a.status === "success"
  )
  
  let analysis = ""
  if (completedAgent && suggestedType) {
    analysis = `Task appears related to ${suggestedType} operations. ${completedAgent.name} has already completed similar work. Recommend reviewing existing output before proceeding.`
  } else if (existingAgent) {
    analysis = `Identified as ${suggestedType || "general"} task. ${existingAgent.name} is available and suitable for this assignment.`
  } else if (suggestedType) {
    analysis = `This task requires a ${suggestedType} agent. No suitable agent currently available - recommend spawning a new ${suggestedType.toUpperCase()}_AGENT.`
  } else {
    analysis = `Task scope is ambiguous. Consider breaking down into specific firmware, software, validation, or reporting subtasks for optimal agent assignment.`
  }
  
  return { analysis, suggestedType, existingAgent }
}

// Empty defaults — real data from backend via useEngine
const emptyTasks: Task[] = []

const initialChatHistory: ChatMessage[] = [
  {
    id: "msg-sys-1",
    role: "system",
    content: "Task backlog initialized. Awaiting data from backend...",
    timestamp: "--:--:--"
  }
]

export function TaskBacklog({ agents, tasks: externalTasks, onAssignTask, onCreateAgent, onUpdateAgentStatus, onAddTask }: TaskBacklogProps) {
  const [tasks, setTasks] = useState<Task[]>(externalTasks ?? emptyTasks)

  // Sync when external tasks change (from backend)
  useEffect(() => {
    if (externalTasks && externalTasks.length > 0) {
      setTasks(externalTasks)
    }
  }, [externalTasks])
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>(initialChatHistory)
  const [inputValue, setInputValue] = useState("")
  const [newTaskTitle, setNewTaskTitle] = useState("")
  const [newTaskPriority, setNewTaskPriority] = useState<TaskPriority>("medium")
  const [showAddTask, setShowAddTask] = useState(false)
  const [expandedView, setExpandedView] = useState<"tasks" | "chat">("tasks")
  const [isAnalyzing, setIsAnalyzing] = useState(false)
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set())
  
  const chatEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  
  // Toggle task expansion
  const toggleTaskExpanded = useCallback((taskId: string) => {
    setExpandedTasks(prev => {
      const next = new Set(prev)
      if (next.has(taskId)) {
        next.delete(taskId)
      } else {
        next.add(taskId)
      }
      return next
    })
  }, [])
  
  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [chatMessages])

  // Add new task
  const handleAddTask = useCallback(() => {
    if (!newTaskTitle.trim()) return

    // Call backend API if available
    if (onAddTask) {
      onAddTask(newTaskTitle, newTaskPriority)
    } else {
      // Fallback: local only
      const newTask: Task = {
        id: `task-${Date.now()}`,
        title: newTaskTitle,
        priority: newTaskPriority,
        status: "backlog",
        createdAt: new Date().toTimeString().slice(0, 8)
      }
      setTasks(prev => [newTask, ...prev])
    }

    setNewTaskTitle("")
    setShowAddTask(false)
  }, [newTaskTitle, newTaskPriority, onAddTask])

  // Analyze task and add AI message
  const analyzeAndSuggest = useCallback((taskId: string) => {
    const task = tasks.find(t => t.id === taskId) || (taskId === "new" ? null : null)
    if (!task && taskId !== "new") {
      // Find in current state
      setTasks(prev => {
        const foundTask = prev.find(t => t.id === taskId)
        if (foundTask) {
          const { analysis, suggestedType, existingAgent } = analyzeTask(foundTask, agents)
          
          // Update task with analysis
          const updatedTasks = prev.map(t => 
            t.id === taskId 
              ? { ...t, status: "analyzing" as TaskStatus, aiAnalysis: analysis, suggestedAgentType: suggestedType || undefined }
              : t
          )
          
          // Add chat message
          const aiMessage: ChatMessage = {
            id: `msg-${Date.now()}`,
            role: "assistant",
            content: `**Analyzing:** "${foundTask.title}"\n\n${analysis}`,
            timestamp: new Date().toTimeString().slice(0, 8),
            taskReferences: [taskId],
            agentReferences: existingAgent ? [existingAgent.id] : undefined,
            action: existingAgent ? {
              type: "assign",
              taskId,
              agentId: existingAgent.id
            } : suggestedType ? {
              type: "create",
              taskId
            } : undefined
          }
          
          setChatMessages(prev => [...prev, aiMessage])
          
          // Transition back to backlog or assigned after analysis
          setTimeout(() => {
            setTasks(p => p.map(t => 
              t.id === taskId && t.status === "analyzing"
                ? { ...t, status: "backlog" as TaskStatus }
                : t
            ))
          }, 2000)
          
          return updatedTasks
        }
        return prev
      })
    }
  }, [agents, tasks])

  // Handle chat input
  const handleSendMessage = useCallback(() => {
    if (!inputValue.trim()) return
    
    const userMessage: ChatMessage = {
      id: `msg-user-${Date.now()}`,
      role: "user",
      content: inputValue,
      timestamp: new Date().toTimeString().slice(0, 8)
    }
    
    setChatMessages(prev => [...prev, userMessage])
    setInputValue("")
    setIsAnalyzing(true)
    
    // Parse user intent and generate AI response
    const input = inputValue.toLowerCase()
    
    setTimeout(() => {
      let aiResponse: ChatMessage
      
      // Check for task creation intent
      if (input.includes("add") || input.includes("create") || input.includes("new task")) {
        const titleMatch = inputValue.match(/["']([^"']+)["']/) || inputValue.match(/(?:add|create|new task[:]?)\s+(.+)/i)
        const title = titleMatch ? titleMatch[1] : "New Task"
        
        // Determine priority from keywords
        let priority: TaskPriority = "medium"
        if (input.includes("critical") || input.includes("urgent")) priority = "critical"
        else if (input.includes("high") || input.includes("important")) priority = "high"
        else if (input.includes("low")) priority = "low"
        
        const newTask: Task = {
          id: `task-${Date.now()}`,
          title,
          priority,
          status: "analyzing",
          createdAt: new Date().toTimeString().slice(0, 8)
        }
        
        setTasks(prev => [newTask, ...prev])
        
        const { analysis, suggestedType, existingAgent } = analyzeTask(newTask, agents)
        
        aiResponse = {
          id: `msg-ai-${Date.now()}`,
          role: "assistant",
          content: `**Task Created:** "${title}" [${priority.toUpperCase()}]\n\n${analysis}${existingAgent ? `\n\nRecommend assigning to **${existingAgent.name}**. Shall I proceed?` : suggestedType ? `\n\nNo suitable ${suggestedType} agent available. Spawn new agent?` : ""}`,
          timestamp: new Date().toTimeString().slice(0, 8),
          taskReferences: [newTask.id],
          action: existingAgent ? { type: "assign", taskId: newTask.id, agentId: existingAgent.id } : undefined
        }
        
        // Update task with analysis
        setTimeout(() => {
          setTasks(prev => prev.map(t => 
            t.id === newTask.id 
              ? { ...t, status: "backlog", aiAnalysis: analysis, suggestedAgentType: suggestedType || undefined }
              : t
          ))
        }, 1500)
        
      } else if (input.includes("assign") || input.includes("give") || input.includes("send to")) {
        // Find which task and agent user is referring to
        const backlogTasks = tasks.filter(t => t.status === "backlog" || t.status === "analyzing")
        const availableAgents = agents.filter(a => a.status === "idle" || a.status === "success")
        
        if (backlogTasks.length === 0) {
          aiResponse = {
            id: `msg-ai-${Date.now()}`,
            role: "assistant",
            content: "No unassigned tasks in the backlog. Add a new task or check the task list.",
            timestamp: new Date().toTimeString().slice(0, 8)
          }
        } else if (availableAgents.length === 0) {
          aiResponse = {
            id: `msg-ai-${Date.now()}`,
            role: "assistant",
            content: "No agents currently available. All agents are busy or in error state. Consider spawning a new agent or waiting for current operations to complete.",
            timestamp: new Date().toTimeString().slice(0, 8)
          }
        } else {
          // Auto-match first backlog task with suitable agent
          const task = backlogTasks[0]
          const matchingAgent = task.suggestedAgentType 
            ? availableAgents.find(a => a.type === task.suggestedAgentType) || availableAgents[0]
            : availableAgents[0]
          
          setTasks(prev => prev.map(t => 
            t.id === task.id 
              ? { ...t, status: "assigned", assignedAgentId: matchingAgent.id }
              : t
          ))
          
          onAssignTask?.(task.id, matchingAgent.id)
          onUpdateAgentStatus?.(matchingAgent.id, "running", `Assigned task: ${task.title}`)
          
          aiResponse = {
            id: `msg-ai-${Date.now()}`,
            role: "assistant",
            content: `**Task Assigned**\n\n"${task.title}" has been assigned to **${matchingAgent.name}**.\n\nAgent status updated to RUNNING. Monitoring progress...`,
            timestamp: new Date().toTimeString().slice(0, 8),
            taskReferences: [task.id],
            agentReferences: [matchingAgent.id],
            action: { type: "assign", taskId: task.id, agentId: matchingAgent.id }
          }
        }
        
      } else if (input.includes("status") || input.includes("progress") || input.includes("what")) {
        // Generate status report
        const inProgress = tasks.filter(t => t.status === "in_progress").length
        const completed = tasks.filter(t => t.status === "completed").length
        const backlog = tasks.filter(t => t.status === "backlog").length
        const runningAgents = agents.filter(a => a.status === "running").length
        
        aiResponse = {
          id: `msg-ai-${Date.now()}`,
          role: "assistant",
          content: `**Status Report**\n\n` +
            `Tasks: ${backlog} backlog, ${inProgress} in progress, ${completed} completed\n` +
            `Agents: ${runningAgents}/${agents.length} active\n\n` +
            `${inProgress > 0 ? `Currently processing: ${tasks.filter(t => t.status === "in_progress").map(t => t.title).join(", ")}` : "No tasks currently in progress."}`,
          timestamp: new Date().toTimeString().slice(0, 8)
        }
        
      } else if (input.includes("spawn") || input.includes("new agent")) {
        // Determine agent type to spawn
        let agentType: Agent["type"] = "custom"
        if (input.includes("firmware")) agentType = "firmware"
        else if (input.includes("software") || input.includes("codec")) agentType = "software"
        else if (input.includes("validator") || input.includes("test")) agentType = "validator"
        else if (input.includes("reporter")) agentType = "reporter"
        
        onCreateAgent?.(agentType)
        
        aiResponse = {
          id: `msg-ai-${Date.now()}`,
          role: "assistant",
          content: `**Agent Spawned**\n\nNew ${agentType.toUpperCase()}_AGENT has been initialized and is standing by for task assignment.`,
          timestamp: new Date().toTimeString().slice(0, 8)
        }
        
      } else if (input.includes("complete") || input.includes("done") || input.includes("finish")) {
        // Mark referenced task as complete
        const inProgressTasks = tasks.filter(t => t.status === "in_progress" || t.status === "assigned")
        if (inProgressTasks.length > 0) {
          const task = inProgressTasks[0]
          setTasks(prev => prev.map(t => 
            t.id === task.id 
              ? { ...t, status: "completed", completedAt: new Date().toTimeString().slice(0, 8) }
              : t
          ))
          
          if (task.assignedAgentId) {
            onUpdateAgentStatus?.(task.assignedAgentId, "success", `Task completed: ${task.title}`)
          }
          
          aiResponse = {
            id: `msg-ai-${Date.now()}`,
            role: "assistant",
            content: `**Task Completed**\n\n"${task.title}" has been marked as complete.${task.assignedAgentId ? ` Agent status updated.` : ""}`,
            timestamp: new Date().toTimeString().slice(0, 8),
            taskReferences: [task.id],
            action: { type: "complete", taskId: task.id }
          }
        } else {
          aiResponse = {
            id: `msg-ai-${Date.now()}`,
            role: "assistant",
            content: "No tasks currently in progress to complete.",
            timestamp: new Date().toTimeString().slice(0, 8)
          }
        }
        
      } else {
        // Default analysis response
        aiResponse = {
          id: `msg-ai-${Date.now()}`,
          role: "assistant",
          content: `I can help you manage tasks and agents. Try:\n\n` +
            `- "Add task: [description]" - Create new task\n` +
            `- "Assign" - Auto-assign backlog tasks\n` +
            `- "Status" - Get progress report\n` +
            `- "Spawn [type] agent" - Create new agent\n` +
            `- "Complete" - Mark task as done`,
          timestamp: new Date().toTimeString().slice(0, 8)
        }
      }
      
      setChatMessages(prev => [...prev, aiResponse])
      setIsAnalyzing(false)
    }, 1000)
  }, [inputValue, tasks, agents, onAssignTask, onCreateAgent, onUpdateAgentStatus])

  // Delete task
  const handleDeleteTask = useCallback((taskId: string) => {
    setTasks(prev => prev.filter(t => t.id !== taskId))
  }, [])

  // Get agent name
  const getAgentName = (agentId: string) => {
    return agents.find(a => a.id === agentId)?.name || agentId
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 holo-glass-simple mb-3 corner-brackets data-stream">
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[var(--hardware-orange)] pulse-orange pulse-ring" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--hardware-orange)]">
              TASK BACKLOG
            </h2>
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); setShowAddTask(true) }}
            className="relative z-20 flex items-center gap-1.5 px-2 py-1 rounded text-xs font-mono bg-[var(--hardware-orange)]/20 hover:bg-[var(--hardware-orange)]/40 text-[var(--hardware-orange)] transition-colors cursor-pointer"
          >
            <Plus size={12} />
            ADD
          </button>
        </div>
        <div className="flex items-center gap-3 mt-2 text-xs font-mono">
          <span className="text-[var(--muted-foreground)]">{tasks.length} TASKS</span>
          <span className="text-[var(--neural-blue)]">{tasks.filter(t => t.status === "in_progress").length} ACTIVE</span>
          <span className="text-[var(--validation-emerald)]">{tasks.filter(t => t.status === "completed").length} DONE</span>
        </div>
      </div>
      
      {/* View Toggle */}
      <div className="flex mb-3 px-1">
        <button
          onClick={() => setExpandedView("tasks")}
          className={`flex-1 py-2 text-xs font-mono rounded-l transition-colors ${
            expandedView === "tasks"
              ? "bg-[var(--hardware-orange)]/20 text-[var(--hardware-orange)]"
              : "bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
          }`}
        >
          TASKS
        </button>
        <button
          onClick={() => setExpandedView("chat")}
          className={`flex-1 py-2 text-xs font-mono rounded-r transition-colors flex items-center justify-center gap-1.5 ${
            expandedView === "chat"
              ? "bg-[var(--artifact-purple)]/20 text-[var(--artifact-purple)]"
              : "bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
          }`}
        >
          <Bot size={12} />
          ORCHESTRATOR
        </button>
      </div>
      
      {/* Content Area */}
      <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
        {expandedView === "tasks" ? (
          /* Task List */
          <div className="flex-1 overflow-auto space-y-2 pr-1">
            {tasks.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full text-center py-8">
                <Target size={24} className="text-[var(--muted-foreground)] opacity-30 mb-2" />
                <p className="font-mono text-xs text-[var(--muted-foreground)]">NO TASKS</p>
                <p className="font-mono text-xs text-[var(--muted-foreground)] opacity-60">Add a task to get started</p>
              </div>
            ) : (
              <>
              {tasks.map(task => {
                const isExpanded = expandedTasks.has(task.id)
                const hasExpandableContent = task.description || task.aiAnalysis
                
                return (
                  <div
                    key={task.id}
                    className={`holo-glass-simple rounded p-2.5 transition-all group ${
                      task.status === "analyzing" ? "border-l-2 border-l-[var(--hardware-orange)] pulse-orange" : ""
                    } ${task.status === "completed" ? "opacity-60" : ""}`}
                  >
                    {/* Header Row - Always Visible */}
                    <div className="flex items-center gap-2">
                      {/* Expand/Collapse Button */}
                      {hasExpandableContent ? (
                        <button
                          onClick={() => toggleTaskExpanded(task.id)}
                          className="w-4 h-4 flex items-center justify-center text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] transition-colors shrink-0"
                        >
                          {isExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                        </button>
                      ) : (
                        <span className="w-4 shrink-0" />
                      )}
                      
                      {/* Status Icon */}
                      <div className="shrink-0">
                        {task.status === "completed" ? (
                          <CheckCircle2 size={12} style={{ color: getStatusColor(task.status) }} />
                        ) : task.status === "in_progress" ? (
                          <Play size={12} style={{ color: getStatusColor(task.status) }} className="animate-pulse" />
                        ) : task.status === "analyzing" ? (
                          <Sparkles size={12} style={{ color: getStatusColor(task.status) }} className="animate-spin" />
                        ) : (
                          <Circle size={12} style={{ color: getStatusColor(task.status) }} />
                        )}
                      </div>
                      
                      {/* Task Title */}
                      <span 
                        className={`font-mono text-xs flex-1 min-w-0 ${isExpanded ? "" : "truncate"} ${task.status === "completed" ? "line-through text-[var(--muted-foreground)]" : "text-[var(--foreground)]"}`}
                        title={task.title}
                      >
                        {task.title}
                      </span>
                      
                      {/* Priority Dot */}
                      <span 
                        className="w-1.5 h-1.5 rounded-full shrink-0"
                        style={{ backgroundColor: getPriorityColor(task.priority) }}
                        title={task.priority}
                      />
                      
                      {/* Actions */}
                      <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                        {task.status === "backlog" && (
                          <button
                            onClick={() => analyzeAndSuggest(task.id)}
                            className="p-1 rounded bg-[var(--artifact-purple)]/20 hover:bg-[var(--artifact-purple)]/40 text-[var(--artifact-purple)] transition-colors"
                            title="Analyze with AI"
                          >
                            <Sparkles size={10} />
                          </button>
                        )}
                        <button
                          onClick={() => handleDeleteTask(task.id)}
                          className="p-1 rounded bg-[var(--critical-red)]/20 hover:bg-[var(--critical-red)]/40 text-[var(--critical-red)] transition-colors"
                          title="Delete task"
                        >
                          <Trash2 size={10} />
                        </button>
                      </div>
                    </div>
                    
                    {/* Expanded Content */}
                    {isExpanded && (
                      <div className="mt-2 ml-6 space-y-2">
                        {/* Description */}
                        {task.description && (
                          <p className="font-mono text-xs text-[var(--muted-foreground)] leading-relaxed">
                            {task.description}
                          </p>
                        )}
                        
                        {/* Assignment */}
                        {task.assignedAgentId && (
                          <div className="flex items-center gap-1.5">
                            <ArrowRight size={10} className="text-[var(--artifact-purple)] shrink-0" />
                            <span className="font-mono text-[10px] text-[var(--muted-foreground)]">Assigned to:</span>
                            <span className="font-mono text-xs text-[var(--artifact-purple)]">
                              {getAgentName(task.assignedAgentId)}
                            </span>
                          </div>
                        )}
                        
                        {/* AI Analysis */}
                        {task.aiAnalysis && task.status !== "completed" && (
                          <div className="p-2 rounded bg-[var(--secondary)] border-l-2 border-l-[var(--artifact-purple)]">
                            <div className="flex items-center gap-1 mb-1">
                              <Bot size={10} className="text-[var(--artifact-purple)]" />
                              <span className="font-mono text-[10px] text-[var(--artifact-purple)] uppercase tracking-wider">AI Analysis</span>
                            </div>
                            <p className="font-mono text-xs text-[var(--muted-foreground)] leading-relaxed">
                              {task.aiAnalysis}
                            </p>
                          </div>
                        )}
                        
                        {/* Metadata */}
                        <div className="flex items-center gap-3 pt-1 border-t border-[var(--border)]/50">
                          <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                            Created: {new Date(task.createdAt).toLocaleDateString()}
                          </span>
                          {task.completedAt && (
                            <span className="font-mono text-[10px] text-[var(--validation-emerald)]">
                              Completed: {new Date(task.completedAt).toLocaleDateString()}
                            </span>
                          )}
                          <span 
                            className="font-mono text-[10px] px-1.5 py-0.5 rounded"
                            style={{ 
                              backgroundColor: `color-mix(in srgb, ${getStatusColor(task.status)} 20%, transparent)`,
                              color: getStatusColor(task.status)
                            }}
                          >
                            {task.status.replace("_", " ").toUpperCase()}
                          </span>
                        </div>
                      </div>
                    )}
                    
                    {/* Collapsed Assignment Hint */}
                    {!isExpanded && task.assignedAgentId && (
                      <div className="flex items-center gap-1 mt-1 ml-6">
                        <ArrowRight size={10} className="text-[var(--artifact-purple)] shrink-0" />
                        <span className="font-mono text-[10px] text-[var(--artifact-purple)] truncate">
                          {getAgentName(task.assignedAgentId)}
                        </span>
                      </div>
                    )}
                  </div>
                )
              })}
              </>
            )}
          </div>
        ) : (
          /* AI Orchestrator Chat */
          <div className="flex-1 flex flex-col min-h-0">
            {/* Chat Messages */}
            <div className="flex-1 overflow-auto space-y-3 pr-1 mb-3">
              {chatMessages.map(msg => (
                <div
                  key={msg.id}
                  className={`flex gap-2 ${msg.role === "user" ? "justify-end" : ""}`}
                >
                  {msg.role !== "user" && (
                    <div className={`w-6 h-6 rounded-full flex items-center justify-center shrink-0 ${
                      msg.role === "system" 
                        ? "bg-[var(--muted-foreground)]/20" 
                        : "bg-[var(--artifact-purple)]/20"
                    }`}>
                      <Bot size={12} className={msg.role === "system" ? "text-[var(--muted-foreground)]" : "text-[var(--artifact-purple)]"} />
                    </div>
                  )}
                  
                  <div className={`max-w-[85%] ${msg.role === "user" ? "order-first" : ""}`}>
                    <div className={`px-3 py-2 rounded-lg ${
                      msg.role === "user" 
                        ? "bg-[var(--neural-blue)]/20 text-[var(--foreground)]" 
                        : msg.role === "system"
                        ? "bg-[var(--secondary)] text-[var(--muted-foreground)]"
                        : "bg-[var(--artifact-purple)]/10 text-[var(--foreground)]"
                    }`}>
                      <p className="font-mono text-xs whitespace-pre-wrap leading-relaxed">
                        {msg.content}
                      </p>
                    </div>
                    <span className="font-mono text-xs text-[var(--muted-foreground)] ml-1">
                      {msg.timestamp}
                    </span>
                  </div>
                  
                  {msg.role === "user" && (
                    <div className="w-6 h-6 rounded-full bg-[var(--neural-blue)]/20 flex items-center justify-center shrink-0">
                      <User size={12} className="text-[var(--neural-blue)]" />
                    </div>
                  )}
                </div>
              ))}
              
              {isAnalyzing && (
                <div className="flex gap-2">
                  <div className="w-6 h-6 rounded-full bg-[var(--artifact-purple)]/20 flex items-center justify-center">
                    <Bot size={12} className="text-[var(--artifact-purple)] animate-pulse" />
                  </div>
                  <div className="px-3 py-2 rounded-lg bg-[var(--artifact-purple)]/10">
                    <div className="flex items-center gap-2">
                      <Sparkles size={12} className="text-[var(--artifact-purple)] animate-spin" />
                      <span className="font-mono text-xs text-[var(--artifact-purple)]">Analyzing...</span>
                    </div>
                  </div>
                </div>
              )}
              
              <div ref={chatEndRef} />
            </div>
            
            {/* Chat Input */}
            <div className="flex items-center gap-1.5 fui-input px-2 py-1.5">
              <span className="font-mono text-xs text-[var(--muted-foreground)] shrink-0">{">"}</span>
              <input
                ref={inputRef}
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSendMessage()}
                placeholder="Ask orchestrator..."
                className="flex-1 min-w-0 bg-transparent font-mono text-xs text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none"
                disabled={isAnalyzing}
              />
              <button
                onClick={handleSendMessage}
                disabled={!inputValue.trim() || isAnalyzing}
                className="p-1.5 rounded bg-[var(--artifact-purple)] hover:bg-[var(--artifact-purple)]/80 text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
              >
                <Send size={12} />
              </button>
            </div>
          </div>
        )}
      </div>
      
      {/* Add Task Modal — rendered via portal to escape backdrop-filter containment */}
      {showAddTask && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-sm holo-glass rounded-lg overflow-hidden animate-in fade-in zoom-in-95 duration-300">
            <div className="px-4 py-3 border-b border-[var(--border)] flex items-center justify-between">
              <h3 className="font-sans text-sm font-semibold tracking-fui text-[var(--hardware-orange)]">
                NEW TASK
              </h3>
              <button
                onClick={() => setShowAddTask(false)}
                className="p-1 rounded hover:bg-[var(--secondary)] transition-colors"
              >
                <X size={14} className="text-[var(--muted-foreground)]" />
              </button>
            </div>
            
            <div className="p-4 space-y-4">
              <div>
                <label className="block font-mono text-xs text-[var(--muted-foreground)] mb-2">
                  TASK DESCRIPTION
                </label>
                <input
                  type="text"
                  value={newTaskTitle}
                  onChange={(e) => setNewTaskTitle(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleAddTask()}
                  placeholder="What needs to be done?"
                  className="w-full px-3 py-2 rounded bg-[var(--secondary)] border border-[var(--border)] font-mono text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--hardware-orange)]"
                  autoFocus
                />
              </div>
              
              <div>
                <label className="block font-mono text-xs text-[var(--muted-foreground)] mb-2">
                  PRIORITY
                </label>
                <div className="flex gap-2">
                  {(["low", "medium", "high", "critical"] as TaskPriority[]).map(p => (
                    <button
                      key={p}
                      onClick={() => setNewTaskPriority(p)}
                      className={`flex-1 py-1.5 rounded text-xs font-mono transition-colors ${
                        newTaskPriority === p
                          ? "ring-2"
                          : "bg-[var(--secondary)] hover:bg-[var(--secondary-foreground)]/10"
                      }`}
                      style={{
                        backgroundColor: newTaskPriority === p ? `color-mix(in srgb, ${getPriorityColor(p)} 20%, transparent)` : undefined,
                        color: getPriorityColor(p),
                        ringColor: getPriorityColor(p)
                      }}
                    >
                      {p.toUpperCase()}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            
            <div className="px-4 py-3 border-t border-[var(--border)] flex justify-end gap-2">
              <button
                onClick={() => setShowAddTask(false)}
                className="px-3 py-1.5 rounded font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--secondary)] transition-colors"
              >
                CANCEL
              </button>
              <button
                onClick={handleAddTask}
                disabled={!newTaskTitle.trim()}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded font-mono text-xs bg-[var(--hardware-orange)] hover:bg-[var(--hardware-orange)]/80 text-white transition-colors disabled:opacity-50"
              >
                <Plus size={12} />
                ADD TASK
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}

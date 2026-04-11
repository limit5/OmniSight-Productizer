"use client"

import { useState, useCallback, useEffect, useRef } from "react"
import type { Agent, AgentStatus } from "@/components/omnisight/agent-matrix-wall"
import type { Task } from "@/components/omnisight/task-backlog"
import type { OrchestratorMessage } from "@/components/omnisight/orchestrator-ai"
import * as api from "@/lib/api"

// ─── Mappers (snake_case API → camelCase frontend) ───

function mapAgent(a: api.ApiAgent): Agent {
  return {
    id: a.id,
    name: a.name,
    type: a.type as Agent["type"],
    subType: a.sub_type || undefined,
    status: a.status as AgentStatus,
    progress: a.progress,
    thoughtChain: a.thought_chain,
    aiModel: a.ai_model || undefined,
    subTasks: a.sub_tasks?.map(s => ({ id: s.id, name: s.label, status: (s.status === "completed" ? "done" : s.status) as "pending" | "running" | "done" | "error" })),
  }
}

function mapTask(t: api.ApiTask): Task {
  return {
    id: t.id,
    title: t.title,
    description: t.description ?? undefined,
    priority: t.priority as Task["priority"],
    status: t.status as Task["status"],
    assignedAgentId: t.assigned_agent_id ?? undefined,
    createdAt: t.created_at,
    completedAt: t.completed_at ?? undefined,
    aiAnalysis: t.ai_analysis ?? undefined,
    suggestedAgentType: t.suggested_agent_type as Agent["type"] | undefined,
  }
}

function mapChatMessage(m: api.ApiChatMessage): OrchestratorMessage {
  return {
    id: m.id,
    role: m.role,
    content: m.content,
    timestamp: m.timestamp,
    suggestion: m.suggestion
      ? {
          id: m.suggestion.id,
          type: m.suggestion.type as OrchestratorMessage["suggestion"] extends infer S
            ? S extends { type: infer T } ? T : never : never,
          title: m.suggestion.title,
          description: m.suggestion.description,
          taskId: m.suggestion.task_id,
          agentId: m.suggestion.agent_id,
          agentType: m.suggestion.agent_type as Agent["type"] | undefined,
          priority: m.suggestion.priority as "high" | "medium" | "low",
          status: m.suggestion.status as "pending" | "accepted" | "rejected",
        }
      : undefined,
  }
}

// ─── Helpers ───

function _formatActionMessage(action: api.InvokeAction): string | null {
  switch (action.type) {
    case "assign":
      return `[ASSIGN] '${action.task_title}' → ${action.agent_name}`
    case "retry":
      return `[RETRY] ${action.agent_name} — restarting from checkpoint`
    case "command":
      return action.answer || action.error || null
    case "report":
      return action.summary || null
    case "health":
      return `[HEALTH] ${action.agent_count} agents | ${action.running} running | ${action.idle} idle | ${action.pending} pending tasks`
    default:
      return null
  }
}

// ─── Hook ───

export function useEngine() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [messages, setMessages] = useState<OrchestratorMessage[]>([])
  const [connected, setConnected] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)
  const [systemStatus, setSystemStatus] = useState<api.SystemStatus | null>(null)
  const [systemInfo, setSystemInfo] = useState<api.SystemInfo | null>(null)
  const [devices, setDevices] = useState<api.SystemDevice[]>([])
  const [spec, setSpec] = useState<api.SpecValue[]>([])
  const [repos, setRepos] = useState<api.RepoInfo[]>([])
  const [logs, setLogs] = useState<api.LogEntry[]>([])
  const [tokenUsage, setTokenUsage] = useState<api.TokenUsage[]>([])
  const initRef = useRef(false)

  // Fetch initial data and subscribe to SSE event stream
  useEffect(() => {
    if (initRef.current) return
    initRef.current = true

    let eventSource: EventSource | null = null

    let sysInterval: ReturnType<typeof setInterval> | null = null

    async function fetchSystemData() {
      try {
        const [statusRes, infoRes, devicesRes, specRes, reposRes, logsRes, tokensRes] = await Promise.all([
          api.getSystemStatus(),
          api.getSystemInfo(),
          api.getDevices(),
          api.getSpec(),
          api.getRepos(),
          api.getLogs(),
          api.getTokenUsage(),
        ])
        setSystemStatus(statusRes)
        setSystemInfo(infoRes)
        setDevices(devicesRes)
        setSpec(specRes)
        setRepos(reposRes)
        setLogs(logsRes)
        setTokenUsage(tokensRes)
        console.log("[Engine] System data loaded:", infoRes.hostname, infoRes.cpu_model)
      } catch (e) {
        console.warn("[Engine] System data fetch failed:", e)
      }
    }

    async function init() {
      // Always fetch system data regardless of agent/task status
      fetchSystemData()
      sysInterval = setInterval(fetchSystemData, 5000)

      try {
        const [agentsRes, tasksRes] = await Promise.all([
          api.listAgents(),
          api.listTasks(),
        ])
        setAgents(agentsRes.map(mapAgent))
        setTasks(tasksRes.map(mapTask))
        setConnected(true)

        // Subscribe to persistent SSE for real-time state changes
        eventSource = api.subscribeEvents((event) => {
          // ── Push ALL events into REPORTER VORTEX logs ──
          if (event.event !== "heartbeat") {
            const d = event.data as Record<string, unknown>
            const ts = (d.timestamp as string) || new Date().toISOString()
            const time = ts.includes("T") ? ts.split("T")[1]?.slice(0, 8) || ts : ts
            let logMsg = ""
            let logLevel: "info" | "warn" | "error" = "info"

            if (event.event === "agent_update") {
              const status = d.status as string
              logMsg = `[AGENT] ${d.agent_id} → ${status?.toUpperCase()}`
              if (d.thought_chain) logMsg += `: ${(d.thought_chain as string).slice(0, 80)}`
              logLevel = status === "error" ? "error" : status === "warning" ? "warn" : "info"
            } else if (event.event === "task_update") {
              logMsg = `[TASK] ${d.task_id} → ${(d.status as string)?.toUpperCase()}`
              if (d.assigned_agent_id) logMsg += ` (agent: ${d.assigned_agent_id})`
            } else if (event.event === "tool_progress") {
              const phase = d.phase as string
              const tool = d.tool_name as string
              if (phase === "start") logMsg = `[TOOL] ⟳ ${tool} executing...`
              else if (phase === "done") logMsg = `[TOOL] ✓ ${tool}: ${((d.output as string) || "").slice(0, 60)}`
              else logMsg = `[TOOL] ✗ ${tool}: ${((d.output as string) || "").slice(0, 60)}`
              logLevel = phase === "error" ? "error" : "info"
            } else if (event.event === "pipeline") {
              logMsg = `[PIPELINE] ${d.phase}: ${d.detail}`
              logLevel = (d.phase as string)?.includes("error") ? "error" : "info"
            } else if (event.event === "workspace") {
              logMsg = `[WORKSPACE] ${d.agent_id} ${d.action}: ${d.detail}`
            } else if (event.event === "container") {
              logMsg = `[DOCKER] ${d.agent_id} ${d.action}: ${d.detail}`
            } else if (event.event === "invoke") {
              logMsg = `[INVOKE] ${d.action_type}: ${d.detail}`
            }

            if (logMsg) {
              setLogs(prev => [...prev.slice(-199), { timestamp: time, message: logMsg, level: logLevel }])
            }
          }

          // ── State updates ──
          if (event.event === "agent_update") {
            const d = event.data
            setAgents(prev => prev.map(a =>
              a.id === d.agent_id
                ? { ...a, status: d.status as AgentStatus, thoughtChain: d.thought_chain || a.thoughtChain }
                : a
            ))
          } else if (event.event === "task_update") {
            const d = event.data
            setTasks(prev => prev.map(t =>
              t.id === d.task_id
                ? { ...t, status: d.status as Task["status"], assignedAgentId: d.assigned_agent_id ?? t.assignedAgentId }
                : t
            ))
          } else if (event.event === "tool_progress") {
            const d = event.data
            const label = d.phase === "start" ? `⟳ ${d.tool_name}...` : d.phase === "done" ? `✓ ${d.tool_name}` : `✗ ${d.tool_name}`
            setMessages(prev => {
              const toolMsgId = `tool-${d.tool_name}-${d.index ?? 0}`
              const existing = prev.find(m => m.id === toolMsgId)
              const msg: OrchestratorMessage = {
                id: toolMsgId,
                role: "system",
                content: `[TOOL] ${label}: ${d.output.slice(0, 300)}`,
                timestamp: d.timestamp,
              }
              return existing
                ? prev.map(m => m.id === toolMsgId ? msg : m)
                : [...prev, msg]
            })
          } else if (event.event === "pipeline") {
            const d = event.data
            setMessages(prev => [...prev, {
              id: `pipeline-${Date.now()}`,
              role: "system" as const,
              content: `[PIPELINE] ${d.phase}: ${d.detail}`,
              timestamp: d.timestamp,
            }])
          }
        }, () => {
          // On SSE error — don't crash, just log
          console.warn("[Engine] SSE connection lost, will auto-reconnect")
        })
      } catch {
        console.warn("[Engine] Backend unavailable, using offline mode")
        setConnected(false)
      }
    }
    init()

    return () => {
      eventSource?.close()
      if (sysInterval) clearInterval(sysInterval)
    }
  }, [])

  // ── Agent operations ──

  const addAgent = useCallback(async (type: Agent["type"], name?: string, subType?: string, aiModel?: string) => {
    const agentName = name || `${type.toUpperCase()}_AGENT_${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`
    if (connected) {
      try {
        const res = await api.createAgent({ name: agentName, type, sub_type: subType, ai_model: aiModel })
        setAgents(prev => [...prev, mapAgent(res)])
        return mapAgent(res)
      } catch (e) {
        console.error("[Engine] Failed to create agent:", e)
      }
    }
    // Offline fallback
    const fallback: Agent = {
      id: `agent-${Date.now()}`,
      name: agentName,
      type,
      subType,
      status: "booting",
      progress: { current: 0, total: 5 },
      thoughtChain: "Initializing...",
      aiModel: aiModel || undefined,
    }
    setAgents(prev => [...prev, fallback])
    return fallback
  }, [connected])

  const removeAgent = useCallback(async (id: string) => {
    setAgents(prev => prev.filter(a => a.id !== id))
    if (connected) {
      try { await api.deleteAgent(id) } catch { /* swallow */ }
    }
  }, [connected])

  const patchAgentLocal = useCallback((id: string, patch: Partial<Agent>) => {
    setAgents(prev => prev.map(a => a.id === id ? { ...a, ...patch } : a))
  }, [])

  const updateAgentStatusFn = useCallback(async (id: string, status: AgentStatus, thoughtChain?: string) => {
    patchAgentLocal(id, { status, ...(thoughtChain ? { thoughtChain } : {}) })
    if (connected) {
      try { await api.updateAgentStatus(id, status) } catch { /* swallow */ }
    }
  }, [connected, patchAgentLocal])

  // ── Task operations ──

  const assignTask = useCallback(async (taskId: string, agentId: string) => {
    setTasks(prev => prev.map(t =>
      t.id === taskId ? { ...t, status: "assigned" as Task["status"], assignedAgentId: agentId } : t
    ))
    patchAgentLocal(agentId, { status: "running", thoughtChain: `Task assigned: Processing ${taskId}...` })
    if (connected) {
      try { await api.updateTask(taskId, { status: "assigned", assigned_agent_id: agentId }) } catch { /* swallow */ }
    }
  }, [connected, patchAgentLocal])

  const completeTask = useCallback(async (taskId: string) => {
    setTasks(prev => prev.map(t =>
      t.id === taskId ? { ...t, status: "completed" as Task["status"], completedAt: new Date().toISOString() } : t
    ))
    if (connected) {
      try { await api.updateTask(taskId, { status: "completed" }) } catch { /* swallow */ }
    }
  }, [connected])

  const forceAssign = useCallback(async (taskId: string, agentId: string) => {
    patchAgentLocal(agentId, { status: "running", thoughtChain: `FORCE ASSIGNED: Task ${taskId}` })
    setTasks(prev => prev.map(t =>
      t.id === taskId ? { ...t, status: "assigned" as Task["status"], assignedAgentId: agentId } : t
    ))
    if (connected) {
      try { await api.updateTask(taskId, { status: "assigned", assigned_agent_id: agentId }) } catch { /* swallow */ }
    }
  }, [connected, patchAgentLocal])

  // ── Chat / Command ──

  const sendCommand = useCallback(async (command: string) => {
    // Add user message immediately
    const userMsg: OrchestratorMessage = {
      id: `msg-${Date.now()}`,
      role: "user",
      content: command,
      timestamp: new Date().toISOString(),
    }
    setMessages(prev => [...prev, userMsg])

    if (connected) {
      try {
        setIsStreaming(true)
        // Use streaming endpoint
        let accumulated = ""
        for await (const chunk of api.streamChat(command)) {
          if (chunk.event === "token") {
            const { token } = chunk.data as { token: string }
            accumulated += (accumulated ? " " : "") + token
            // Update a temporary streaming message
            setMessages(prev => {
              const existing = prev.find(m => m.id === "streaming")
              const streamMsg: OrchestratorMessage = {
                id: "streaming",
                role: "orchestrator",
                content: accumulated,
                timestamp: new Date().toISOString(),
              }
              return existing
                ? prev.map(m => m.id === "streaming" ? streamMsg : m)
                : [...prev, streamMsg]
            })
          } else if (chunk.event === "done") {
            const data = chunk.data as api.ApiChatMessage
            const finalMsg = mapChatMessage(data)
            setMessages(prev => prev.filter(m => m.id !== "streaming").concat(finalMsg))
          }
        }
        setIsStreaming(false)
        return
      } catch (e) {
        console.error("[Engine] Stream failed, falling back to sync:", e)
        setIsStreaming(false)
      }

      // Fallback to sync chat
      try {
        const res = await api.sendChat(command)
        setMessages(prev => [...prev, mapChatMessage(res.message)])
        return
      } catch { /* fall through to offline */ }
    }

    // Offline fallback
    const offlineMsg: OrchestratorMessage = {
      id: `msg-${Date.now()}-offline`,
      role: "orchestrator",
      content: `[OFFLINE] Command received: "${command}". Backend not connected.`,
      timestamp: new Date().toISOString(),
    }
    setMessages(prev => [...prev, offlineMsg])
  }, [connected])

  // ── Invoke (Singularity Sync) ──

  const invoke = useCallback(async (command?: string) => {
    if (!connected) {
      // Offline: just add a system message
      setMessages(prev => [...prev, {
        id: `msg-${Date.now()}-invoke`,
        role: "system" as const,
        content: "[INVOKE] Backend not connected — cannot perform singularity sync.",
        timestamp: new Date().toISOString(),
      }])
      return
    }

    // Add system message showing invoke started
    setMessages(prev => [...prev, {
      id: `msg-${Date.now()}-invoke-start`,
      role: "system" as const,
      content: command
        ? `[INVOKE] Singularity sync initiated with command: "${command}"`
        : "[INVOKE] Singularity sync initiated — analysing system state...",
      timestamp: new Date().toISOString(),
    }])

    try {
      setIsStreaming(true)
      for await (const event of api.streamInvoke(command)) {
        if (event.event === "analysis") {
          const a = event.data as api.InvokeAnalysis
          setMessages(prev => [...prev, {
            id: `msg-${Date.now()}-analysis`,
            role: "orchestrator" as const,
            content: `[ANALYSIS] ${a.agents_total} agents (${a.agents_running} running, ${a.agents_idle} idle, ${a.agents_error} error) | ${a.tasks_unassigned} pending tasks | Planning ${a.planned_actions} action(s): ${a.action_types.join(", ")}`,
            timestamp: new Date().toISOString(),
          }])
        } else if (event.event === "phase") {
          const p = event.data as { phase: string; message: string }
          setMessages(prev => [...prev, {
            id: `msg-${Date.now()}-phase-${p.phase}`,
            role: "system" as const,
            content: `[${p.phase.toUpperCase()}] ${p.message}`,
            timestamp: new Date().toISOString(),
          }])
        } else if (event.event === "action") {
          const action = event.data as api.InvokeAction
          // Apply state changes locally based on action type
          if (action.type === "assign" && action.agent_id && action.task_id) {
            patchAgentLocal(action.agent_id, {
              status: "running",
              thoughtChain: `Task assigned: ${action.task_title}. Processing...`,
            })
            setTasks(prev => prev.map(t =>
              t.id === action.task_id
                ? { ...t, status: "assigned" as const, assignedAgentId: action.agent_id }
                : t
            ))
          } else if (action.type === "retry" && action.agent_id) {
            patchAgentLocal(action.agent_id, {
              status: "running",
              thoughtChain: "Auto-retry initiated by INVOKE sync.",
            })
          }

          // Add action result as message
          const content = _formatActionMessage(action)
          if (content) {
            setMessages(prev => [...prev, {
              id: `msg-${Date.now()}-action-${action.type}`,
              role: "orchestrator" as const,
              content,
              timestamp: new Date().toISOString(),
            }])
          }
        } else if (event.event === "done") {
          const d = event.data as { action_count: number; results: string[] }
          setMessages(prev => [...prev, {
            id: `msg-${Date.now()}-done`,
            role: "orchestrator" as const,
            content: `[INVOKE COMPLETE] ${d.action_count} action(s) executed:\n${d.results.map(r => `  • ${r}`).join("\n")}`,
            timestamp: new Date().toISOString(),
          }])
        }
      }
    } catch (e) {
      console.error("[Engine] Invoke stream failed:", e)
      setMessages(prev => [...prev, {
        id: `msg-${Date.now()}-invoke-err`,
        role: "system" as const,
        content: `[INVOKE ERROR] ${e}`,
        timestamp: new Date().toISOString(),
      }])
    } finally {
      setIsStreaming(false)
    }
  }, [connected, patchAgentLocal])

  // ── Refresh from backend ──

  const refresh = useCallback(async () => {
    if (!connected) return
    try {
      const [agentsRes, tasksRes] = await Promise.all([
        api.listAgents(),
        api.listTasks(),
      ])
      setAgents(agentsRes.map(mapAgent))
      setTasks(tasksRes.map(mapTask))
    } catch { /* swallow */ }
  }, [connected])

  return {
    // State
    agents,
    tasks,
    messages,
    connected,
    isStreaming,
    systemStatus,
    systemInfo,
    devices,
    spec,
    repos,
    logs,
    tokenUsage,
    // Setters (for local-only operations like emergency stop)
    setAgents,
    setTasks,
    // Agent ops
    addAgent,
    removeAgent,
    patchAgentLocal,
    updateAgentStatus: updateAgentStatusFn,
    // Task ops
    assignTask,
    completeTask,
    forceAssign,
    // Chat
    sendCommand,
    // Invoke
    invoke,
    // Utils
    refresh,
  }
}

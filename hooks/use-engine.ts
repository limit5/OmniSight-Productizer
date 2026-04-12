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
    externalIssueId: t.external_issue_id ?? undefined,
    issueUrl: t.issue_url ?? undefined,
    acceptanceCriteria: t.acceptance_criteria ?? undefined,
    labels: t.labels ?? [],
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
  const [tokenBudget, setTokenBudget] = useState<api.TokenBudgetInfo | null>(null)
  const [notifications, setNotifications] = useState<api.NotificationItem[]>([])
  const [unreadCount, setUnreadCount] = useState(0)
  const [compressionStats, setCompressionStats] = useState<api.CompressionStats | null>(null)
  const [artifacts, setArtifacts] = useState<api.ArtifactItem[]>([])
  const [simulations, setSimulations] = useState<api.SimulationItem[]>([])
  const [npiData, setNpiData] = useState<api.NPIData | null>(null)
  const initRef = useRef(false)

  // Fetch initial data and subscribe to SSE event stream
  useEffect(() => {
    if (initRef.current) return
    initRef.current = true

    let eventSource: EventSource | null = null
    let lastEventTimestamp: string = ""

    let sysInterval: ReturnType<typeof setInterval> | null = null

    async function fetchSystemData() {
      try {
        const [rStatus, rInfo, rDevices, rSpec, rRepos, rLogs, rTokens, rBudget, rUnread, rCompress, rSims] = await Promise.allSettled([
          api.getSystemStatus(),
          api.getSystemInfo(),
          api.getDevices(),
          api.getSpec(),
          api.getRepos(),
          api.getLogs(),
          api.getTokenUsage(),
          api.getTokenBudget(),
          api.getUnreadCount(),
          api.getCompressionStats(),
          api.listSimulations(),
        ])
        // Only update state for successful fetches — stale data is better than no data
        if (rStatus.status === "fulfilled") setSystemStatus(rStatus.value)
        if (rInfo.status === "fulfilled") setSystemInfo(rInfo.value)
        if (rDevices.status === "fulfilled") setDevices(rDevices.value)
        if (rSpec.status === "fulfilled") setSpec(rSpec.value)
        if (rRepos.status === "fulfilled") setRepos(rRepos.value)
        if (rLogs.status === "fulfilled") setLogs(rLogs.value)
        if (rTokens.status === "fulfilled") setTokenUsage(rTokens.value)
        if (rBudget.status === "fulfilled") setTokenBudget(rBudget.value)
        if (rUnread.status === "fulfilled") setUnreadCount(rUnread.value.count)
        if (rCompress.status === "fulfilled") setCompressionStats(rCompress.value)
        if (rSims.status === "fulfilled") setSimulations(rSims.value)
        const all = [rStatus, rInfo, rDevices, rSpec, rRepos, rLogs, rTokens, rBudget, rUnread, rCompress, rSims]
        const failCount = all.filter(r => r.status === "rejected").length
        if (failCount > 0) console.warn(`[Engine] ${failCount}/${all.length} system data fetches failed`)
      } catch (e) {
        console.warn("[Engine] System data fetch failed:", e)
      }
    }

    // Fetch NPI data once (not on interval — rarely changes)
    async function fetchNPI() {
      try {
        const npi = await api.getNPIState()
        setNpiData(npi)
      } catch { /* NPI not configured */ }
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

        // Subscribe to persistent SSE for real-time state changes (with auto-reconnect)
        let reconnectAttempts = 0
        function connectSSE() {
          eventSource = api.subscribeEvents((event) => {
            reconnectAttempts = 0  // Reset on successful event
          const evtTimestamp = (event.data as Record<string, unknown>)?.timestamp as string
          if (evtTimestamp) lastEventTimestamp = evtTimestamp
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
            } else if (event.event === "artifact_created") {
              logMsg = `[ARTIFACT] ${d.name} (${d.type}, ${d.size} bytes)`
              setArtifacts(prev => [{
                id: d.id as string, task_id: (d.task_id as string) || null,
                agent_id: (d.agent_id as string) || null, name: d.name as string,
                type: (d.type as string) as api.ArtifactItem["type"],
                file_path: "", size: d.size as number,
                created_at: ts,
              }, ...prev.slice(0, 49)])
            } else if (event.event === "notification") {
              const level = d.level as string
              logMsg = `[NOTIFY:${level.toUpperCase()}] ${d.title}`
              if (d.message) logMsg += `: ${(d.message as string).slice(0, 60)}`
              logLevel = level === "critical" || level === "action" ? "error" : level === "warning" ? "warn" : "info"
              // Add to notifications list and increment unread
              setNotifications(prev => [{
                id: d.id as string, level: level as api.NotificationItem["level"],
                title: d.title as string, message: (d.message as string) || "",
                source: (d.source as string) || "", timestamp: ts,
                read: false, action_url: d.action_url as string | undefined,
                action_label: d.action_label as string | undefined,
              }, ...prev.slice(0, 49)])
              setUnreadCount(prev => prev + 1)
            } else if (event.event === "token_warning") {
              const level = d.level as string
              logMsg = `[TOKEN] ${level.toUpperCase()}: ${d.message}`
              logLevel = level === "frozen" ? "error" : level === "downgrade" ? "warn" : "warn"
              // Update budget state from event data
              setTokenBudget(prev => prev ? {
                ...prev,
                usage: (d.usage as number) || prev.usage,
                ratio: prev.budget > 0 ? ((d.usage as number) || prev.usage) / prev.budget : 0,
                level,
                frozen: level === "frozen",
              } : prev)
            } else if (event.event === "simulation") {
              const action = d.action as string
              const simId = d.sim_id as string
              logMsg = `[SIM] ${(d.track as string || "").toUpperCase()}/${d.module}: ${d.detail}`
              logLevel = (d.status as string) === "fail" || (d.status as string) === "error" ? "error" : "info"
              if (action === "start") {
                setSimulations(prev => [{
                  id: simId,
                  task_id: null,
                  agent_id: null,
                  track: (d.track as "algo" | "hw") || "algo",
                  module: (d.module as string) || "",
                  status: "running",
                  tests_total: (d.tests_total as number) || 0,
                  tests_passed: 0,
                  tests_failed: 0,
                  coverage_pct: 0,
                  valgrind_errors: 0,
                  duration_ms: 0,
                  created_at: (d.timestamp as string) || new Date().toISOString(),
                }, ...prev.slice(0, 49)])
              } else if (action === "result") {
                setSimulations(prev => {
                  const exists = prev.some(s => s.id === simId)
                  if (exists) {
                    return prev.map(s =>
                      s.id === simId
                        ? {
                            ...s,
                            status: (d.status as "pass" | "fail" | "error") || s.status,
                            tests_passed: (d.tests_passed as number) ?? s.tests_passed,
                            tests_total: (d.tests_total as number) ?? s.tests_total,
                            tests_failed: (d.tests_failed as number) ?? ((d.tests_total as number ?? 0) - (d.tests_passed as number ?? 0)),
                          }
                        : s
                    )
                  }
                  // Result arrived before start — create entry
                  return [{
                    id: simId,
                    task_id: null, agent_id: null,
                    track: (d.track as "algo" | "hw") || "algo",
                    module: (d.module as string) || "",
                    status: (d.status as "pass" | "fail" | "error") || "error",
                    tests_total: (d.tests_total as number) || 0,
                    tests_passed: (d.tests_passed as number) || 0,
                    tests_failed: (d.tests_failed as number) || 0,
                    coverage_pct: 0, valgrind_errors: 0, duration_ms: 0,
                    created_at: (d.timestamp as string) || new Date().toISOString(),
                  }, ...prev.slice(0, 49)]
                })
              }
            } else if (event.event === "debug_finding") {
              const severity = (d.severity as string) || "info"
              logMsg = `[DEBUG] ${(d.finding_type as string || "").toUpperCase()}: ${d.message}`
              logLevel = severity === "error" || severity === "critical" ? "error" : severity === "warn" ? "warn" : "info"
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
          }, (err) => {
            // On SSE error — reconnect with backoff + replay missed events
            if (eventSource?.readyState === EventSource.CLOSED && reconnectAttempts < 5) {
              reconnectAttempts++
              const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000)
              console.warn(`[Engine] SSE closed, reconnecting in ${delay}ms (attempt ${reconnectAttempts})`)
              setTimeout(async () => {
                connectSSE()
                // Replay missed events from gap period
                if (lastEventTimestamp) {
                  try {
                    const missed = await api.replayEvents(lastEventTimestamp, 200)
                    if (missed.length > 0) {
                      console.log(`[Engine] Replayed ${missed.length} missed events`)
                    }
                  } catch {
                    // Replay is best-effort
                  }
                }
              }, delay)
            }
          })
        }
        connectSSE()
      } catch {
        console.warn("[Engine] Backend unavailable, using offline mode")
        setConnected(false)
      }
    }
    init()
    fetchNPI()

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
    tokenBudget,
    compressionStats,
    artifacts,
    simulations,
    setSimulations,
    npiData,
    setNpiData,
    notifications,
    unreadCount,
    setUnreadCount,
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

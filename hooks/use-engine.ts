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
  const providerSwitchCallbackRef = useRef<(() => void) | null>(null)
  // Track resources for cleanup (supports StrictMode double-mount)
  const eventSourceRef = useRef<{ close: () => void; readyState: number } | null>(null)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Fetch initial data and subscribe to SSE event stream
  useEffect(() => {
    // Phase 48-Fix P0: subscribeEvents now returns a handle (close + readyState)
    // instead of a raw EventSource so all callers share one underlying stream.
    let eventSource: { close: () => void; readyState: number } | null = null
    let lastEventTimestamp: string = ""
    let cancelled = false  // cleanup flag to prevent setState after unmount

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
        if (cancelled) return
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
              // Q.3-SUB-2 (#297): ``action`` discriminates
              // created / updated / deleted so other devices can patch
              // their list without polling. Pre-Q.3-SUB-2 only updates
              // emitted, so a missing ``action`` means "updated".
              const action = d.action as string | undefined
              if (action === "deleted") {
                logMsg = `[TASK] ${d.task_id} → DELETED`
              } else if (action === "created") {
                logMsg = `[TASK] ${d.task_id} → CREATED (${(d.status as string)?.toUpperCase()})`
              } else {
                logMsg = `[TASK] ${d.task_id} → ${(d.status as string)?.toUpperCase()}`
              }
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
              // Trigger provider data refetch when provider is switched from any source
              if (d.action_type === "provider_switch" && providerSwitchCallbackRef.current) {
                providerSwitchCallbackRef.current()
              }
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
            } else if (event.event === "notification.read") {
              // Q.3-SUB-3 (#297): device A marked a notification read →
              // device B decrements its bell badge and flips the row's
              // ``read`` flag in its local list. REST is still the
              // source of truth; the next /notifications/unread-count
              // poll catches stragglers if the SSE push is missed.
              // ``broadcast_scope='user'`` is advisory until Q.4 (#298)
              // enforces per-user fan-out — until then we decrement
              // unconditionally but only patch list rows that are
              // actually present + currently unread, so a replayed
              // event can't double-flip state.
              logMsg = `[NOTIFY] ${d.id} → READ`
              logLevel = "info"
              let patched = false
              setNotifications(prev => {
                const hit = prev.some(n => n.id === d.id && !n.read)
                if (!hit) return prev
                patched = true
                return prev.map(n => n.id === d.id ? { ...n, read: true } : n)
              })
              // Always decrement — the unread counter is the
              // tenant-scoped COUNT from DB, not derived from the
              // bounded local list (only last 50 entries are cached).
              // Clamp at 0 so a replay / cross-user misfire cannot
              // drive the badge negative.
              void patched
              setUnreadCount(prev => Math.max(0, prev - 1))
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
                  track: (d.track as "algo" | "hw" | "npu") || "algo",
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
                    track: (d.track as "algo" | "hw" | "npu") || "algo",
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
            } else if (event.event === "agent.entropy") {
              // R2 (#308): Semantic Entropy Monitor measurement. Log line
              // is informational at "ok", warn at "warning", error at
              // "deadlock" so REPORTER VORTEX colouring matches the
              // card's own verdict badge.
              const verdict = (d.verdict as string) || "ok"
              const score = Number(d.entropy_score) || 0
              logMsg = `[ENTROPY] ${d.agent_id} score=${score.toFixed(2)} → ${verdict.toUpperCase()}`
              logLevel = verdict === "deadlock" ? "error" : verdict === "warning" ? "warn" : "info"
            } else if (event.event === "agent.scratchpad.saved") {
              // R3 (#309): scratchpad flush. Always info-level — a save
              // is a healthy checkpoint, not a warning.
              logMsg = `[SCRATCHPAD] ${d.agent_id} turn=${d.turn} trigger=${d.trigger} size=${d.size_bytes}B`
              logLevel = "info"
            } else if (event.event === "agent.token_continuation") {
              // R3 (#309): auto-continuation round. Warn level because it
              // hints the provider's max_tokens ceiling is too tight.
              logMsg = `[CONTINUE] ${d.agent_id} round=${d.continuation_round}/${d.total_rounds} +${d.appended_chars}c`
              logLevel = "warn"
            } else if (event.event === "security.new_device_login") {
              // Q.2 (#296): mirror the security event into REPORTER VORTEX
              // for the audit trail. The accompanying notify() emits a
              // separate "notification" event that updates the bell badge,
              // and SecurityAlertsCenter renders the actionable toast —
              // we do not duplicate either of those here.
              logMsg = `[SECURITY] new device login user=${d.user_id} ip=${d.ip}`
              logLevel = "warn"
            } else if (event.event === "preferences.updated") {
              // Q.3-SUB-4 (#297): mirror user-preference changes into
              // REPORTER VORTEX. Storage side-effects (localStorage
              // write + in-tab `onStorageChange` notify so the I18n
              // context and other consumers rehydrate) are handled
              // by `storage-bridge.tsx` — we do not duplicate the
              // patch here. ``broadcast_scope='user'`` is advisory
              // until Q.4 (#298); the bridge self-filters on
              // ``user_id``.
              const prefKey = (d.pref_key as string) || ""
              const value = (d.value as string) || ""
              logMsg = `[PREFS] ${prefKey}=${value.slice(0, 40)}`
              logLevel = "info"
            } else if (event.event === "workflow_updated") {
              // Q.3-SUB-1 (#297): mirror workflow_run state changes into
              // REPORTER VORTEX. List-state patching is handled by the
              // dedicated `useWorkflows()` hook (hooks/use-workflows.ts)
              // which subscribes to the same SSE stream — we do not
              // duplicate the patch here.
              const st = (d.status as string) || ""
              logMsg = `[WORKFLOW] ${d.run_id} → ${st.toUpperCase()} (v${d.version})`
              logLevel = st === "failed" ? "error"
                : st === "halted" ? "warn"
                : "info"
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
          } else if (event.event === "agent.entropy") {
            // R2 (#308): update the agent's Cognitive Health block in
            // place. We roll the sparkline locally (append-and-trim to
            // 20) so the UI stays live even if the /entropy endpoint
            // isn't polled. `recentOutputs` and `lastUpdated` are kept
            // if present; the backend includes recent outputs via the
            // REST snapshot, not on every SSE event.
            const d = event.data as Record<string, unknown>
            const score = Number(d.entropy_score) || 0
            const verdict = ((d.verdict as string) || "ok") as "ok" | "warning" | "deadlock"
            const warn = Number(d.threshold_warn ?? 0.5)
            const dead = Number(d.threshold_deadlock ?? 0.7)
            setAgents(prev => prev.map(a => {
              if (a.id !== d.agent_id) return a
              const prevSpark = a.cognitive?.sparkline ?? []
              const sparkline = [...prevSpark, score].slice(-20)
              return {
                ...a,
                cognitive: {
                  entropyScore: score,
                  verdict,
                  thresholdWarn: warn,
                  thresholdDeadlock: dead,
                  sparkline,
                  loopCount: a.cognitive?.loopCount ?? (Number(d.round) || prevSpark.length + 1),
                  loopMax: a.cognitive?.loopMax ?? 10,
                  recentOutputs: a.cognitive?.recentOutputs,
                  lastUpdated: (d.timestamp as string) || new Date().toISOString(),
                },
              }
            }))
          } else if (event.event === "agent.scratchpad.saved") {
            // R3 (#309): refresh the agent's Scratchpad Progress
            // Indicator in place. The backend payload carries everything
            // we need for the card; the optional preview is fetched
            // lazily via /scratchpad/agents/<id>/preview on demand.
            const d = event.data as Record<string, unknown>
            const agentId = d.agent_id as string
            const turn = Number(d.turn) || 0
            const sizeBytes = Number(d.size_bytes) || 0
            const sectionsCount = Number(d.sections_count) || 0
            const trigger = (d.trigger as string) || "manual"
            const updatedAtIso = (d.timestamp as string) || new Date().toISOString()
            setAgents(prev => prev.map(a => {
              if (a.id !== agentId) return a
              const prevSummary = a.scratchpad
              // Keep the highest totalTurns we've seen so the progress
              // bar denominator doesn't regress when a tool-done save
              // lands mid-cycle.
              const totalTurns = Math.max(
                Number((d as Record<string, unknown>).total_turns) || 0,
                prevSummary?.totalTurns || 0,
                turn,
              )
              return {
                ...a,
                scratchpad: {
                  turn,
                  totalTurns,
                  sectionsCount,
                  sizeBytes,
                  trigger,
                  subtask: (d.subtask as string | null) ?? prevSummary?.subtask ?? null,
                  ageSeconds: 0,
                  updatedAtIso,
                  recoverable: true,
                },
              }
            }))
          } else if (event.event === "agent.token_continuation") {
            // R3 (#309): attach the "↩ auto-continued" tag to the most
            // recent assistant message in this agent's stream. If no
            // matching message exists we synthesize a placeholder so the
            // indicator still renders in the expanded card.
            const d = event.data as Record<string, unknown>
            const agentId = d.agent_id as string
            const rounds = Number(d.total_rounds) || Number(d.continuation_round) || 1
            setAgents(prev => prev.map(a => {
              if (a.id !== agentId) return a
              const existing = a.messages || []
              const lastIdx = existing.length - 1
              if (lastIdx >= 0) {
                const target = existing[lastIdx]
                const patched = { ...target, autoContinued: true, continuationRounds: rounds }
                const next = [...existing]
                next[lastIdx] = patched
                return { ...a, messages: next }
              }
              return {
                ...a,
                messages: [{
                  id: `cont-${agentId}-${Date.now()}`,
                  type: "info" as const,
                  message: "(output stitched from multiple provider calls)",
                  timestamp: (d.timestamp as string) || new Date().toISOString(),
                  autoContinued: true,
                  continuationRounds: rounds,
                }],
              }
            }))
          } else if (event.event === "task_update") {
            // Q.3-SUB-2 (#297): created → append, updated → patch,
            // deleted → remove. The SSE payload for "created" carries
            // only task_id / status / assigned_agent_id, not the full
            // task row, so we refetch the list rather than synthesise
            // a partial entry that would miss title / description /
            // labels / acceptance_criteria. listTasks() failure is
            // non-fatal — the next periodic refresh catches up.
            const d = event.data
            const action = (d as Record<string, unknown>).action as string | undefined
            if (action === "deleted") {
              setTasks(prev => prev.filter(t => t.id !== d.task_id))
            } else if (action === "created") {
              api.listTasks()
                .then(list => {
                  if (cancelled) return
                  setTasks(list.map(mapTask))
                })
                .catch(() => { /* best-effort — next poll catches up */ })
            } else {
              setTasks(prev => prev.map(t =>
                t.id === d.task_id
                  ? { ...t, status: d.status as Task["status"], assignedAgentId: d.assigned_agent_id ?? t.assignedAgentId }
                  : t
              ))
            }
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
            // On SSE error — reconnect with backoff + replay missed events
            if (cancelled) return
            if (reconnectAttempts < 5) {
              reconnectAttempts++
              const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000)
              console.warn(`[Engine] SSE closed, reconnecting in ${delay}ms (attempt ${reconnectAttempts})`)
              // Close old EventSource before scheduling reconnect
              eventSource?.close()
              const timerId = setTimeout(async () => {
                if (cancelled) return
                connectSSE()
                // Store new ref for cleanup
                eventSourceRef.current = eventSource
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
              reconnectTimerRef.current = timerId
            }
          })
          // Store ref for cleanup
          eventSourceRef.current = eventSource
        }
        connectSSE()
      } catch {
        console.warn("[Engine] Backend unavailable, using offline mode")
        if (!cancelled) setConnected(false)
      }
    }
    init()
    fetchNPI()

    return () => {
      cancelled = true
      eventSource?.close()
      eventSourceRef.current?.close()
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current)
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
    // Provider sync callback — set by page.tsx to refetch provider data on SSE event
    setProviderSwitchCallback: (cb: (() => void) | null) => { providerSwitchCallbackRef.current = cb },
  }
}

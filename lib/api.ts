/**
 * OmniSight Engine API client
 * Connects the Next.js frontend to the FastAPI backend.
 */

// Use relative path — Next.js rewrites proxy /api/v1/* to the Python backend.
// This avoids CORS and WSL2↔Windows networking issues.
const API_V1 = process.env.NEXT_PUBLIC_API_URL
  ? `${process.env.NEXT_PUBLIC_API_URL}/api/v1`
  : "/api/v1"

// ─── Persistent SSE Events ───

export type SSEEvent =
  | { event: "agent_update"; data: { agent_id: string; status: string; thought_chain: string; timestamp: string } }
  | { event: "task_update"; data: { task_id: string; status: string; assigned_agent_id: string | null; timestamp: string } }
  | { event: "tool_progress"; data: { tool_name: string; phase: "start" | "done" | "error"; output: string; timestamp: string; index?: number; success?: boolean } }
  | { event: "pipeline"; data: { phase: string; detail: string; timestamp: string } }
  | { event: "workspace"; data: { agent_id: string; action: string; detail: string; timestamp: string } }
  | { event: "container"; data: { agent_id: string; action: string; detail: string; timestamp: string } }
  | { event: "invoke"; data: { action_type: string; detail: string; timestamp: string } }
  | { event: "token_warning"; data: { level: string; message: string; usage: number; budget: number; timestamp: string } }
  | { event: "notification"; data: { id: string; level: string; title: string; message: string; source: string; timestamp: string; action_url?: string; action_label?: string } }
  | { event: "artifact_created"; data: { id: string; name: string; type: string; task_id: string; agent_id: string; size: number } }
  | { event: "simulation"; data: { sim_id: string; action: "start" | "progress" | "result"; detail: string; status?: string; track?: string; module?: string; tests_total?: number; tests_passed?: number; timestamp: string } }
  | { event: "heartbeat"; data: { subscribers: number } }

/**
 * Subscribe to the persistent SSE event stream from the backend.
 * Returns an EventSource that auto-reconnects.
 */
export function subscribeEvents(
  onEvent: (event: SSEEvent) => void,
  onError?: (err: Event) => void,
): EventSource {
  // EventSource needs absolute URL in some environments
  const eventsUrl = API_V1.startsWith("http") ? `${API_V1}/events` : `${window.location.origin}${API_V1}/events`
  const es = new EventSource(eventsUrl)

  for (const eventType of ["agent_update", "task_update", "tool_progress", "pipeline", "workspace", "container", "invoke", "token_warning", "notification", "artifact_created", "simulation", "heartbeat"]) {
    es.addEventListener(eventType, (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data)
        onEvent({ event: eventType, data } as SSEEvent)
      } catch { /* skip malformed */ }
    })
  }

  es.onerror = (e) => {
    onError?.(e)
  }

  return es
}

// ─── Helpers ───

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_V1}${path}`, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text().catch(() => "")
    throw new Error(`API ${res.status}: ${body}`)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

// ─── Health ───

export async function getHealth() {
  return request<{ status: string; engine: string; version: string }>("/health")
}

// ─── Agents ───

export interface ApiAgent {
  id: string
  name: string
  type: string
  sub_type: string
  status: string
  progress: { current: number; total: number }
  thought_chain: string
  ai_model: string | null
  sub_tasks: { id: string; label: string; status: string }[]
}

export async function listAgents() {
  return request<ApiAgent[]>("/agents")
}

export async function getAgent(id: string) {
  return request<ApiAgent>(`/agents/${id}`)
}

export async function createAgent(body: { name: string; type: string; sub_type?: string; ai_model?: string }) {
  return request<ApiAgent>("/agents", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function updateAgentStatus(id: string, status: string) {
  return request<ApiAgent>(`/agents/${id}?status=${status}`, { method: "PATCH" })
}

export async function deleteAgent(id: string) {
  return request<void>(`/agents/${id}`, { method: "DELETE" })
}

// ─── Tasks ───

export interface ApiTask {
  id: string
  title: string
  description: string | null
  priority: string
  status: string
  assigned_agent_id: string | null
  created_at: string
  completed_at: string | null
  ai_analysis: string | null
  suggested_agent_type: string | null
  suggested_sub_type: string | null
  parent_task_id: string | null
  child_task_ids: string[]
  external_issue_id: string | null
  issue_url: string | null
  acceptance_criteria: string | null
  labels: string[]
}

export async function listTasks() {
  return request<ApiTask[]>("/tasks")
}

export async function createTask(body: {
  title: string
  description?: string
  priority?: string
  suggested_agent_type?: string
  external_issue_id?: string
  issue_url?: string
  acceptance_criteria?: string
  labels?: string[]
}) {
  return request<ApiTask>("/tasks", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function updateTask(
  id: string,
  body: { status?: string; assigned_agent_id?: string; title?: string }
) {
  return request<ApiTask>(`/tasks/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
}

export async function deleteTask(id: string) {
  return request<void>(`/tasks/${id}`, { method: "DELETE" })
}

// ─── Chat ───

export interface ApiChatMessage {
  id: string
  role: "user" | "orchestrator" | "system"
  content: string
  timestamp: string
  suggestion?: {
    id: string
    type: string
    title: string
    description: string
    task_id?: string
    agent_id?: string
    agent_type?: string
    priority: string
    status: string
  } | null
}

export async function sendChat(message: string) {
  return request<{ message: ApiChatMessage }>("/chat", {
    method: "POST",
    body: JSON.stringify({ message }),
  })
}

/**
 * SSE streaming chat — yields tokens as they arrive.
 */
export async function* streamChat(
  message: string
): AsyncGenerator<{ event: string; data: unknown }> {
  const res = await fetch(`${API_V1}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  })
  if (!res.ok || !res.body) throw new Error(`Stream error: ${res.status}`)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split("\n")
    buffer = lines.pop() || ""

    let currentEvent = "message"
    for (const line of lines) {
      if (line.startsWith("event:")) {
        currentEvent = line.slice(6).trim()
      } else if (line.startsWith("data:")) {
        try {
          const data = JSON.parse(line.slice(5).trim())
          yield { event: currentEvent, data }
        } catch {
          // skip malformed data lines
        }
      }
    }
  }
}

export async function getChatHistory() {
  return request<ApiChatMessage[]>("/chat/history")
}

export async function clearChatHistory() {
  return request<void>("/chat/history", { method: "DELETE" })
}

// ─── Providers ───

export interface ProviderConfig {
  id: string
  name: string
  default_model: string
  models: string[]
  requires_key: boolean
  env_var: string | null
  configured: boolean
  base_url?: string
}

export interface ProvidersResponse {
  active_provider: string
  active_model: string
  providers: ProviderConfig[]
}

export async function getProviders() {
  return request<ProvidersResponse>("/providers")
}

export async function switchProvider(provider: string, model?: string) {
  return request<{ status: string; provider: string; model: string }>(
    "/providers/switch",
    {
      method: "POST",
      body: JSON.stringify({ provider, model }),
    }
  )
}

export async function testProvider() {
  return request<{
    status: string
    provider: string
    model: string
    response?: string
    message?: string
    error?: string
  }>("/providers/test")
}

// ─── System Info ───

export interface SystemInfo {
  hostname: string
  os: string
  kernel: string
  arch: string
  cpu_model: string
  cpu_cores: number
  cpu_usage: number
  memory_total: number
  memory_used: number
  disk_total_mb: number
  disk_used_mb: number
  disk_use_pct: string
  uptime: string
  wsl: boolean
  docker: boolean
}

export interface SystemDevice {
  id: string
  name: string
  type: "usb" | "camera" | "storage" | "network" | "display"
  status: "connected" | "disconnected" | "detecting" | "error"
  vendorId?: string
  productId?: string
  speed?: string | null
  mountPoint?: string
}

export interface SystemStatus {
  tasks_completed: number
  tasks_total: number
  agents_running: number
  wsl_status: string
  usb_status: string
  cpu_summary: string
  memory_summary: string
  workspaces_active: number
  containers_active: number
}

export async function getSystemInfo() {
  return request<SystemInfo>("/system/info")
}

export async function getDevices() {
  return request<SystemDevice[]>("/system/devices")
}

export async function getSystemStatus() {
  return request<SystemStatus>("/system/status")
}

// ─── Spec ───

export interface SpecValue {
  key: string
  value: string | number | boolean | SpecValue[]
  type?: "hardware" | "software" | "config" | "default"
  options?: string[]
  step?: number
  min?: number
  max?: number
}

export async function getSpec() {
  return request<SpecValue[]>("/system/spec")
}

export async function updateSpec(path: string[], value: string | number | boolean) {
  return request<{ status: string }>("/system/spec", {
    method: "PUT",
    body: JSON.stringify({ path, value }),
  })
}

// ─── Repos ───

export interface RepoInfo {
  id: string
  name: string
  url: string
  branch: string
  status: "synced" | "syncing" | "error" | "detached"
  lastCommit: string
  lastCommitTime: string
  tetheredAgentId: string | null
}

export async function getRepos() {
  return request<RepoInfo[]>("/system/repos")
}

// ─── Logs ───

export interface LogEntry {
  timestamp: string
  message: string
  level: "info" | "warn" | "error"
}

export async function getLogs(limit: number = 50) {
  return request<LogEntry[]>(`/system/logs?limit=${limit}`)
}

// ─── Token Usage ───

export interface TokenUsage {
  model: string
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cost: number
  request_count: number
  avg_latency: number
  last_used: string
}

export async function getTokenUsage() {
  return request<TokenUsage[]>("/system/tokens")
}

export interface CompressionStats {
  total_original_bytes: number
  total_compressed_bytes: number
  compression_count: number
  total_lines_removed: number
  avg_ratio: number
  estimated_tokens_saved: number
}

export async function getCompressionStats() {
  return request<CompressionStats>("/system/compression")
}

// ─── Simulations ───

export interface SimulationItem {
  id: string
  task_id: string | null
  agent_id: string | null
  track: "algo" | "hw"
  module: string
  status: "running" | "pass" | "fail" | "error"
  tests_total: number
  tests_passed: number
  tests_failed: number
  coverage_pct: number
  valgrind_errors: number
  duration_ms: number
  report_json?: Record<string, unknown>
  created_at: string
}

export async function listSimulations(params?: { task_id?: string; status?: string }): Promise<SimulationItem[]> {
  const qs = new URLSearchParams()
  if (params?.task_id) qs.set("task_id", params.task_id)
  if (params?.status) qs.set("status", params.status)
  return request<SimulationItem[]>(`/simulations?${qs.toString()}`)
}

export async function getSimulation(simId: string): Promise<SimulationItem> {
  return request<SimulationItem>(`/simulations/${simId}`)
}

export async function triggerSimulation(body: { track: string; module: string; input_data?: string; mock?: boolean; platform?: string }): Promise<{ result: string }> {
  return request<{ result: string }>("/simulations", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

// ─── Artifacts ───

export interface ArtifactItem {
  id: string
  task_id: string | null
  agent_id: string | null
  name: string
  type: "pdf" | "markdown" | "json" | "log" | "html"
  file_path: string
  size: number
  created_at: string
}

export async function listArtifacts(taskId?: string, agentId?: string) {
  const params = new URLSearchParams()
  if (taskId) params.set("task_id", taskId)
  if (agentId) params.set("agent_id", agentId)
  return request<ArtifactItem[]>(`/artifacts?${params.toString()}`)
}

export async function getArtifactDownloadUrl(id: string): Promise<string> {
  return `${API_V1}/artifacts/${id}/download`
}

// ─── NPI Lifecycle ───

export interface NPIMilestone {
  id: string
  title: string
  track: "engineering" | "design" | "market"
  status: "pending" | "in_progress" | "completed" | "blocked"
  due_date?: string
  jira_tag?: string
}

export interface NPIPhase {
  id: string
  name: string
  short_name: string
  order: number
  status: "pending" | "active" | "completed" | "blocked"
  milestones: NPIMilestone[]
}

export interface NPIData {
  business_model: "odm" | "oem" | "jdm" | "obm"
  current_phase_id?: string
  phases: NPIPhase[]
}

export async function getNPIState() {
  return request<NPIData>("/system/npi")
}

export async function updateNPIState(updates: { business_model?: string; current_phase_id?: string }) {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(updates)) {
    if (v !== undefined) params.set(k, v)
  }
  return request<NPIData>(`/system/npi?${params.toString()}`, { method: "PUT" })
}

export async function updateNPIMilestone(milestoneId: string, status: string) {
  return request<NPIMilestone>(`/system/npi/milestones/${milestoneId}?status=${status}`, { method: "PATCH" })
}

// ─── Token Budget ───

export interface TokenBudgetInfo {
  budget: number
  usage: number
  ratio: number
  frozen: boolean
  level: string  // "normal" | "warn" | "downgrade" | "frozen"
  warn_threshold: number
  downgrade_threshold: number
  freeze_threshold: number
  fallback_provider: string
  fallback_model: string
}

export async function getTokenBudget() {
  return request<TokenBudgetInfo>("/system/token-budget")
}

export async function updateTokenBudget(updates: {
  budget?: number
  warn_threshold?: number
  downgrade_threshold?: number
  freeze_threshold?: number
  fallback_provider?: string
  fallback_model?: string
}) {
  const params = new URLSearchParams()
  for (const [key, val] of Object.entries(updates)) {
    if (val !== undefined) params.set(key, String(val))
  }
  return request<TokenBudgetInfo>(`/system/token-budget?${params.toString()}`, { method: "PUT" })
}

export async function resetTokenFreeze() {
  return request<{ status: string }>("/system/token-budget/reset", { method: "POST" })
}

// ─── Notifications ───

export interface NotificationItem {
  id: string
  level: "info" | "warning" | "action" | "critical"
  title: string
  message: string
  source: string
  timestamp: string
  read: boolean
  action_url?: string
  action_label?: string
}

export async function getNotifications(limit: number = 50, level?: string) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (level) params.set("level", level)
  return request<NotificationItem[]>(`/system/notifications?${params.toString()}`)
}

export async function markNotificationRead(id: string) {
  return request<{ status: string }>(`/system/notifications/${id}/read`, { method: "POST" })
}

export async function getUnreadCount() {
  return request<{ count: number }>("/system/notifications/unread-count")
}

// ─── Invoke (Singularity Sync) ───

export interface InvokeAction {
  type: "command" | "assign" | "retry" | "report" | "health"
  // assign
  task_id?: string
  task_title?: string
  agent_id?: string
  agent_name?: string
  // command
  routed_to?: string
  answer?: string
  // report
  summary?: string
  // health
  agent_count?: number
  task_count?: number
  running?: number
  idle?: number
  pending?: number
  // error
  error?: string
}

export interface InvokeAnalysis {
  agents_total: number
  agents_idle: number
  agents_running: number
  agents_error: number
  tasks_unassigned: number
  tasks_in_progress: number
  tasks_completed: number
  planned_actions: number
  action_types: string[]
}

export type InvokeEvent =
  | { event: "analysis"; data: InvokeAnalysis }
  | { event: "phase"; data: { phase: string; message: string } }
  | { event: "action"; data: InvokeAction }
  | { event: "done"; data: { action_count: number; results: string[]; timestamp: string } }

/**
 * SSE streaming invoke — yields events as the system analyses and acts.
 */
export async function haltInvoke(): Promise<{ status: string }> {
  return request<{ status: string }>("/invoke/halt", { method: "POST" })
}

export async function resumeInvoke(): Promise<{ status: string }> {
  return request<{ status: string }>("/invoke/resume", { method: "POST" })
}

export async function* streamInvoke(
  command?: string
): AsyncGenerator<InvokeEvent> {
  const params = command ? `?command=${encodeURIComponent(command)}` : ""
  const res = await fetch(`${API_V1}/invoke/stream${params}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  })
  if (!res.ok || !res.body) throw new Error(`Invoke error: ${res.status}`)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split("\n")
    buffer = lines.pop() || ""

    let currentEvent = "message"
    for (const line of lines) {
      if (line.startsWith("event:")) {
        currentEvent = line.slice(6).trim()
      } else if (line.startsWith("data:")) {
        try {
          const data = JSON.parse(line.slice(5).trim())
          yield { event: currentEvent, data } as InvokeEvent
        } catch {
          // skip malformed
        }
      }
    }
  }
}

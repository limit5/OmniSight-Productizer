/**
 * OmniSight Engine API client
 * Connects the Next.js frontend to the FastAPI backend.
 */

// Use relative path — Next.js rewrites proxy /api/v1/* to the Python backend.
// This avoids CORS and WSL2↔Windows networking issues.
// R2 #37: if NEXT_PUBLIC_API_URL is set, validate it looks like a URL
// before handing it to EventSource / fetch. A malformed value fails
// loud here instead of throwing deep inside the streaming code.
function _resolveApiBase(): string {
  const env = process.env.NEXT_PUBLIC_API_URL
  if (!env) return "/api/v1"
  try {
    new URL(env)  // throws on invalid
  } catch {
    // Dev-time signal; still fall back to relative so the app works.
    if (typeof console !== "undefined") {
      console.error(
        `[api] NEXT_PUBLIC_API_URL=${env} is not a valid URL; falling back to /api/v1`,
      )
    }
    return "/api/v1"
  }
  return `${env.replace(/\/+$/, "")}/api/v1`
}
const API_V1 = _resolveApiBase()

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
  | { event: "simulation"; data: { sim_id: string; action: "start" | "progress" | "result"; detail: string; status?: string; track?: string; module?: string; tests_total?: number; tests_passed?: number; tests_failed?: number; timestamp: string } }
  | { event: "debug_finding"; data: { id: string; task_id: string; agent_id: string; finding_type: string; severity: string; message: string; timestamp: string } }
  | { event: "heartbeat"; data: { subscribers: number } }
  // ─── Phase 47: Autonomous Decision Engine ───
  | { event: "mode_changed"; data: { mode: OperationMode; previous: OperationMode; parallel_cap: number; in_flight: number; over_cap: number; timestamp: string } }
  // Phase 47 decisions always carry timestamp (publisher sets it in bus.publish);
  // intersect with DecisionPayload so consumers don't branch on optional.
  | { event: "decision_pending"; data: DecisionPayload & { timestamp: string } }
  | { event: "decision_auto_executed"; data: DecisionPayload & { timestamp: string } }
  | { event: "decision_resolved"; data: DecisionPayload & { timestamp: string } }
  | { event: "decision_undone"; data: DecisionPayload & { timestamp: string } }
  | { event: "budget_strategy_changed"; data: { strategy: BudgetStrategyId; previous: BudgetStrategyId; tuning: BudgetTuning; timestamp: string } }

// ─── Global SSE manager ───
// 48A-Fix P0: a single EventSource per origin, shared across every caller.
// Each `subscribeEvents()` now registers a listener on the shared stream
// instead of opening its own connection. Closing the returned handle only
// removes the listener; the underlying EventSource is torn down when the
// last subscriber leaves. This fixes both the 3×-connection waste and the
// browser's 6-connection-per-origin hard cap.

// Event type names the backend actually emits — keep in sync with
// sse_schemas.SSE_EVENT_SCHEMAS.
const SSE_EVENT_TYPES = [
  "agent_update",
  "task_update",
  "tool_progress",
  "pipeline",
  "workspace",
  "container",
  "invoke",
  "token_warning",
  "notification",
  "artifact_created",
  "simulation",
  "debug_finding",
  "heartbeat",
  // Phase 47 decision engine
  "mode_changed",
  "decision_pending",
  "decision_auto_executed",
  "decision_resolved",
  "decision_undone",
  "budget_strategy_changed",
] as const

type SSEListener = (ev: SSEEvent) => void
type ErrorListener = (err: Event) => void

let _sharedES: EventSource | null = null
const _sseListeners = new Set<SSEListener>()
const _sseErrorListeners = new Set<ErrorListener>()

function _ensureSharedEventSource(): EventSource {
  if (_sharedES && _sharedES.readyState !== EventSource.CLOSED) {
    return _sharedES
  }
  const eventsUrl = API_V1.startsWith("http")
    ? `${API_V1}/events`
    : `${window.location.origin}${API_V1}/events`
  const es = new EventSource(eventsUrl)
  for (const eventType of SSE_EVENT_TYPES) {
    es.addEventListener(eventType, (e: MessageEvent) => {
      let data: unknown
      try { data = JSON.parse(e.data) } catch { return }
      const payload = { event: eventType, data } as SSEEvent
      // Copy the set so a listener removing itself mid-dispatch is safe.
      for (const l of Array.from(_sseListeners)) {
        try { l(payload) } catch (err) { console.warn("[SSE listener error]", err) }
      }
    })
  }
  es.onerror = (e) => {
    for (const l of Array.from(_sseErrorListeners)) {
      try { l(e) } catch { /* swallow */ }
    }
  }
  _sharedES = es
  return es
}

/**
 * Subscribe to the persistent SSE event stream. All callers share a single
 * underlying EventSource. Returns a handle whose `.close()` removes only
 * this subscriber; when the last one leaves, the connection is torn down.
 *
 * The returned object keeps `readyState` / `close()` members so existing
 * call sites that treat the return value as an EventSource continue to
 * work. New code should just call `.close()`.
 */
export function subscribeEvents(
  onEvent: SSEListener,
  onError?: ErrorListener,
): { close: () => void; readyState: number } {
  const es = _ensureSharedEventSource()
  _sseListeners.add(onEvent)
  if (onError) _sseErrorListeners.add(onError)

  let closed = false
  return {
    get readyState() {
      return _sharedES ? _sharedES.readyState : EventSource.CLOSED
    },
    close() {
      if (closed) return
      closed = true
      _sseListeners.delete(onEvent)
      if (onError) _sseErrorListeners.delete(onError)
      if (_sseListeners.size === 0 && _sseErrorListeners.size === 0 && _sharedES) {
        _sharedES.close()
        _sharedES = null
      }
    },
  }
}

// ─── Helpers ───

const FETCH_TIMEOUT = 15_000 // 15 seconds
const MAX_RETRIES = 2

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let lastError: Error | null = null
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT)
    try {
      const res = await fetch(`${API_V1}${path}`, {
        signal: controller.signal,
        headers: { "Content-Type": "application/json", ...init?.headers },
        ...init,
      })
      clearTimeout(timer)
      if (!res.ok) {
        const body = await res.text().catch(() => "")
        const method = (init?.method || "GET").toUpperCase()
        const isIdempotent = ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"].includes(method)

        // Retry on 429 (rate limited) and 503 (overloaded) — all methods, with backoff
        if ((res.status === 429 || res.status === 503) && attempt < MAX_RETRIES) {
          const retryAfter = parseInt(res.headers.get("Retry-After") || "0", 10)
          const delay = retryAfter > 0 ? retryAfter * 1000 : 1000 * Math.pow(2, attempt)
          lastError = new Error(`API ${res.status}: ${body}`)
          console.warn(`[API] ${res.status} on ${path}, retrying in ${delay}ms (attempt ${attempt + 1}/${MAX_RETRIES})`)
          await new Promise(r => setTimeout(r, delay))
          continue
        }
        // Retry idempotent methods on 5xx
        if (res.status >= 500 && isIdempotent && attempt < MAX_RETRIES) {
          lastError = new Error(`API ${res.status}: ${body}`)
          await new Promise(r => setTimeout(r, 1000 * (attempt + 1)))
          continue
        }
        throw new Error(`API ${res.status}: ${body}`)
      }
      if (res.status === 204) return undefined as T
      return res.json()
    } catch (e) {
      clearTimeout(timer)
      if (e instanceof DOMException && e.name === "AbortError") {
        const method = (init?.method || "GET").toUpperCase()
        const isIdempotent = ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"].includes(method)
        lastError = new Error(`Request timeout: ${path}`)
        if (isIdempotent && attempt < MAX_RETRIES) {
          await new Promise(r => setTimeout(r, 1000 * (attempt + 1)))
          continue
        }
      }
      throw lastError || e
    }
  }
  throw lastError!
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
  workspace?: { branch: string; path: string; status: string; commit_count: number; task_id: string | null; remote_name: string; repo_url: string }
  file_scope?: string[]
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
  depends_on?: string[]
  external_issue_id: string | null
  issue_url: string | null
  external_issue_platform?: string | null
  last_external_sync_at?: string | null
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

// ─── Handoffs ───

export interface HandoffItem {
  task_id: string
  agent_id: string
  created_at: string
}

export async function getTaskHandoffs(taskId: string): Promise<HandoffItem[]> {
  return request<HandoffItem[]>(`/tasks/${taskId}/handoffs`)
}

export async function getRecentHandoffs(limit: number = 20): Promise<HandoffItem[]> {
  return request<HandoffItem[]>(`/tasks/handoffs/recent?limit=${limit}`)
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

  try {
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
    if (buffer.trim().length > 0) {
      yield { event: "error", data: { reason: "stream_truncated", partial: buffer } }
    }
  } finally {
    try { reader.releaseLock() } catch { /* already released */ }
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

export interface ProviderHealth {
  id: string
  name: string
  configured: boolean
  is_active: boolean
  last_failure: number | null
  cooldown_remaining: number
  status: "active" | "cooldown" | "available" | "unconfigured"
}

export interface ProviderHealthResponse {
  chain: string[]
  health: ProviderHealth[]
}

export async function getProviderHealth(): Promise<ProviderHealthResponse> {
  return request<ProviderHealthResponse>("/providers/health")
}

export async function updateFallbackChain(chain: string[]): Promise<{ status: string; chain: string[] }> {
  return request<{ status: string; chain: string[] }>("/providers/fallback-chain", {
    method: "PUT",
    body: JSON.stringify({ chain }),
  })
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
  type: "usb" | "camera" | "storage" | "network" | "display" | "evk"
  status: "connected" | "disconnected" | "detecting" | "error"
  vendorId?: string
  productId?: string
  speed?: string | null
  mountPoint?: string
  v4l2_device?: string
  deploy_target_ip?: string
  deploy_method?: string
  reachable?: boolean
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
  status: "synced" | "syncing" | "error" | "detached" | "unconfigured"
  lastCommit: string
  lastCommitTime: string
  tetheredAgentId: string | null
  platform?: "github" | "gitlab" | "gerrit" | "unknown"
  repoId?: string
  remotes?: Record<string, string>
  authStatus?: "ok" | "no_token" | "no_key" | "unknown"
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
  track: "algo" | "hw" | "npu"
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
  // NPU-specific fields (only present for npu track)
  npu_latency_ms?: number
  npu_throughput_fps?: number
  accuracy_delta?: number
  model_size_kb?: number
  npu_framework?: string
}

export async function listSimulations(params?: { task_id?: string; status?: string }): Promise<SimulationItem[]> {
  const qs = new URLSearchParams()
  if (params?.task_id) qs.set("task_id", params.task_id)
  if (params?.status) qs.set("status", params.status)
  return request<SimulationItem[]>(`/system/simulations?${qs.toString()}`)
}

export async function getSimulation(simId: string): Promise<SimulationItem> {
  return request<SimulationItem>(`/system/simulations/${simId}`)
}

export async function triggerSimulation(body: { track: string; module: string; input_data?: string; mock?: boolean; platform?: string }): Promise<{ result: string }> {
  return request<{ result: string }>("/system/simulations", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

// ─── Integration Settings ───

export async function getSettings(): Promise<Record<string, Record<string, unknown>>> {
  return request<Record<string, Record<string, unknown>>>("/system/settings")
}

export async function updateSettings(updates: Record<string, string | number | boolean>): Promise<{ status: string; applied: string[]; rejected: Record<string, string> }> {
  return request<{ status: string; applied: string[]; rejected: Record<string, string> }>("/system/settings", {
    method: "PUT",
    body: JSON.stringify({ updates }),
  })
}

export async function testIntegration(type: string): Promise<{ status: string; message?: string; [key: string]: unknown }> {
  return request<{ status: string; message?: string }>(`/system/test/${type}`, { method: "POST" })
}

export async function createVendorSDK(body: Record<string, unknown>): Promise<{ status: string; platform: string }> {
  return request<{ status: string; platform: string }>("/system/vendor/sdks", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function deleteVendorSDK(platform: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/system/vendor/sdks/${platform}`, { method: "DELETE" })
}

// ─── Event Replay ───

export interface ReplayEvent {
  id: number
  event: string
  data: Record<string, unknown>
  timestamp: string
}

export async function replayEvents(since: string, limit: number = 200): Promise<ReplayEvent[]> {
  const qs = new URLSearchParams()
  if (since) qs.set("since", since)
  qs.set("limit", String(limit))
  return request<ReplayEvent[]>(`/events/replay?${qs.toString()}`)
}

// ─── Artifacts ───

export interface ArtifactItem {
  id: string
  task_id: string | null
  agent_id: string | null
  name: string
  type: "pdf" | "markdown" | "json" | "log" | "html" | "binary" | "firmware" | "kernel_module" | "sdk" | "model" | "archive"
  file_path: string
  size: number
  created_at: string
  version?: string
  checksum?: string
}

export async function listArtifacts(taskId?: string, agentId?: string) {
  const params = new URLSearchParams()
  if (taskId) params.set("task_id", taskId)
  if (agentId) params.set("agent_id", agentId)
  return request<ArtifactItem[]>(`/artifacts?${params.toString()}`)
}

export function getArtifactDownloadUrl(id: string): string {
  return `${API_V1}/artifacts/${id}/download`
}

// ─── NPI Lifecycle ───

export interface NPIMilestone {
  id: string
  title: string
  track: "engineering" | "design" | "market"
  status: "pending" | "in_progress" | "completed" | "blocked"
  due_date?: string
  completed_date?: string | null
  assigned_agent_type?: string | null
  jira_tag?: string
}

export interface NPIPhase {
  id: string
  name: string
  short_name: string
  order: number
  status: "pending" | "active" | "completed" | "blocked"
  start_date?: string | null
  target_date?: string | null
  completed_date?: string | null
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

  try {
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
    // Surface partial trailing chunk as an explicit truncation signal so
    // the consumer doesn't mistake a dropped connection for a clean end.
    if (buffer.trim().length > 0) {
      yield { event: "error", data: { reason: "stream_truncated", partial: buffer } } as unknown as InvokeEvent
    }
  } finally {
    try { reader.releaseLock() } catch { /* already released */ }
  }
}


// ─── Phase 47: Autonomous Decision Engine ────────────────────────────────────

export type OperationMode = "manual" | "supervised" | "full_auto" | "turbo"
export type DecisionSeverity = "info" | "routine" | "risky" | "destructive"
export type DecisionStatus =
  | "pending"
  | "auto_executed"
  | "approved"
  | "rejected"
  | "undone"
  | "timeout_default"

export interface DecisionOption {
  id: string
  label: string
  description?: string
  is_safe_default?: boolean
}

// Known keys the backend attaches to Decision.source. Other keys may
// appear (stuck_detector passes arbitrary context), so this is a
// narrowed-but-open interface: named fields typed, rest preserved.
export interface DecisionSource {
  agent_id?: string | null
  task_id?: string | null
  reason?: string
  [extra: string]: unknown
}

export interface DecisionPayload {
  id: string
  kind: string
  severity: DecisionSeverity
  title: string
  detail: string
  status: DecisionStatus
  options: DecisionOption[]
  default_option_id: string | null
  chosen_option_id: string | null
  resolver: "user" | "auto" | "timeout" | null
  created_at: number
  deadline_at: number | null
  resolved_at: number | null
  source: DecisionSource
  timestamp?: string
}

export interface OperationModeInfo {
  mode: OperationMode
  parallel_cap: number
  in_flight: number
  modes: OperationMode[]
}

export async function getOperationMode() {
  return request<OperationModeInfo>("/operation-mode")
}

export async function setOperationMode(mode: OperationMode) {
  return request<{ mode: OperationMode; parallel_cap: number }>(
    "/operation-mode",
    { method: "PUT", body: JSON.stringify({ mode }) },
  )
}

export async function listDecisions(status: "pending" | "history" = "pending", limit = 100) {
  const params = new URLSearchParams({ status, limit: String(limit) })
  return request<{ items: DecisionPayload[]; count: number }>(
    `/decisions?${params.toString()}`,
  )
}

export async function approveDecision(id: string, option_id: string) {
  return request<DecisionPayload>(
    `/decisions/${id}/approve`,
    { method: "POST", body: JSON.stringify({ option_id }) },
  )
}

export async function rejectDecision(id: string) {
  return request<DecisionPayload>(`/decisions/${id}/reject`, { method: "POST" })
}

export async function undoDecision(id: string) {
  return request<DecisionPayload>(`/decisions/${id}/undo`, { method: "POST" })
}

export async function triggerSweep() {
  return request<{ resolved: number; ids: string[] }>(
    "/decisions/sweep",
    { method: "POST" },
  )
}

// Budget strategy

export type BudgetStrategyId = "quality" | "balanced" | "cost_saver" | "sprint"

export interface BudgetTuning {
  strategy: BudgetStrategyId
  model_tier: "premium" | "default" | "budget"
  max_retries: number
  downgrade_at_usage_pct: number
  freeze_at_usage_pct: number
  prefer_parallel: boolean
}

export interface BudgetStrategyInfo {
  strategy: BudgetStrategyId
  tuning: BudgetTuning
  available: BudgetTuning[]
}

export async function getBudgetStrategy() {
  return request<BudgetStrategyInfo>("/budget-strategy")
}

export async function setBudgetStrategy(strategy: BudgetStrategyId) {
  return request<{ strategy: BudgetStrategyId; tuning: BudgetTuning }>(
    "/budget-strategy",
    { method: "PUT", body: JSON.stringify({ strategy }) },
  )
}

// ─── Phase 50A: Pipeline Timeline ───

export type PipelineStepStatus = "idle" | "active" | "done" | "overdue"

export interface PipelineTimelineStep {
  id: string
  name: string
  npi_phase: string
  auto_advance: boolean
  human_checkpoint: string | null
  planned_at: string | null
  started_at: string | null
  completed_at: string | null
  deadline_at: string | null
  status: PipelineStepStatus
}

export interface PipelineVelocity {
  avg_step_seconds: number
  eta_completion: string | null
  tasks_completed_7d: number
  pipeline_id: string | null
  pipeline_status: string
}

export interface PipelineTimeline {
  steps: PipelineTimelineStep[]
  velocity: PipelineVelocity
}

export async function getPipelineTimeline() {
  return request<PipelineTimeline>("/system/pipeline/timeline")
}

// ─── Phase 50B: Decision Rules Editor ───

export interface DecisionRule {
  id: string
  kind_pattern: string
  severity: DecisionSeverity | null
  auto_in_modes: OperationMode[]
  default_option_id: string | null
  priority: number
  enabled: boolean
  note: string
}

export interface DecisionRulesInfo {
  rules: DecisionRule[]
  severities: DecisionSeverity[]
  modes: OperationMode[]
}

export interface DecisionRulesTestHit {
  kind: string
  rule_id: string | null
  severity: DecisionSeverity | null
  auto: boolean
}

export async function getDecisionRules() {
  return request<DecisionRulesInfo>("/decision-rules")
}

export async function putDecisionRules(rules: Partial<DecisionRule>[]) {
  return request<{ rules: DecisionRule[] }>("/decision-rules", {
    method: "PUT",
    body: JSON.stringify({ rules }),
  })
}

export async function testDecisionRules(kinds: string[], mode?: OperationMode) {
  return request<{ mode: string; hits: DecisionRulesTestHit[] }>(
    "/decision-rules/test",
    { method: "POST", body: JSON.stringify({ kinds, mode }) },
  )
}

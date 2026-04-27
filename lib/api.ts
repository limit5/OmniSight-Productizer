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
  | { event: "notification"; data: { id: string; level: string; title: string; message: string; source: string; timestamp: string; action_url?: string; action_label?: string; severity?: string | null } }
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
  // ─── O9 (#272) Orchestration observability ───
  | {
      event: "orchestration.queue.tick"
      data: {
        queue: OrchestrationQueueSnapshot
        workers: OrchestrationWorkerSnapshot
        timestamp: string
      }
    }
  | {
      event: "orchestration.lock.acquired"
      data: {
        task_id: string
        paths: string[]
        priority: number
        wait_seconds: number
        expires_at: number
        timestamp: string
      }
    }
  | {
      event: "orchestration.lock.released"
      data: { task_id: string; released_count: number; timestamp: string }
    }
  | {
      event: "orchestration.merger.voted"
      data: {
        change_id: string
        file_path: string
        reason: string
        voted_score: number
        confidence: number
        push_sha: string
        review_url: string
        timestamp: string
      }
    }
  | {
      event: "orchestration.change.awaiting_human_plus_two"
      data: {
        change_id: string
        project: string
        file_path: string
        merger_confidence: number
        review_url: string
        push_sha: string
        awaiting_since: number
        jira_ticket: string
        timestamp: string
      }
    }
  | { event: "pep.decision"; data: PepDecisionEvent }
  | { event: "chatops.message"; data: ChatOpsMessageEvent }
  // ─── B12 Cloudflare Tunnel wizard ───
  | { event: "cf_tunnel_provision"; data: { step: string; detail: string; progress: number; timestamp: string } }
  // ─── R2 (#308) Semantic Entropy Monitor ───
  | { event: "agent.entropy"; data: { agent_id: string; entropy_score: number; threshold: number; verdict: "ok" | "warning" | "deadlock"; timestamp: string } }
  // ─── R3 (#309) Scratchpad + Auto-Continuation ───
  | { event: "agent.scratchpad.saved"; data: { agent_id: string; turn: number; size_bytes: number; sections_count: number; timestamp: string } }
  | { event: "agent.token_continuation"; data: { agent_id: string; continuation_round: number; timestamp: string } }
  // ─── H1 / H3: whole-host metrics tick (5s cadence) ───
  | { event: "host.metrics.tick"; data: HostMetricsTickEvent }
  // ─── ZZ.A2 (#303-2) per-turn context-usage snapshot ───
  // Emitted by ``backend/agents/llm.py::TokenTrackingCallback.on_llm_end``
  // at the end of every LLM turn. ``context_limit`` / ``context_usage_pct``
  // are nullable when the YAML has no entry for the provider/model pair
  // (Ollama local, OpenRouter pass-through, unknown providers) — UI must
  // render "—" for nulls instead of a fake 0% (NULL-vs-genuine-zero
  // contract from ZZ.A1).
  | {
      event: "turn_metrics"
      data: {
        provider: string | null
        model: string
        input_tokens: number
        output_tokens: number
        tokens_used: number
        context_limit: number | null
        context_usage_pct: number | null
        latency_ms: number
        cache_read_tokens: number
        cache_create_tokens: number
        timestamp: string
      }
    }
  // ─── ZZ.A3 (#303-3) per-turn tool-execution summary ───
  // Emitted by ``backend/agents/nodes.py::summarizer_node`` once at the
  // end of every graph run. ``agent_type`` + ``task_id`` identify the
  // run; ``tool_call_count`` / ``tool_failure_count`` are the aggregates
  // the TokenUsageStats card's "tools N / failed M" mini-columns render.
  // ``failed_tools`` preserves order + duplicates (retry loops count).
  // The event carries NO model field — summarizer doesn't know which
  // model the LLM node used; the frontend attributes each emission to
  // the most-recently-seen ``turn_metrics.model`` (same graph run).
  | {
      event: "turn_tool_stats"
      data: {
        agent_type: string
        task_id: string | null
        tool_call_count: number
        tool_failure_count: number
        failed_tools: string[]
        timestamp: string
      }
    }
  // ─── ZZ.B1 (#304-1) per-turn terminal event — full messages + tools + cost ───
  // Emitted once per LLM turn from the backend TokenTrackingCallback
  // after ``turn_metrics``. Carries the richer payload the
  // <TurnDetailDrawer> needs (prompt + assistant messages, per-tool-
  // call detail, backend-authoritative cost). Persisted to event_log
  // so ``GET /runtime/turns`` can backfill the ring buffer on reconnect.
  | {
      event: "turn.complete"
      data: {
        turn_id: string
        provider: string | null
        model: string
        agent_type: string | null
        task_id: string | null
        input_tokens: number
        output_tokens: number
        tokens_used: number
        context_limit: number | null
        context_usage_pct: number | null
        latency_ms: number
        cache_read_tokens: number
        cache_create_tokens: number
        cost_usd: number | null
        started_at: string | null
        ended_at: string | null
        summary: string | null
        messages: Array<{
          role: "system" | "user" | "assistant" | "tool"
          content: string
          tokens?: number | null
          tool_name?: string | null
        }>
        tool_calls: Array<{
          name: string
          success: boolean
          args?: Record<string, unknown> | null
          result?: string | null
          duration_ms?: number | null
        }>
        tool_call_count: number
        tool_failure_count: number
        timestamp: string
      }
    }
  // ─── Q.2 (#296) new device login alert (broadcast_scope=user) ───
  | {
      event: "security.new_device_login"
      data: {
        user_id: string
        token_hint: string
        ip: string
        user_agent: string
        timestamp: string
      }
    }
  // ─── Q.3-SUB-1 (#297) workflow_run state sync (broadcast_scope=user) ───
  | {
      event: "workflow_updated"
      data: {
        run_id: string
        status: string
        version: number
        kind: string | null
        timestamp: string
      }
    }
  // ─── Q.3-SUB-3 (#297) notification read-state sync (broadcast_scope=user) ───
  | {
      event: "notification.read"
      data: {
        id: string
        user_id: string
        timestamp: string
      }
    }
  // ─── Q.3-SUB-4 (#297) user preferences sync (broadcast_scope=user) ───
  | {
      event: "preferences.updated"
      data: {
        pref_key: string
        value: string
        user_id: string
        timestamp: string
      }
    }
  // ─── Q.3-SUB-5 (#297) integration-settings non-LLM sync (broadcast_scope=user) ───
  | {
      event: "integration.settings.updated"
      data: {
        fields_changed: string[]
        timestamp: string
      }
    }
  // ─── Q.3-SUB-6 (#297) chat-history cross-device sync (broadcast_scope=user) ───
  | {
      event: "chat.message"
      data: {
        id: string
        user_id: string
        role: "user" | "orchestrator" | "system"
        content: string
        ts: string
        suggestion?: {
          id: string
          type: string
          title: string
          description: string
          task_id?: string
          agent_id?: string
          agent_type?: string
          priority?: string
          status?: string
        }
        timestamp: string
      }
    }
  // ─── ZZ.B2 (#304-2) LLM-generated chat-session auto-title (broadcast_scope=user) ───
  | {
      event: "session.titled"
      data: {
        session_id: string
        user_id: string
        title: string
        source: "auto" | "user"
        timestamp?: string
      }
    }
  // ─── BS.4.4 / BS.7.4 installer progress tick (broadcast_scope=tenant) ───
  // Single channel emitted by ``backend/routers/installer.py::report_progress``
  // after every sidecar progress UPDATE. The ``state`` field in the payload
  // discriminates running / completed / failed / cancelled — there is no
  // separate ``install.completed`` / ``install.failed`` channel even though
  // the BS.7.4 row hint reads as if there were three. State-based dispatch
  // in the consumer covers all three cases.
  | {
      event: "installer_progress"
      data: {
        job_id: string
        state: InstallJobState
        stage: string
        bytes_done: number
        bytes_total: number | null
        eta_seconds: number | null
        log_tail: string
        sidecar_id: string | null
        entry_id: string | null
        timestamp: string
      }
    }

export interface HostMetricsTickSample {
  cpu_percent: number
  mem_percent: number
  mem_used_gb: number
  mem_total_gb: number
  disk_percent: number
  disk_used_gb: number
  disk_total_gb: number
  loadavg_1m: number
  loadavg_5m: number
  loadavg_15m: number
  sampled_at: number
}

export interface HostMetricsTickDocker {
  container_count: number
  total_mem_reservation_bytes: number
  source: string
  sampled_at: number
}

export interface HostMetricsTickBaseline {
  cpu_cores: number
  mem_total_gb: number
  disk_total_gb: number
  cpu_model: string
}

export interface HostMetricsTickEvent {
  host: HostMetricsTickSample
  docker: HostMetricsTickDocker
  baseline: HostMetricsTickBaseline
  high_pressure: boolean
  sampled_at: number
  timestamp?: string
}

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
  // ─── O9 (#272) Orchestration observability ───
  "orchestration.queue.tick",
  "orchestration.lock.acquired",
  "orchestration.lock.released",
  "orchestration.merger.voted",
  "orchestration.change.awaiting_human_plus_two",
  // ─── R0 (#306) PEP Gateway ───
  "pep.decision",
  // ─── R1 (#307) ChatOps Interactive ───
  "chatops.message",
  // ─── H1 / H3 whole-host metrics tick ───
  "host.metrics.tick",
  // ─── ZZ.A2 (#303-2) per-turn context-usage snapshot for TokenUsageStats card ───
  "turn_metrics",
  // ─── ZZ.A3 (#303-3) per-turn tool-execution summary for TokenUsageStats card ───
  "turn_tool_stats",
  // ─── ZZ.B1 (#304-1) per-turn terminal event carrying messages + tool detail + cost ───
  "turn.complete",
  // ─── Q.3-SUB-1 (#297) workflow_run state sync ───
  "workflow_updated",
  // ─── Q.3-SUB-3 (#297) notification read-state sync ───
  "notification.read",
  // ─── Q.3-SUB-4 (#297) user preferences cross-device sync ───
  "preferences.updated",
  // ─── Q.3-SUB-5 (#297) integration-settings cross-device sync (non-LLM) ───
  "integration.settings.updated",
  // ─── Q.3-SUB-6 (#297) chat-history cross-device sync ───
  "chat.message",
  // ─── ZZ.B2 (#304-2) LLM-generated chat-session auto-title push ───
  "session.titled",
  // ─── BS.4.4 / BS.7.4 installer sidecar progress tick ───
  "installer_progress",
] as const

export type BroadcastScope = "session" | "user" | "global" | "tenant"
export type SSEFilterMode = "this_session" | "all_sessions"

// V0 #6 — workspace-scoped SSE routing.  The three product-line
// workspaces (`web` / `mobile` / `software`) each mount their own
// SSE subscribers; the command-center dashboard mounts one too.
// An event that carries `_workspace_type` belongs to exactly that
// workspace and must not reach any other surface — including the
// command center, whose `_currentWorkspaceType === null` is the
// "no workspace attached" sentinel that rejects these events by
// design (so agent chatter from `/workspace/web` doesn't pollute
// the Agent Matrix Wall).
export type WorkspaceType = "web" | "mobile" | "software"

type SSEListener = (ev: SSEEvent) => void
type ErrorListener = (err: Event) => void

let _sharedES: EventSource | null = null
const _sseListeners = new Set<SSEListener>()
const _sseErrorListeners = new Set<ErrorListener>()

let _currentSessionId: string | null = null
let _currentTenantId: string | null = null
// Y5 (#281) row 4 — current project id, paired with X-Project-Id
// header on every request when set. Validated server-side by
// backend.main._project_header_gate (mirror of I7 _tenant_header_gate).
let _currentProjectId: string | null = null
let _currentWorkspaceType: WorkspaceType | null = null
let _sseFilterMode: SSEFilterMode = "this_session"
const _filterModeListeners = new Set<(mode: SSEFilterMode) => void>()

export function setCurrentSessionId(sid: string | null): void {
  _currentSessionId = sid
}
export function getCurrentSessionId(): string | null {
  return _currentSessionId
}
export function setCurrentTenantId(tid: string | null): void {
  _currentTenantId = tid
}
export function getCurrentTenantId(): string | null {
  return _currentTenantId
}
export function setCurrentProjectId(pid: string | null): void {
  _currentProjectId = pid
}
export function getCurrentProjectId(): string | null {
  return _currentProjectId
}
export function setCurrentWorkspaceType(type: WorkspaceType | null): void {
  _currentWorkspaceType = type
}
export function getCurrentWorkspaceType(): WorkspaceType | null {
  return _currentWorkspaceType
}
export function setSSEFilterMode(mode: SSEFilterMode): void {
  _sseFilterMode = mode
  for (const l of Array.from(_filterModeListeners)) {
    try { l(mode) } catch { /* swallow */ }
  }
}
export function getSSEFilterMode(): SSEFilterMode {
  return _sseFilterMode
}
export function onFilterModeChange(cb: (mode: SSEFilterMode) => void): () => void {
  _filterModeListeners.add(cb)
  return () => { _filterModeListeners.delete(cb) }
}

function _shouldDeliverEvent(data: Record<string, unknown>): boolean {
  const scope = (data._broadcast_scope as BroadcastScope) || "global"
  const eventSessionId = (data._session_id as string) || ""
  const eventTenantId = (data._tenant_id as string) || ""
  const eventWorkspaceType = (data._workspace_type as string) || ""

  // V0 #6 — workspace gate runs before scope-based filters.  A
  // non-empty `_workspace_type` binds the event to exactly that
  // workspace subtree.  The command center (no workspace attached)
  // is isolated by design; cross-workspace bleed is rejected too.
  // Events without `_workspace_type` fall through unchanged — that
  // is the backward-compat contract with the J1/I3 filters.
  if (eventWorkspaceType) {
    if (_currentWorkspaceType === null) return false
    if (_currentWorkspaceType !== eventWorkspaceType) return false
  }

  if (scope === "global") return true
  if (scope === "tenant") {
    if (!_currentTenantId || !eventTenantId) return true
    return eventTenantId === _currentTenantId
  }
  if (!_currentSessionId || !eventSessionId) return true
  if (_sseFilterMode === "all_sessions") return true
  if (scope === "user") return true
  return eventSessionId === _currentSessionId
}

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
      if (!_shouldDeliverEvent(data as Record<string, unknown>)) return
      const payload = { event: eventType, data } as SSEEvent
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
  const _es = _ensureSharedEventSource()
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

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null
  for (const part of document.cookie.split(";")) {
    const [k, ...v] = part.trim().split("=")
    if (k === name) return decodeURIComponent(v.join("="))
  }
  return null
}

// ─── B13 Part C (#339): Global API error handler ────────────────────────
//
// Single point of classification for every non-OK response coming out of
// `request()`. Does three things on the *terminal* failure (after the
// retry loop is exhausted — not on transient retries, so we never
// double-toast):
//
//   1. 401                 → redirect to `/login?next=<current>`
//   2. 503 bootstrap_required → redirect to `/setup-required`
//                              (retained from Part A — kept inline in
//                              `request()` because it short-circuits the
//                              retry loop and swallows the resolution)
//   3. everything else     → emit a typed `ApiError` via the
//                              `onApiError` bus so the FUI toast layer
//                              (or any caller) can surface it.
//
// The handler also emits for offline / timeout failures so the UI can
// show a "連線中斷，嘗試重新連線..." indicator.
//
// We deliberately do NOT import `@/hooks/use-toast` here: `lib/api.ts`
// is a leaf module and the shadcn `<Toaster />` isn't mounted in the
// root layout. Keeping the bus callback-based means any surface (shadcn
// Toaster, a future FUI ApiErrorToastCenter, a Cypress test spy) can
// subscribe without pulling React into this file.

export type ApiErrorKind =
  | "bad_request"          // 400
  | "unauthorized"         // 401
  | "forbidden"            // 403
  | "not_found"            // 404
  | "conflict"             // 409 — Q.7 #301 optimistic-lock version conflict
  | "validation"           // 422
  | "rate_limited"         // 429
  | "precondition_required" // 428 — If-Match header missing
  | "bootstrap_required"   // 503 + {error: "bootstrap_required"}
  | "server_error"         // 500
  | "bad_gateway"          // 502
  | "service_unavailable"  // 503 (non-bootstrap)
  | "timeout"              // AbortError
  | "offline"              // TypeError from fetch (DNS / no network)
  | "unknown"

/**
 * Typed error raised by `request()` on every non-OK response. Callers
 * can `instanceof ApiError` and branch on `.kind` / `.status` /
 * `.traceId` without parsing string messages.
 */
export class ApiError extends Error {
  kind: ApiErrorKind
  status: number
  body: string
  parsed: Record<string, unknown> | null
  traceId: string | null
  path: string
  method: string

  constructor(args: {
    kind: ApiErrorKind
    status: number
    body: string
    parsed: Record<string, unknown> | null
    traceId: string | null
    path: string
    method: string
    message?: string
  }) {
    super(args.message ?? `API ${args.status}: ${args.body}`)
    this.name = "ApiError"
    this.kind = args.kind
    this.status = args.status
    this.body = args.body
    this.parsed = args.parsed
    this.traceId = args.traceId
    this.path = args.path
    this.method = args.method
  }
}

type ApiErrorListener = (err: ApiError) => void
const _apiErrorListeners = new Set<ApiErrorListener>()

/**
 * Subscribe to terminal API errors from every call through `request()`.
 * The FUI toast layer mounts one of these in the root layout; tests
 * also use it to assert on classification without stubbing fetch-level
 * internals. Returns an unsubscribe.
 */
export function onApiError(listener: ApiErrorListener): () => void {
  _apiErrorListeners.add(listener)
  return () => { _apiErrorListeners.delete(listener) }
}

function _emitApiError(err: ApiError): void {
  for (const l of Array.from(_apiErrorListeners)) {
    try { l(err) } catch (e) { console.warn("[onApiError]", e) }
  }
}

function _parseJsonSafe(body: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(body)
    return v && typeof v === "object" ? v as Record<string, unknown> : null
  } catch { return null }
}

/**
 * Q.1 UI follow-up (2026-04-24): parse the structured 401 detail that
 * ``backend/auth.py::current_user`` attaches when it detects a recent
 * peer-session revocation. Returns ``{ reason, trigger, message }``
 * when the body matches ``{"detail": {"reason": "user_security_event",
 * "trigger": "password_change" | ..., "message": "..."}}``. Returns
 * ``null`` for every other 401 shape (plain strings, bearer-token
 * failures, unrelated validation errors) so the caller falls through
 * to the generic /login redirect.
 */
function _extractSessionRevocation(
  parsed: Record<string, unknown> | null,
): { reason: string; trigger: string; message: string } | null {
  if (!parsed) return null
  const detail = parsed.detail
  if (!detail || typeof detail !== "object") return null
  const d = detail as Record<string, unknown>
  const reason = typeof d.reason === "string" ? d.reason : ""
  if (reason !== "user_security_event") return null
  return {
    reason,
    trigger: typeof d.trigger === "string" ? d.trigger : "",
    message: typeof d.message === "string" ? d.message : "",
  }
}

function _extractTraceId(
  res: Response | null,
  parsed: Record<string, unknown> | null,
): string | null {
  const fromHeader = res?.headers.get("X-Trace-Id")
    || res?.headers.get("X-Request-Id")
    || null
  if (fromHeader) return fromHeader
  if (!parsed) return null
  const raw = parsed.trace_id ?? parsed.traceId ?? parsed.request_id
  return typeof raw === "string" && raw.length > 0 ? raw : null
}

function _classifyStatus(
  status: number,
  parsed: Record<string, unknown> | null,
  isOffline: boolean,
  isTimeout: boolean,
): ApiErrorKind {
  if (isOffline) return "offline"
  if (isTimeout) return "timeout"
  if (status === 400) return "bad_request"
  if (status === 401) return "unauthorized"
  if (status === 403) return "forbidden"
  if (status === 404) return "not_found"
  if (status === 409) return "conflict"
  if (status === 422) return "validation"
  if (status === 428) return "precondition_required"
  if (status === 429) return "rate_limited"
  if (status === 500) return "server_error"
  if (status === 502) return "bad_gateway"
  if (status === 503) {
    return parsed?.error === "bootstrap_required"
      ? "bootstrap_required"
      : "service_unavailable"
  }
  return "unknown"
}

/**
 * Build the terminal `ApiError`, emit it to listeners, and trigger the
 * redirects that must happen before the error propagates:
 *
 *   - 401 → `/login?next=<current>` (skip if already on /login*)
 *
 * The 503 bootstrap redirect is handled inside `request()` because it
 * must short-circuit the retry loop and swallow the promise resolution.
 */
function _handleTerminalError(args: {
  status: number
  body: string
  parsed: Record<string, unknown> | null
  res: Response | null
  path: string
  method: string
  isOffline: boolean
  isTimeout: boolean
  skipGlobalHandler: boolean
}): ApiError {
  const kind = _classifyStatus(args.status, args.parsed, args.isOffline, args.isTimeout)
  const traceId = _extractTraceId(args.res, args.parsed)
  const err = new ApiError({
    kind,
    status: args.status,
    body: args.body,
    parsed: args.parsed,
    traceId,
    path: args.path,
    method: args.method,
    message: args.isTimeout
      ? `Request timeout: ${args.path}`
      : args.isOffline
        ? `Network offline: ${args.path}`
        : `API ${args.status}: ${args.body}`,
  })

  if (args.skipGlobalHandler) return err

  // B13 Part C (#339): 401 → redirect to /login?next=<current> WITHOUT
  // firing the onApiError bus, so the FUI toast layer stays silent
  // during the unload (the toast would otherwise race the navigation
  // and flash). Skip the redirect (and fall through to the emit) if
  // we're already on the login page — the form itself surfaces the
  // auth failure — or on /setup-required, where the operator hasn't
  // logged in yet and we don't want to punt them to /login mid-boot.
  //
  // Q.1 UI follow-up (2026-04-24): when the 401 body carries a
  // ``detail.reason`` of ``"user_security_event"`` (peer-session
  // rotation after password change / MFA change / admin role change
  // / account disable), append ``&reason=<reason>&trigger=<trigger>``
  // to the /login URL so the login page renders a tailored banner
  // ("your password was changed on another device — please sign in
  // again") instead of a bare login form. The body's ``detail.message``
  // is also forwarded so the backend is the single source of truth
  // for the copy.
  if (kind === "unauthorized" && typeof window !== "undefined") {
    const here = window.location.pathname
    const skipRedirect =
      here.startsWith("/login")
      || here === "/setup-required"
    if (!skipRedirect) {
      const next = encodeURIComponent(
        window.location.pathname + window.location.search,
      )
      const rev = _extractSessionRevocation(args.parsed)
      const extra = rev
        ? `&reason=${encodeURIComponent(rev.reason)}`
          + `&trigger=${encodeURIComponent(rev.trigger)}`
          + (rev.message
              ? `&message=${encodeURIComponent(rev.message)}`
              : "")
        : ""
      window.location.assign(`/login?next=${next}${extra}`)
      // Short-circuit: page is unloading, no toast, no listeners.
      return err
    }
  }

  _emitApiError(err)
  return err
}

interface RequestOptions {
  /**
   * When true, suppresses the redirect side effect AND emits nothing on
   * the `onApiError` bus. Use on auth endpoints (`/auth/login`) and any
   * call where the caller wants full control over the UX — the typed
   * `ApiError` is still thrown so the caller can branch on `.kind`.
   */
  skipGlobalErrorHandler?: boolean
}

async function request<T>(
  path: string,
  init?: RequestInit,
  options?: RequestOptions,
): Promise<T> {
  const skipGlobalHandler = options?.skipGlobalErrorHandler ?? false
  const methodUpper = (init?.method || "GET").toUpperCase()
  let lastError: Error | null = null
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT)
    try {
      // Phase 54 / Internet-auth: send the session cookie with every
      // call so the backend's auth_mode=session/strict can recognise
      // the operator. CSRF token is read from the non-HttpOnly cookie
      // and echoed via X-CSRF-Token for state-changing methods.
      const baseHeaders: Record<string, string> = {
        "Content-Type": "application/json",
      }
      if (_currentTenantId) {
        baseHeaders["X-Tenant-Id"] = _currentTenantId
      }
      // Y5 (#281) row 4 — paired with X-Tenant-Id for project-scoped
      // requests. Backend ``_project_header_gate`` double-verifies the
      // caller has membership; the SQLAlchemy listener (Y5 row 3) then
      // injects ``WHERE project_id = :p OR project_id IS NULL`` on
      // every SELECT.
      if (_currentProjectId) {
        baseHeaders["X-Project-Id"] = _currentProjectId
      }
      if (typeof document !== "undefined"
          && !["GET", "HEAD", "OPTIONS"].includes(methodUpper)) {
        const csrf = readCookie("omnisight_csrf")
        if (csrf) baseHeaders["X-CSRF-Token"] = csrf
      }
      const res = await fetch(`${API_V1}${path}`, {
        signal: controller.signal,
        credentials: "include",
        headers: { ...baseHeaders, ...init?.headers },
        ...init,
      })
      clearTimeout(timer)
      if (!res.ok) {
        const body = await res.text().catch(() => "")
        const isIdempotent = ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"].includes(methodUpper)
        const parsed = _parseJsonSafe(body)

        // B13 Part A (#339): first-run bootstrap_required 503 → redirect
        // to the FUI /setup-required landing page instead of surfacing a
        // raw error toast. Short-circuits retry, swallows the promise
        // resolution, and bails out if we're already on /setup-required
        // so that page can render its diagnostic panel from the live 503.
        if (res.status === 503 && parsed?.error === "bootstrap_required") {
          if (!skipGlobalHandler
              && typeof window !== "undefined"
              && window.location.pathname !== "/setup-required") {
            window.location.assign("/setup-required")
            // Never resolves — the current page is unloading.
            return new Promise<T>(() => { /* unloading */ })
          }
          throw _handleTerminalError({
            status: 503, body, parsed, res,
            path, method: methodUpper,
            isOffline: false, isTimeout: false, skipGlobalHandler,
          })
        }

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
        throw _handleTerminalError({
          status: res.status, body, parsed, res,
          path, method: methodUpper,
          isOffline: false, isTimeout: false, skipGlobalHandler,
        })
      }
      if (res.status === 204) return undefined as T
      return res.json()
    } catch (e) {
      clearTimeout(timer)
      // Already a terminal ApiError — propagate unchanged (don't re-emit).
      if (e instanceof ApiError) {
        throw e
      }
      const isTimeout = e instanceof DOMException && e.name === "AbortError"
      const isOffline = e instanceof TypeError
      if (isTimeout || isOffline) {
        const isIdempotent = ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"].includes(methodUpper)
        lastError = new Error(
          isTimeout ? `Request timeout: ${path}` : `Network offline: ${path}`,
        )
        if (isIdempotent && attempt < MAX_RETRIES) {
          await new Promise(r => setTimeout(r, 1000 * (attempt + 1)))
          continue
        }
        // Terminal network failure → emit ApiError so the FUI toast
        // layer can show「網路連線中斷，嘗試重新連線...」without the
        // caller having to pattern-match on string messages.
        throw _handleTerminalError({
          status: 0, body: "", parsed: null, res: null,
          path, method: methodUpper,
          isOffline, isTimeout, skipGlobalHandler,
        })
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

export interface ReadyzCheck {
  ok: boolean
  detail: string
}

export interface ReadyzResponse {
  status: "ready" | "not_ready"
  ready: boolean
  checks: Record<string, ReadyzCheck>
  timestamp: number
}

/** /readyz — deep liveness used by the setup-required telemetry ticker. */
export async function getReadyz() {
  return request<ReadyzResponse>("/readyz")
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

// M3 — Per-tenant per-provider per-key circuit breaker
export interface CircuitBreakerEntry {
  tenant_id: string
  provider: string
  fingerprint: string
  open: boolean
  cooldown_remaining: number
  failure_count: number
  last_failure: number | null
  opened_at: number | null
  closed_at: number | null
  reason: string | null
}

export interface CircuitBreakerResponse {
  tenant_id: string
  scope: "tenant" | "all"
  cooldown_seconds: number
  circuits: CircuitBreakerEntry[]
}

export async function getCircuitBreakers(scope: "tenant" | "all" = "tenant"): Promise<CircuitBreakerResponse> {
  return request<CircuitBreakerResponse>(`/providers/circuits?scope=${scope}`)
}

export async function resetCircuitBreaker(opts: { provider?: string; fingerprint?: string; scope?: "tenant" | "all" } = {}): Promise<{ status: string; cleared: number; tenant_id: string; scope: string }> {
  return request<{ status: string; cleared: number; tenant_id: string; scope: string }>("/providers/circuits/reset", {
    method: "POST",
    body: JSON.stringify({
      provider: opts.provider ?? null,
      fingerprint: opts.fingerprint ?? null,
      scope: opts.scope ?? "tenant",
    }),
  })
}

// M4 — Per-tenant host metrics
export interface TenantUsage {
  tenant_id: string
  cpu_percent: number
  mem_used_gb: number
  disk_used_gb: number
  sandbox_count: number
}

export interface HostMetricsListResponse { tenants: TenantUsage[] }
export interface HostMetricsSingleResponse { tenant: TenantUsage }

export async function getHostMetricsForTenant(tenantId: string): Promise<HostMetricsSingleResponse> {
  return request<HostMetricsSingleResponse>(`/host/metrics?tenant_id=${encodeURIComponent(tenantId)}`)
}

export async function getMyHostMetrics(): Promise<HostMetricsSingleResponse> {
  return request<HostMetricsSingleResponse>("/host/metrics/me")
}

export async function getAllHostMetrics(): Promise<HostMetricsListResponse> {
  return request<HostMetricsListResponse>("/host/metrics")
}

export interface TenantAccountingRow {
  tenant_id: string
  cpu_seconds_total: number
  mem_gb_seconds_total: number
  last_updated: number
}

export async function getHostAccounting(): Promise<{ tenants: TenantAccountingRow[] }> {
  return request<{ tenants: TenantAccountingRow[] }>("/host/accounting")
}

// M6 — Per-tenant egress allow-list
export interface TenantEgressPolicy {
  tenant_id: string
  allowed_hosts: string[]
  allowed_cidrs: string[]
  default_action: "deny" | "allow"
  updated_at: string | null
  updated_by: string
}

export interface TenantEgressRequest {
  id: string
  tenant_id: string
  requested_by: string
  kind: "host" | "cidr"
  value: string
  justification: string
  status: "pending" | "approved" | "rejected"
  decided_by: string | null
  decided_at: string | null
  decision_note: string
  created_at: string
}

export async function getMyEgressPolicy(): Promise<{ policy: TenantEgressPolicy }> {
  return request<{ policy: TenantEgressPolicy }>("/tenants/me/egress")
}

export async function listEgressPolicies(): Promise<{ policies: TenantEgressPolicy[] }> {
  return request<{ policies: TenantEgressPolicy[] }>("/tenants/egress")
}

export async function getEgressPolicy(tenantId: string): Promise<{ policy: TenantEgressPolicy }> {
  return request<{ policy: TenantEgressPolicy }>(`/tenants/${encodeURIComponent(tenantId)}/egress`)
}

export async function putEgressPolicy(
  tenantId: string,
  body: Partial<Pick<TenantEgressPolicy, "allowed_hosts" | "allowed_cidrs" | "default_action">>,
): Promise<{ policy: TenantEgressPolicy }> {
  return request<{ policy: TenantEgressPolicy }>(`/tenants/${encodeURIComponent(tenantId)}/egress`, {
    method: "PUT",
    body: JSON.stringify(body),
  })
}

export async function submitEgressRequest(body: {
  kind: "host" | "cidr"
  value: string
  justification?: string
}): Promise<{ request: TenantEgressRequest }> {
  return request<{ request: TenantEgressRequest }>("/tenants/me/egress/requests", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function listMyEgressRequests(
  status?: "pending" | "approved" | "rejected",
): Promise<{ requests: TenantEgressRequest[] }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ""
  return request<{ requests: TenantEgressRequest[] }>(`/tenants/me/egress/requests${qs}`)
}

export async function listAllEgressRequests(opts?: {
  tenant_id?: string
  status?: "pending" | "approved" | "rejected"
}): Promise<{ requests: TenantEgressRequest[] }> {
  const params = new URLSearchParams()
  if (opts?.tenant_id) params.set("tenant_id", opts.tenant_id)
  if (opts?.status) params.set("status", opts.status)
  const qs = params.toString()
  return request<{ requests: TenantEgressRequest[] }>(
    `/tenants/egress/requests${qs ? `?${qs}` : ""}`,
  )
}

export async function approveEgressRequest(
  rid: string,
  note?: string,
): Promise<{ request: TenantEgressRequest; policy: TenantEgressPolicy }> {
  return request(`/tenants/egress/requests/${encodeURIComponent(rid)}/approve`, {
    method: "POST",
    body: JSON.stringify({ note: note ?? "" }),
  })
}

export async function rejectEgressRequest(
  rid: string,
  note?: string,
): Promise<{ request: TenantEgressRequest }> {
  return request(`/tenants/egress/requests/${encodeURIComponent(rid)}/reject`, {
    method: "POST",
    body: JSON.stringify({ note: note ?? "" }),
  })
}

export async function resetEgressDnsCache(
  tenantId: string,
): Promise<{ tenant_id: string; resolved: Record<string, string[]> }> {
  return request(`/tenants/${encodeURIComponent(tenantId)}/egress/dns-cache/reset`, {
    method: "POST",
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
  return request<SystemInfo>("/runtime/info")
}

export async function getDevices() {
  return request<SystemDevice[]>("/runtime/devices")
}

export async function getSystemStatus() {
  return request<SystemStatus>("/runtime/status")
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
  return request<SpecValue[]>("/runtime/spec")
}

export async function updateSpec(path: string[], value: string | number | boolean) {
  return request<{ status: string }>("/runtime/spec", {
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
  return request<RepoInfo[]>("/runtime/repos")
}

// ─── Logs ───

export interface LogEntry {
  timestamp: string
  message: string
  level: "info" | "warn" | "error"
}

export async function getLogs(limit: number = 50) {
  return request<LogEntry[]>(`/runtime/logs?limit=${limit}`)
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
  // ZZ.A1 (#303-2, 2026-04-24): prompt-cache observability fields.
  // ``null`` on pre-ZZ rows (legacy payloads before the cache columns
  // existed) — distinguishes "no data" from "genuine zero hits". The
  // UI maps null → "—" so operators see the gap explicitly.
  cache_read_tokens: number | null
  cache_create_tokens: number | null
  cache_hit_ratio: number | null
}

export async function getTokenUsage() {
  return request<TokenUsage[]>("/runtime/tokens")
}

/**
 * ZZ.B3 #304-3 checkbox 1 shape: single 60 s bucket of aggregated
 * ``turn.complete`` events. Backend docstring in
 * ``backend/routers/system.py::get_token_burn_rate`` owns the source-of-
 * truth description. ``timestamp`` is ISO-8601 UTC (``…Z`` suffix);
 * ``cost_per_hour`` is already projected from the 60 s bucket so the
 * sparkline consumer just plots the series as-is.
 */
export interface TokenBurnRatePoint {
  timestamp: string
  tokens_per_hour: number
  cost_per_hour: number
}

export interface TokenBurnRateResponse {
  window: "15m" | "1h" | "24h"
  bucket_seconds: number
  points: TokenBurnRatePoint[]
}

export type TokenBurnRateWindow = "15m" | "1h" | "24h"

/**
 * ZZ.B3 #304-3 checkbox 2: fetch the 60 s-bucketed burn-rate series for
 * the TokenUsageStats header sparkline. Window whitelist is enforced
 * server-side (400 on anything outside ``15m|1h|24h``).
 */
export async function fetchTokenBurnRate(
  window: TokenBurnRateWindow = "1h",
): Promise<TokenBurnRateResponse> {
  return request<TokenBurnRateResponse>(
    `/runtime/tokens/burn-rate?window=${encodeURIComponent(window)}`,
  )
}

/**
 * ZZ.C2 (#305-2, 2026-04-24) checkbox 1: one sparse cell in the
 * session-heatmap matrix. Mirrors ``backend/models.py::TokenHeatmapCell``
 * — ``day`` is a UTC ``YYYY-MM-DD`` string, ``hour`` is 0–23 in UTC; the
 * frontend grid shifts both into the browser's local timezone at render
 * time (checkbox 5 of the TODO spec locks that contract).
 */
export interface TokenHeatmapCell {
  day: string
  hour: number
  token_total: number
  cost_total: number
}

export interface TokenHeatmapResponse {
  window: TokenHeatmapWindow
  cells: TokenHeatmapCell[]
  /** ZZ.C2 #305-2 checkbox 4 (2026-04-24): distinct ``model`` slugs
   *  observed across the unfiltered window so the per-model dropdown
   *  can render every choice without a second round-trip. Backend
   *  populates this regardless of whether a filter was applied. */
  available_models?: string[]
  /** Echoes the applied filter (``null`` when "All models" is
   *  selected). Lets the frontend reconcile dropdown state after a
   *  remount or URL-driven load. */
  model?: string | null
}

export type TokenHeatmapWindow = "7d" | "30d"

/**
 * ZZ.C2 #305-2 checkbox 2: fetch the ``(day, hour)`` token + cost matrix
 * that feeds the calendar-style heatmap under ``<TokenUsageStats>``.
 * ``window`` whitelist is enforced server-side (400 on anything outside
 * ``7d|30d``).
 *
 * ZZ.C2 #305-2 checkbox 4 (2026-04-24): optional ``model`` slug
 * restricts the aggregate to one model (e.g. ``claude-opus-4-7``).
 * ``null`` / ``undefined`` / empty string means "all models" — the
 * param is omitted from the URL entirely so backend stays backward
 * compatible with callers that don't know about the filter.
 */
export async function fetchTokenHeatmap(
  window: TokenHeatmapWindow = "7d",
  model?: string | null,
): Promise<TokenHeatmapResponse> {
  const params = new URLSearchParams({ window })
  if (model != null && model !== "") {
    params.set("model", model)
  }
  return request<TokenHeatmapResponse>(
    `/runtime/tokens/heatmap?${params.toString()}`,
  )
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
  return request<CompressionStats>("/runtime/compression")
}

/**
 * ZZ.C1 #305-1 checkbox 3 — prompt-version timeline + diff endpoints.
 *
 * Mirrors the Pydantic envelopes in ``backend/models.py``
 * (``PromptVersionEntry`` / ``PromptVersionsListResponse`` /
 * ``PromptDiffResponse``). The backend is the source of truth for the
 * field contract; this declaration just keeps the drawer's TS strict.
 */
export interface PromptVersionEntry {
  id: number
  agent_type: string
  content_hash: string
  content: string
  content_preview: string
  created_at: string
  supersedes_id: number | null
  version: number
  role: string
}

export interface PromptVersionsListResponse {
  agent_type: string
  path: string
  limit: number
  versions: PromptVersionEntry[]
}

export interface PromptDiffResponse {
  from_id: number
  to_id: number
  agent_type: string
  from_hash: string
  to_hash: string
  from_version: number
  to_version: number
  from_created_at: string
  to_created_at: string
  diff: string
}

export async function fetchPromptVersions(
  agentType: string,
  limit = 20,
): Promise<PromptVersionsListResponse> {
  const qs = new URLSearchParams({
    agent_type: agentType,
    limit: String(limit),
  })
  return request<PromptVersionsListResponse>(`/runtime/prompts?${qs.toString()}`)
}

export async function fetchPromptDiff(
  fromId: number,
  toId: number,
): Promise<PromptDiffResponse> {
  const qs = new URLSearchParams({
    from: String(fromId),
    to: String(toId),
  })
  return request<PromptDiffResponse>(`/runtime/prompts/diff?${qs.toString()}`)
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
  return request<SimulationItem[]>(`/runtime/simulations?${qs.toString()}`)
}

export async function getSimulation(simId: string): Promise<SimulationItem> {
  return request<SimulationItem>(`/runtime/simulations/${simId}`)
}

export async function triggerSimulation(body: { track: string; module: string; input_data?: string; mock?: boolean; platform?: string }): Promise<{ result: string }> {
  return request<{ result: string }>("/runtime/simulations", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

// ─── Dashboard aggregator (Phase 4-2) ───
//
// Server-side fan-out that collapses the 11 `/runtime/*` calls useEngine
// used to make every 5 s into a single composite response. Each subkey
// carries its own `{ok, data|error}` envelope so one failing sub-query
// does not take down the rest — matches the old `Promise.allSettled`
// semantics, pushed to the server. See `backend/routers/dashboard.py`.

export type DashboardSubResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string }

export interface DashboardSummary {
  systemStatus: DashboardSubResult<SystemStatus>
  systemInfo: DashboardSubResult<SystemInfo>
  devices: DashboardSubResult<SystemDevice[]>
  spec: DashboardSubResult<SpecValue[]>
  repos: DashboardSubResult<RepoInfo[]>
  logs: DashboardSubResult<LogEntry[]>
  tokenUsage: DashboardSubResult<TokenUsage[]>
  tokenBudget: DashboardSubResult<TokenBudgetInfo>
  notificationsUnread: DashboardSubResult<{ count: number }>
  compression: DashboardSubResult<CompressionStats>
  simulations: DashboardSubResult<SimulationItem[]>
}

export async function getDashboardSummary() {
  return request<DashboardSummary>("/dashboard/summary")
}

// ─── LLM provider balance (Z.2 #291 + Z.4 #293 checkbox 5) ───
//
// Mirrors the three-envelope shape emitted by
// ``backend/routers/llm_balance.py::resolve_balance`` — ``ok`` (cached
// or freshly fetched), ``unsupported`` (vendor has no public balance
// API with API-key auth), or ``error`` (auth failure / 5xx / missing
// key). The dashboard's 60 s poll in ``useEngine`` hits the batch
// ``/runtime/providers/balance`` route so the nine-provider panel
// lands in one round-trip.

export type ProviderBalanceStatus = "ok" | "unsupported" | "error"

export interface ProviderBalanceEnvelope {
  status: ProviderBalanceStatus
  provider: string
  // ok envelope
  currency?: string
  balance_remaining?: number | null
  granted_total?: number | null
  usage_total?: number | null
  last_refreshed_at?: number | null
  source?: "cache" | "live"
  raw?: Record<string, unknown>
  stale_since?: number | null
  // unsupported envelope
  reason?: string
  // error envelope
  message?: string
}

export interface ProviderBalanceBatch {
  providers: ProviderBalanceEnvelope[]
}

export async function getProvidersBalance(): Promise<ProviderBalanceBatch> {
  return request<ProviderBalanceBatch>("/runtime/providers/balance")
}

// ─── Integration Settings ───

export async function getSettings(): Promise<Record<string, Record<string, unknown>>> {
  return request<Record<string, Record<string, unknown>>>("/runtime/settings")
}

export async function updateSettings(updates: Record<string, string | number | boolean>): Promise<{ status: string; applied: string[]; rejected: Record<string, string> }> {
  return request<{ status: string; applied: string[]; rejected: Record<string, string> }>("/runtime/settings", {
    method: "PUT",
    body: JSON.stringify({ updates }),
  })
}

export async function testIntegration(type: string): Promise<{ status: string; message?: string; [key: string]: unknown }> {
  return request<{ status: string; message?: string }>(`/runtime/test/${type}`, { method: "POST" })
}

// ─── B14 Part A row 3+: Probe a candidate Git-forge credential ───
//
// Validates a credential supplied by the operator (e.g. in the Bootstrap
// Step 3.5 Git Forge form) without mutating `settings.*_token`. Shape
// varies by provider:
//   - GitHub: { token }                     → REST `/user`
//   - GitLab: { token, url? }               → REST `/api/v4/version`
//   - Gerrit: { ssh_host, ssh_port, url? }  → SSH `gerrit version`
export interface GitForgeTokenTestResult {
  status: "ok" | "error"
  user?: string
  name?: string
  scopes?: string
  // GitLab / Gerrit — resolved instance version.
  version?: string
  revision?: string
  url?: string
  // Gerrit — echoes the probed SSH endpoint so the caller can persist
  // the exact host/port that validated.
  ssh_host?: string
  ssh_port?: number
  message?: string
  // P0.1 (audit 2026-04-27): index signature for assignability with
  // `TestResult` interface in `components/omnisight/integration-settings.tsx`
  // (line 16) which carries `[key: string]: unknown`. Without this, passing
  // `GitForgeTokenTestResult` to a `TestResult`-shaped onTest handler raises
  // TS2322 — was masked by `next.config.mjs::typescript.ignoreBuildErrors`
  // for months. Audit's P0.1 flips that flag, so this drift now blocks build.
  // Forward-compatible: caller can attach extra debug fields without breaking
  // the type contract.
  [key: string]: unknown
}

export async function testGitForgeToken(args: {
  provider: "github" | "gitlab" | "gerrit"
  token?: string
  url?: string
  ssh_host?: string
  ssh_port?: number
}): Promise<GitForgeTokenTestResult> {
  const body: Record<string, string | number> = {
    provider: args.provider,
    token: args.token ?? "",
    url: args.url ?? "",
  }
  if (args.provider === "gerrit") {
    body.ssh_host = args.ssh_host ?? ""
    body.ssh_port = args.ssh_port ?? 29418
  }
  return request<GitForgeTokenTestResult>("/runtime/git-forge/test-token", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

// ─── B14 Part C row 223: Fetch the OmniSight SSH public key ───
//
// Gerrit Setup Wizard Step 2 shows the public key the operator must paste
// into `Gerrit Settings → SSH Keys`. The backend derives the `.pub` path
// from `settings.git_ssh_key_path` and returns the raw key line plus a
// SHA256 fingerprint for cross-checking. The private key never leaves
// the host — only the public half is surfaced.
export interface GitForgeSshPubkey {
  status: "ok" | "error"
  public_key?: string
  fingerprint?: string
  key_path?: string
  key_type?: string
  comment?: string
  message?: string
}

export async function getGitForgeSshPubkey(): Promise<GitForgeSshPubkey> {
  return request<GitForgeSshPubkey>("/runtime/git-forge/ssh-pubkey")
}

// ─── Phase 5-9 (#multi-account-forge) — git_accounts CRUD client ───
//
// Wraps the `/git-accounts` REST surface (backend/routers/git_accounts.py).
// Plaintext token / ssh_key / webhook_secret never round-trip through these
// reads — only the masked `…last4` fingerprints land in the response. Mutations
// accept plaintext and the backend encrypts before persisting.
export type GitAccountPlatform = "github" | "gitlab" | "gerrit" | "jira"
export type GitAccountAuthType = "pat" | "oauth" | "ssh"

export interface GitAccount {
  id: string
  tenant_id: string
  platform: GitAccountPlatform
  instance_url: string
  label: string
  username: string
  // Masked fingerprints — `""` when the corresponding secret is unset.
  token_fingerprint: string
  ssh_key_fingerprint: string
  webhook_secret_fingerprint: string
  ssh_host: string
  ssh_port: number
  project: string
  url_patterns: string[]
  auth_type: GitAccountAuthType
  is_default: boolean
  enabled: boolean
  metadata: Record<string, unknown>
  last_used_at: string | null
  created_at: string | null
  updated_at: string | null
  version: number
}

export interface GitAccountCreate {
  platform: GitAccountPlatform
  instance_url?: string
  label?: string
  username?: string
  token?: string
  ssh_key?: string
  ssh_host?: string
  ssh_port?: number
  project?: string
  webhook_secret?: string
  url_patterns?: string[]
  auth_type?: GitAccountAuthType
  is_default?: boolean
  enabled?: boolean
  metadata?: Record<string, unknown>
}

// PATCH body — all optional; for secret rotation, omit (or `undefined`) means
// "leave it alone" and explicit empty string `""` means "clear it".
export interface GitAccountUpdate {
  label?: string
  username?: string
  instance_url?: string
  ssh_host?: string
  ssh_port?: number
  project?: string
  url_patterns?: string[]
  auth_type?: GitAccountAuthType
  is_default?: boolean
  enabled?: boolean
  metadata?: Record<string, unknown>
  token?: string
  ssh_key?: string
  webhook_secret?: string
}

export interface GitAccountListResponse {
  items: GitAccount[]
  count: number
}

export interface GitAccountTestResult {
  account_id: string
  platform: GitAccountPlatform
  status: "ok" | "error" | "not_configured"
  message?: string
  user?: string
  username?: string
  email?: string
  displayName?: string
  accountId?: string
  version?: string
  ssh_host?: string
  ssh_port?: number
  [key: string]: unknown
}

export interface GitAccountResolveResponse {
  url: string
  matched: boolean
  matched_via: "url_pattern" | "exact_host" | "platform_default" | "fallback" | null
  account: GitAccount | null
}

export async function listGitAccounts(opts?: {
  platform?: GitAccountPlatform
  enabled_only?: boolean
}): Promise<GitAccountListResponse> {
  const qs = new URLSearchParams()
  if (opts?.platform) qs.set("platform", opts.platform)
  if (opts?.enabled_only) qs.set("enabled_only", "true")
  const suffix = qs.toString() ? `?${qs.toString()}` : ""
  return request<GitAccountListResponse>(`/git-accounts${suffix}`)
}

export async function getGitAccount(id: string): Promise<GitAccount> {
  return request<GitAccount>(`/git-accounts/${encodeURIComponent(id)}`)
}

export async function createGitAccount(body: GitAccountCreate): Promise<GitAccount> {
  return request<GitAccount>("/git-accounts", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function updateGitAccount(
  id: string,
  body: GitAccountUpdate,
): Promise<GitAccount> {
  return request<GitAccount>(`/git-accounts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
}

export async function deleteGitAccount(
  id: string,
  opts?: { auto_elect_new_default?: boolean },
): Promise<{ status: string; [key: string]: unknown }> {
  // Default to true to match backend; pass `false` to refuse-without-replacement.
  const elect = opts?.auto_elect_new_default ?? true
  const qs = `?auto_elect_new_default=${elect ? "true" : "false"}`
  return request<{ status: string; [key: string]: unknown }>(
    `/git-accounts/${encodeURIComponent(id)}${qs}`,
    { method: "DELETE" },
  )
}

export async function testGitAccountById(id: string): Promise<GitAccountTestResult> {
  return request<GitAccountTestResult>(
    `/git-accounts/${encodeURIComponent(id)}/test`,
    { method: "POST" },
  )
}

export async function resolveGitAccountForUrl(url: string): Promise<GitAccountResolveResponse> {
  const qs = `?url=${encodeURIComponent(url)}`
  return request<GitAccountResolveResponse>(`/git-accounts/resolve${qs}`, {
    method: "POST",
  })
}

// ─── Phase 5b-4 (#llm-credentials) — llm_credentials CRUD client ───
//
// Wraps the `/llm-credentials` REST surface (backend/routers/llm_credentials.py).
// Plaintext API keys never round-trip through these reads — only the masked
// `…last4` fingerprint (`value_fingerprint`) lands in the response. Mutations
// accept plaintext and the backend Fernet-encrypts before persisting.
//
// Provider set matches the backend `_PROVIDER_PATTERN`:
//   anthropic | google | openai | xai | groq | deepseek | together |
//   openrouter | ollama
// Ollama is keyless — it uses `metadata.base_url` instead of `value`.
export type LLMCredentialProvider =
  | "anthropic"
  | "google"
  | "openai"
  | "xai"
  | "groq"
  | "deepseek"
  | "together"
  | "openrouter"
  | "ollama"
export type LLMCredentialAuthType = "pat" | "oauth"

export interface LLMCredential {
  id: string
  tenant_id: string
  provider: LLMCredentialProvider
  label: string
  // Masked fingerprint — `""` when the value is unset (e.g. Ollama).
  value_fingerprint: string
  auth_type: LLMCredentialAuthType
  is_default: boolean
  enabled: boolean
  metadata: Record<string, unknown>
  last_used_at: string | null
  created_at: string | null
  updated_at: string | null
  version: number
}

export interface LLMCredentialCreate {
  provider: LLMCredentialProvider
  label?: string
  value?: string
  auth_type?: LLMCredentialAuthType
  is_default?: boolean
  enabled?: boolean
  metadata?: Record<string, unknown>
}

// PATCH body — all optional; for key rotation, omit (or leave `undefined`)
// means "leave it alone" and explicit empty string `""` means "clear it".
export interface LLMCredentialUpdate {
  label?: string
  auth_type?: LLMCredentialAuthType
  is_default?: boolean
  enabled?: boolean
  metadata?: Record<string, unknown>
  value?: string
}

export interface LLMCredentialListResponse {
  items: LLMCredential[]
  count: number
}

export interface LLMCredentialTestResult {
  credential_id: string
  provider: LLMCredentialProvider
  status: "ok" | "error"
  model_count?: number
  http_status?: number
  base_url?: string
  message?: string
  [key: string]: unknown
}

export async function listLlmCredentials(opts?: {
  provider?: LLMCredentialProvider
  enabled_only?: boolean
}): Promise<LLMCredentialListResponse> {
  const qs = new URLSearchParams()
  if (opts?.provider) qs.set("provider", opts.provider)
  if (opts?.enabled_only) qs.set("enabled_only", "true")
  const suffix = qs.toString() ? `?${qs.toString()}` : ""
  return request<LLMCredentialListResponse>(`/llm-credentials${suffix}`)
}

export async function getLlmCredential(id: string): Promise<LLMCredential> {
  return request<LLMCredential>(`/llm-credentials/${encodeURIComponent(id)}`)
}

export async function createLlmCredential(body: LLMCredentialCreate): Promise<LLMCredential> {
  return request<LLMCredential>("/llm-credentials", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function updateLlmCredential(
  id: string,
  body: LLMCredentialUpdate,
): Promise<LLMCredential> {
  return request<LLMCredential>(`/llm-credentials/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
}

export async function deleteLlmCredential(
  id: string,
  opts?: { auto_elect_new_default?: boolean },
): Promise<{ status: string; [key: string]: unknown }> {
  // Default to true to match backend; pass `false` to refuse-without-replacement.
  const elect = opts?.auto_elect_new_default ?? true
  const qs = `?auto_elect_new_default=${elect ? "true" : "false"}`
  return request<{ status: string; [key: string]: unknown }>(
    `/llm-credentials/${encodeURIComponent(id)}${qs}`,
    { method: "DELETE" },
  )
}

export async function testLlmCredentialById(id: string): Promise<LLMCredentialTestResult> {
  return request<LLMCredentialTestResult>(
    `/llm-credentials/${encodeURIComponent(id)}/test`,
    { method: "POST" },
  )
}

// ─── B14 Part C row 224: Verify the merger-agent-bot Gerrit group ───
//
// Gerrit Setup Wizard Step 3 shows the operator the SSH commands they must
// run against their Gerrit instance to (a) create the `merger-agent-bot`,
// `ai-reviewer-bots`, and `non-ai-reviewer` groups and (b) add the bot
// service account to the first two. After they run the commands, they hit
// "Verify bot account" which calls this helper — the backend runs
// `ssh -p {port} {host} gerrit ls-members merger-agent-bot` and returns
// the member list so the UI can confirm the O7 dual-+2 gate's AI half is
// wired up before moving on to submit-rule validation in Step 4.
export interface GerritBotMember {
  username: string
  full_name?: string
  email?: string
}

export interface GerritBotVerifyResult {
  status: "ok" | "error"
  group?: string
  member_count?: number
  members?: GerritBotMember[]
  ssh_host?: string
  ssh_port?: number
  message?: string
}

export async function verifyGerritMergerBot(args: {
  ssh_host: string
  ssh_port?: number
  group?: string
}): Promise<GerritBotVerifyResult> {
  return request<GerritBotVerifyResult>("/runtime/git-forge/gerrit/verify-bot", {
    method: "POST",
    body: JSON.stringify({
      ssh_host: args.ssh_host,
      ssh_port: args.ssh_port ?? 29418,
      group: args.group ?? "merger-agent-bot",
    }),
  })
}

// ─── B14 Part C row 225: Verify the Gerrit dual-+2 submit-rule ───
//
// Gerrit Setup Wizard Step 4 fetches `refs/meta/config:project.config`
// from the target project and pattern-matches the three ACL fragments
// that encode the O7 dual-+2 policy:
//   (A) label-Code-Review granted to `ai-reviewer-bots`
//   (B) label-Code-Review granted to `non-ai-reviewer`
//   (C) submit restricted to `non-ai-reviewer` (human hard gate)
// Any missing fragment is surfaced per-check so the operator can diff
// against `.gerrit/project.config.example` shipped in the repo.
export interface GerritSubmitRuleCheck {
  id: string
  ok: boolean
  detail?: string
}

export interface GerritSubmitRuleVerifyResult {
  status: "ok" | "error"
  project?: string
  ssh_host?: string
  ssh_port?: number
  checks?: GerritSubmitRuleCheck[]
  missing?: string[]
  message?: string
}

export async function verifyGerritSubmitRule(args: {
  ssh_host: string
  ssh_port?: number
  project: string
}): Promise<GerritSubmitRuleVerifyResult> {
  return request<GerritSubmitRuleVerifyResult>(
    "/runtime/git-forge/gerrit/verify-submit-rule",
    {
      method: "POST",
      body: JSON.stringify({
        ssh_host: args.ssh_host,
        ssh_port: args.ssh_port ?? 29418,
        project: args.project,
      }),
    },
  )
}

// ─── B14 Part C row 226: Gerrit webhook setup (Step 5) ───
//
// Step 5 of the Gerrit Setup Wizard surfaces the inbound webhook URL
// (`<base>/api/v1/webhooks/gerrit`) and the HMAC-SHA256 secret the
// operator must paste into Gerrit's `webhooks.config` (under
// `refs/meta/config`). `getGerritWebhookInfo()` is read-only and only
// returns a masked preview of the configured secret. Use
// `generateGerritWebhookSecret()` to mint + persist a new secret — the
// plain value is returned exactly once in that response and never again,
// so the wizard must keep it in component state for the operator to copy
// before they close the modal.
export interface GerritWebhookInfo {
  status: "ok" | "error"
  webhook_url?: string
  secret_configured?: boolean
  secret_masked?: string
  signature_header?: string
  signature_algorithm?: string
  event_types?: string[]
  message?: string
}

export interface GerritWebhookSecretRotateResult {
  status: "ok" | "error"
  secret?: string  // plain value — surfaced exactly once, never re-readable
  secret_masked?: string
  webhook_url?: string
  signature_header?: string
  signature_algorithm?: string
  note?: string
  message?: string
}

export async function getGerritWebhookInfo(): Promise<GerritWebhookInfo> {
  return request<GerritWebhookInfo>("/runtime/git-forge/gerrit/webhook-info")
}

export async function generateGerritWebhookSecret(): Promise<GerritWebhookSecretRotateResult> {
  return request<GerritWebhookSecretRotateResult>(
    "/runtime/git-forge/gerrit/webhook-secret/generate",
    { method: "POST" },
  )
}

// ─── Y-prep.2 #288 — JIRA webhook secret rotation ───
//
// Structural parity with the Gerrit rotate endpoint (one-time reveal
// contract — plain secret is returned once in the response body and
// is never re-readable). Transport differs: JIRA Cloud webhooks use
// `Authorization: Bearer <token>`, not HMAC body signing, so the
// `signature_header` / `signature_algorithm` fields surface the real
// transport shape (`"Authorization"` / `"bearer-token"`) — field names
// mirror Gerrit, values reflect JIRA semantics.
export type JiraWebhookSecretRotateResult = GerritWebhookSecretRotateResult

export async function generateJiraWebhookSecret(): Promise<JiraWebhookSecretRotateResult> {
  return request<JiraWebhookSecretRotateResult>(
    "/runtime/git-forge/jira/webhook-secret/generate",
    { method: "POST" },
  )
}

// ─── B14 Part C row 227: Gerrit Setup Wizard finalize (write config + enable) ───
//
// After Steps 1–5 all surface DONE the wizard pipes the collected SSH
// endpoint + project + REST URL into a single atomic write that flips
// `settings.gerrit_enabled = true` and persists the rest of the
// `gerrit_*` fields. The response carries the post-write settings echo
// (webhook secret reported as configured/not, never plain) and the
// localised confirmation message the UI renders as "Gerrit 整合已啟用".
export interface GerritFinalizeConfig {
  url?: string
  ssh_host?: string
  ssh_port?: number
  project?: string
  replication_targets?: string
  webhook_secret_configured?: boolean
}

export interface GerritFinalizeResult {
  status: "ok" | "error"
  enabled?: boolean
  message?: string
  config?: GerritFinalizeConfig
  note?: string
}

export async function finalizeGerritIntegration(args: {
  url?: string
  ssh_host: string
  ssh_port?: number
  project?: string
  replication_targets?: string
}): Promise<GerritFinalizeResult> {
  return request<GerritFinalizeResult>("/runtime/git-forge/gerrit/finalize", {
    method: "POST",
    body: JSON.stringify({
      url: args.url ?? "",
      ssh_host: args.ssh_host,
      ssh_port: args.ssh_port ?? 29418,
      project: args.project ?? "",
      replication_targets: args.replication_targets ?? "",
    }),
  })
}

// ─── Tenant Secrets (I4) ───

export interface TenantSecret {
  id: string
  key_name: string
  fingerprint: string
  secret_type: string
  metadata: Record<string, unknown>
  updated_at: string
}

export async function listTenantSecrets(secretType?: string): Promise<TenantSecret[]> {
  const q = secretType ? `?secret_type=${encodeURIComponent(secretType)}` : ""
  return request<TenantSecret[]>(`/secrets${q}`)
}

export async function createTenantSecret(body: {
  key_name: string; value: string; secret_type: string; metadata?: Record<string, unknown>
}): Promise<{ id: string; status: string }> {
  return request<{ id: string; status: string }>("/secrets", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function updateTenantSecret(id: string, body: {
  value?: string; metadata?: Record<string, unknown>
}): Promise<{ id: string; status: string }> {
  return request<{ id: string; status: string }>(`/secrets/${id}`, {
    method: "PUT",
    body: JSON.stringify(body),
  })
}

export async function deleteTenantSecret(id: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/secrets/${id}`, { method: "DELETE" })
}

// ─── M2: Per-tenant Disk Quota ───

export interface TenantStorageUsage {
  tenant_id: string
  plan: string
  quota: { soft_bytes: number; hard_bytes: number; keep_recent_runs: number }
  usage: {
    artifacts_bytes: number
    workflow_runs_bytes: number
    backups_bytes: number
    ingest_tmp_bytes: number
    total_bytes: number
  }
  over_soft: boolean
  over_hard: boolean
}

export interface TenantStorageCleanupSummary {
  tenant_id: string
  usage_before_bytes: number
  usage_after_bytes: number
  target_bytes: number
  deleted: Array<{ run_id: string; freed_bytes: number }>
  skipped_keep: string[]
  skipped_recent: string[]
}

export async function getStorageUsage(tenantId?: string): Promise<TenantStorageUsage> {
  const q = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ""
  return request<TenantStorageUsage>(`/storage/usage${q}`)
}

export async function triggerStorageCleanup(
  tenantId?: string,
  targetBytes?: number,
): Promise<TenantStorageCleanupSummary> {
  const params = new URLSearchParams()
  if (tenantId) params.set("tenant_id", tenantId)
  if (targetBytes !== undefined) params.set("target_bytes", String(targetBytes))
  const q = params.toString() ? `?${params.toString()}` : ""
  return request<TenantStorageCleanupSummary>(`/storage/cleanup${q}`, { method: "POST" })
}

export async function createVendorSDK(body: Record<string, unknown>): Promise<{ status: string; platform: string }> {
  return request<{ status: string; platform: string }>("/runtime/vendor/sdks", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export async function deleteVendorSDK(platform: string): Promise<{ status: string }> {
  return request<{ status: string }>(`/runtime/vendor/sdks/${platform}`, { method: "DELETE" })
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

// ─── Auth (Phase 54 + Internet-exposure hardening) ──────────

export interface AuthUser {
  id: string
  email: string
  name: string
  // Y2 (#278): backend ROLES tuple is ("viewer","operator","admin","super_admin").
  // The platform-tier "super_admin" gates the /admin/tenants REST surface and
  // (Y8 row 3) the matching frontend page.
  role: "viewer" | "operator" | "admin" | "super_admin"
  enabled: boolean
  tenant_id: string
}

export interface TenantInfo {
  id: string
  name: string
  plan: string
  enabled: boolean
}

export interface WhoamiResponse {
  user: AuthUser
  auth_mode: "open" | "session" | "strict"
  session_id: string | null
}

export async function whoami(): Promise<WhoamiResponse> {
  return request<WhoamiResponse>("/auth/whoami")
}

export async function listUserTenants(): Promise<TenantInfo[]> {
  return request<TenantInfo[]>("/auth/tenants")
}

// Y8 row 2 — project-switcher data source. Fetches the live project
// list under ``tenantId`` from the Y4 row 2 endpoint
// (``GET /api/v1/tenants/{tid}/projects``). The backend already
// scopes the response to caller visibility (super_admin / tenant
// admin → full; member / viewer → only ``project_members`` rows),
// so the frontend renders whatever rows it gets without re-applying
// RBAC. ``archived=false`` is the default — archived projects do
// not show in the dashboard switcher (operator manages those from
// the per-tenant settings page Y8 row 4 once it lands).
export interface TenantProjectInfo {
  project_id: string
  tenant_id: string
  product_line: "embedded" | "web" | "mobile" | "software" | "custom"
  name: string
  slug: string
  parent_id: string | null
  plan_override: string | null
  disk_budget_bytes: number | null
  llm_budget_tokens: number | null
  created_by: string
  created_at: string
  archived_at: string | null
}

export async function listTenantProjects(tenantId: string): Promise<TenantProjectInfo[]> {
  const res = await request<{
    tenant_id: string
    product_line_filter: string | null
    archived_filter: "false" | "true" | "all"
    count: number
    projects: TenantProjectInfo[]
  }>(`/tenants/${encodeURIComponent(tenantId)}/projects`)
  return res.projects
}

/**
 * AS.7.1 — login() accepts an optional `extras` map merged into the
 * JSON request body. Used by the login page to thread the AS.6.3
 * turnstile_token + the AS.6.4 rotating honeypot field name (a
 * dynamic key) through the same request without expanding the
 * function's positional signature with five new args.
 *
 * Backend `LoginRequest` declares `model_config = {"extra": "allow"}`
 * so the unknown keys round-trip into `model_dump()` for the
 * AS.6.4 honeypot validate_honeypot to inspect. Same shape contract
 * applies for any future per-form extras the AS.7.x scaffolds add.
 */
export async function login(
  email: string,
  password: string,
  extras?: Readonly<Record<string, string>>,
): Promise<LoginResponse> {
  const payload: Record<string, string> = { email, password, ...(extras || {}) }
  return request<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export async function logout(): Promise<void> {
  await request<{ status: string }>("/auth/logout", { method: "POST" })
}

// ─── Admin tenant CRUD (Y2 #278 / Y8 row 3 admin page) ───────

export type TenantPlan = "free" | "starter" | "pro" | "enterprise"

export interface AdminTenantUsage {
  user_count: number
  project_count: number
  disk_used_bytes: number
  llm_tokens_30d: number
  rate_limit_hits_7d: number
  last_activity_at: number | null
}

export interface AdminTenantRow {
  id: string
  name: string
  plan: TenantPlan
  enabled: boolean
  created_at: string
  usage: AdminTenantUsage
}

export interface AdminTenantListResponse {
  tenants: AdminTenantRow[]
}

export async function adminListTenants(): Promise<AdminTenantListResponse> {
  return request<AdminTenantListResponse>("/admin/tenants")
}

export interface AdminCreateTenantRequest {
  id: string
  name: string
  plan?: TenantPlan
  enabled?: boolean
}

export interface AdminTenantCreated {
  id: string
  name: string
  plan: TenantPlan
  enabled: boolean
  created_at: string
}

export async function adminCreateTenant(
  body: AdminCreateTenantRequest,
): Promise<AdminTenantCreated> {
  return request<AdminTenantCreated>("/admin/tenants", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export interface AdminPatchTenantRequest {
  name?: string
  plan?: TenantPlan
  enabled?: boolean
}

export async function adminPatchTenant(
  tenantId: string,
  body: AdminPatchTenantRequest,
): Promise<AdminTenantCreated> {
  return request<AdminTenantCreated>(
    `/admin/tenants/${encodeURIComponent(tenantId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    },
  )
}

// ─── Tenant settings page (Y8 row 4) ───────────────────────────
//
// REST surface used by ``/tenants/{tid}/settings`` (tenant-admin
// scoped). Backend routers already ship the contracts:
//   • Y3 (#279) — tenant_invites router (POST/GET/DELETE invites)
//   • Y4 (#280) — tenant_members router (GET/PATCH/DELETE members)
//                 + tenant_projects router (POST/PATCH/archive/restore)
// Server-side gating is ``_user_can_manage_members`` /
// ``_user_can_invite_into`` / ``_user_can_create_project_in`` —
// platform super_admin OR active membership with role ∈ {owner,admin}.

export type TenantMemberRole = "owner" | "admin" | "member" | "viewer"
export type TenantMemberStatus = "active" | "suspended"

export interface TenantMemberRow {
  user_id: string
  email: string
  name: string
  role: TenantMemberRole
  status: TenantMemberStatus
  user_enabled: boolean
  joined_at: string
  last_active_at: string | null
}

export interface ListTenantMembersResponse {
  tenant_id: string
  status_filter: "active" | "suspended" | "all"
  count: number
  members: TenantMemberRow[]
}

export async function listTenantMembers(
  tenantId: string,
  opts?: { status?: "active" | "suspended" | "all"; limit?: number },
): Promise<ListTenantMembersResponse> {
  const params = new URLSearchParams()
  if (opts?.status) params.set("status", opts.status)
  if (opts?.limit) params.set("limit", String(opts.limit))
  const qs = params.toString() ? `?${params.toString()}` : ""
  return request<ListTenantMembersResponse>(
    `/tenants/${encodeURIComponent(tenantId)}/members${qs}`,
  )
}

export interface PatchTenantMemberRequest {
  role?: TenantMemberRole
  status?: TenantMemberStatus
}

export async function patchTenantMember(
  tenantId: string,
  userId: string,
  body: PatchTenantMemberRequest,
): Promise<TenantMemberRow & { tenant_id: string; no_change: boolean }> {
  return request<TenantMemberRow & { tenant_id: string; no_change: boolean }>(
    `/tenants/${encodeURIComponent(tenantId)}/members/${encodeURIComponent(userId)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  )
}

export async function deleteTenantMember(
  tenantId: string,
  userId: string,
): Promise<TenantMemberRow & { tenant_id: string; already_suspended?: boolean }> {
  return request<
    TenantMemberRow & { tenant_id: string; already_suspended?: boolean }
  >(
    `/tenants/${encodeURIComponent(tenantId)}/members/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  )
}

export type TenantInviteStatus =
  | "pending"
  | "accepted"
  | "revoked"
  | "expired"

export interface TenantInviteRow {
  invite_id: string
  email: string
  role: TenantMemberRole
  status: TenantInviteStatus
  invited_by: string | null
  created_at: string
  expires_at: string
}

export interface ListTenantInvitesResponse {
  tenant_id: string
  status_filter: "pending" | "accepted" | "revoked" | "expired" | "all"
  count: number
  invites: TenantInviteRow[]
}

export async function listTenantInvites(
  tenantId: string,
  opts?: {
    status?: "pending" | "accepted" | "revoked" | "expired" | "all"
    limit?: number
  },
): Promise<ListTenantInvitesResponse> {
  const params = new URLSearchParams()
  if (opts?.status) params.set("status", opts.status)
  if (opts?.limit) params.set("limit", String(opts.limit))
  const qs = params.toString() ? `?${params.toString()}` : ""
  return request<ListTenantInvitesResponse>(
    `/tenants/${encodeURIComponent(tenantId)}/invites${qs}`,
  )
}

export interface CreateTenantInviteRequest {
  email: string
  role?: TenantMemberRole
}

export interface CreatedTenantInvite {
  invite_id: string
  token_plaintext: string
  expires_at: string
}

export async function createTenantInvite(
  tenantId: string,
  body: CreateTenantInviteRequest,
): Promise<CreatedTenantInvite> {
  return request<CreatedTenantInvite>(
    `/tenants/${encodeURIComponent(tenantId)}/invites`,
    { method: "POST", body: JSON.stringify(body) },
  )
}

export async function revokeTenantInvite(
  tenantId: string,
  inviteId: string,
): Promise<TenantInviteRow & { tenant_id: string; already_revoked?: boolean }> {
  return request<
    TenantInviteRow & { tenant_id: string; already_revoked?: boolean }
  >(
    `/tenants/${encodeURIComponent(tenantId)}/invites/${encodeURIComponent(inviteId)}`,
    { method: "DELETE" },
  )
}

// ─────────────────────────────────────────────────────────────────
// Y8 row 7 — POST /api/v1/invites/{invite_id}/accept (open endpoint)
// Single backend call serves both flows:
//   • anon caller → name/password are consulted; user row created
//     unless an account with the invite's email already exists
//   • authenticated caller → name/password ignored; session.email
//     must match invite.email or backend returns 409
// See backend/routers/tenant_invites.py::accept_invite for the
// full two-branch contract + error map (404/403/409/410/422/429).
// ─────────────────────────────────────────────────────────────────

export interface AcceptInviteRequest {
  token: string
  name?: string
  password?: string | null
}

export interface AcceptInviteResponse {
  invite_id: string
  tenant_id: string
  user_id: string
  user_email: string
  role: TenantMemberRole
  status: "accepted"
  user_was_created: boolean
  already_member: boolean
}

export async function acceptInvite(
  inviteId: string,
  body: AcceptInviteRequest,
): Promise<AcceptInviteResponse> {
  return request<AcceptInviteResponse>(
    `/invites/${encodeURIComponent(inviteId)}/accept`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
    // The accept flow is intentionally usable by anonymous callers
    // (no session cookie). Suppress the global 401 redirect — the
    // page below decides how to surface failures inline.
    { skipGlobalErrorHandler: true },
  )
}

export type ProductLine = "embedded" | "web" | "mobile" | "software" | "custom"

export interface CreateTenantProjectRequest {
  product_line: ProductLine
  name: string
  slug: string
  plan_override?: TenantPlan | null
  disk_budget_bytes?: number | null
  llm_budget_tokens?: number | null
}

export async function createTenantProject(
  tenantId: string,
  body: CreateTenantProjectRequest,
): Promise<TenantProjectInfo> {
  return request<TenantProjectInfo>(
    `/tenants/${encodeURIComponent(tenantId)}/projects`,
    { method: "POST", body: JSON.stringify(body) },
  )
}

export async function archiveTenantProject(
  tenantId: string,
  projectId: string,
): Promise<TenantProjectInfo & { no_change?: boolean }> {
  return request<TenantProjectInfo & { no_change?: boolean }>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/archive`,
    { method: "POST" },
  )
}

export async function restoreTenantProject(
  tenantId: string,
  projectId: string,
): Promise<TenantProjectInfo & { no_change?: boolean }> {
  return request<TenantProjectInfo & { no_change?: boolean }>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/restore`,
    { method: "POST" },
  )
}

// Y8 row 4 — list-with-archived variant for the Projects tab. The
// dashboard ProjectSwitcher (Y8 row 2) only wants live rows, so the
// existing ``listTenantProjects`` defaults ``archived=false``. The
// settings page surfaces archived rows so a tenant admin can restore
// them — we expose a separate wrapper that pins ``archived=all``
// rather than re-shaping the row 2 contract.
export async function listAllTenantProjects(
  tenantId: string,
): Promise<TenantProjectInfo[]> {
  const res = await request<{
    tenant_id: string
    product_line_filter: string | null
    archived_filter: "false" | "true" | "all"
    count: number
    projects: TenantProjectInfo[]
  }>(`/tenants/${encodeURIComponent(tenantId)}/projects?archived=all`)
  return res.projects
}

// ─── Project settings page (Y8 row 5) ──────────────────────────
//
// REST surface for ``/projects/{pid}/settings`` (project-owner /
// tenant-admin scoped). Covers three operation surfaces:
//
//   • Members → GET / POST / PATCH / DELETE
//               /api/v1/tenants/{tid}/projects/{pid}/members[/{uid}]
//   • Budget  → PATCH /api/v1/tenants/{tid}/projects/{pid}
//               (tri-state body — null clears; project then inherits
//               from the tenant). Read uses listTenantProjects.
//   • Shares  → GET / POST / DELETE
//               /api/v1/tenants/{tid}/projects/{pid}/shares[/{sid}]
//
// Tenant scope is supplied by the caller (the page resolves
// tenant_id via lib/project-context's projects[] list); these
// wrappers do not implicitly read ``_currentTenantId`` so two
// concurrent settings pages on different tenants do not race the
// header.

export type ProjectMemberRole = "owner" | "contributor" | "viewer"

export interface ProjectMemberRow {
  user_id: string
  email: string
  name: string
  project_id: string
  role: ProjectMemberRole
  created_at: string
  user_enabled: boolean
}

export interface ListProjectMembersResponse {
  tenant_id: string
  project_id: string
  count: number
  members: ProjectMemberRow[]
}

export async function listProjectMembers(
  tenantId: string,
  projectId: string,
  opts?: { limit?: number },
): Promise<ListProjectMembersResponse> {
  const params = new URLSearchParams()
  if (opts?.limit) params.set("limit", String(opts.limit))
  const qs = params.toString() ? `?${params.toString()}` : ""
  return request<ListProjectMembersResponse>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/members${qs}`,
  )
}

export interface CreateProjectMemberRequest {
  user_id: string
  role: ProjectMemberRole
}

export async function createProjectMember(
  tenantId: string,
  projectId: string,
  body: CreateProjectMemberRequest,
): Promise<ProjectMemberRow & { tenant_id: string }> {
  return request<ProjectMemberRow & { tenant_id: string }>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/members`,
    { method: "POST", body: JSON.stringify(body) },
  )
}

export interface PatchProjectMemberRequest {
  role: ProjectMemberRole
}

export async function patchProjectMember(
  tenantId: string,
  projectId: string,
  userId: string,
  body: PatchProjectMemberRequest,
): Promise<ProjectMemberRow & { tenant_id: string; no_change: boolean }> {
  return request<
    ProjectMemberRow & { tenant_id: string; no_change: boolean }
  >(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  )
}

export async function deleteProjectMember(
  tenantId: string,
  projectId: string,
  userId: string,
): Promise<{ tenant_id: string; project_id: string; user_id: string; already_removed: boolean; role?: ProjectMemberRole }> {
  return request<{
    tenant_id: string
    project_id: string
    user_id: string
    already_removed: boolean
    role?: ProjectMemberRole
  }>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  )
}

// Project budget — uses the existing PATCH /tenants/{tid}/projects/{pid}
// endpoint with the tri-state body (omit = leave alone; null = clear =
// inherit from tenant; non-null = set). Wrapper accepts ``undefined`` to
// signal "leave alone" and ``null`` to signal "clear", matching the
// pydantic ``model_fields_set`` contract on the server.
export interface PatchProjectBudgetRequest {
  plan_override?: TenantPlan | null
  disk_budget_bytes?: number | null
  llm_budget_tokens?: number | null
  name?: string
  parent_id?: string | null
}

export async function patchProjectBudget(
  tenantId: string,
  projectId: string,
  body: PatchProjectBudgetRequest,
): Promise<TenantProjectInfo & { no_change?: boolean }> {
  return request<TenantProjectInfo & { no_change?: boolean }>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  )
}

export type ProjectShareRole = "viewer" | "contributor"

export interface ProjectShareRow {
  share_id: string
  project_id: string
  guest_tenant_id: string
  role: ProjectShareRole
  granted_by: string | null
  created_at: string
  expires_at: string | null
}

export interface ListProjectSharesResponse {
  tenant_id: string
  project_id: string
  count: number
  shares: ProjectShareRow[]
}

export async function listProjectShares(
  tenantId: string,
  projectId: string,
  opts?: { limit?: number },
): Promise<ListProjectSharesResponse> {
  const params = new URLSearchParams()
  if (opts?.limit) params.set("limit", String(opts.limit))
  const qs = params.toString() ? `?${params.toString()}` : ""
  return request<ListProjectSharesResponse>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/shares${qs}`,
  )
}

export interface CreateProjectShareRequest {
  guest_tenant_id: string
  role: ProjectShareRole
  expires_at?: string | null
}

export async function createProjectShare(
  tenantId: string,
  projectId: string,
  body: CreateProjectShareRequest,
): Promise<ProjectShareRow & { tenant_id: string }> {
  return request<ProjectShareRow & { tenant_id: string }>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/shares`,
    { method: "POST", body: JSON.stringify(body) },
  )
}

export async function deleteProjectShare(
  tenantId: string,
  projectId: string,
  shareId: string,
): Promise<{
  tenant_id: string
  project_id: string
  share_id: string
  already_revoked: boolean
  guest_tenant_id?: string
  role?: ProjectShareRole
}> {
  return request<{
    tenant_id: string
    project_id: string
    share_id: string
    already_revoked: boolean
    guest_tenant_id?: string
    role?: ProjectShareRole
  }>(
    `/tenants/${encodeURIComponent(tenantId)}/projects/${encodeURIComponent(projectId)}/shares/${encodeURIComponent(shareId)}`,
    { method: "DELETE" },
  )
}

// ─── Session management (J3) ─────────────────────────────────

export interface SessionItem {
  token_hint: string
  created_at: number
  expires_at: number
  last_seen_at: number
  ip: string
  user_agent: string
  is_current: boolean
}

export async function listSessions(): Promise<{ items: SessionItem[]; count: number }> {
  return request<{ items: SessionItem[]; count: number }>("/auth/sessions")
}

export interface RevokeSessionOptions {
  /**
   * Q.2 (#296) 「這不是我」 cascade. When set to ``"not_me"`` the backend
   * treats the call as an account-compromise response:
   *   - revoke the flagged session (same as default path)
   *   - rotate every other session on this account (including the caller's)
   *   - flip ``must_change_password=1`` so the 428 gate forces a
   *     password change after re-login
   *   - clear the caller's own session + CSRF cookies
   *
   * Caller MUST treat a resolved promise as "you are now signed out on
   * this device" — a full-page navigation to ``/login?reason=user_security_event&trigger=not_me_cascade``
   * is the expected follow-up so the banner copy + re-login flow can
   * drive the password change.
   */
  cascade?: "not_me"
}

export interface RevokeSessionResponse {
  status: string
  token_hint?: string
  cascade?: "not_me"
  rotated_count?: number
  must_change_password?: boolean
}

export async function revokeSession(
  tokenHint: string, options?: RevokeSessionOptions,
): Promise<RevokeSessionResponse> {
  const qs = options?.cascade
    ? `?cascade=${encodeURIComponent(options.cascade)}`
    : ""
  return request<RevokeSessionResponse>(
    `/auth/sessions/${encodeURIComponent(tokenHint)}${qs}`,
    { method: "DELETE" },
  )
}

export async function revokeAllOtherSessions(): Promise<{ status: string; revoked_count: number }> {
  return request<{ status: string; revoked_count: number }>("/auth/sessions", {
    method: "DELETE",
  })
}

// ─── Q.5 (#299) Active device presence ──────────────────────
//
// Read-side counterpart to the SSE heartbeat written by
// `backend/routers/events.py::event_stream`. Powers the dashboard
// header presence badge and its hover mini-list.

export interface PresenceDevice {
  session_id: string
  token_hint: string
  device_name: string
  ua_hash: string
  last_heartbeat_at: number
  idle_seconds: number
  status: "active" | "idle"
  is_current: boolean
}

export interface PresenceResponse {
  active_count: number
  window_seconds: number
  now: number
  devices: PresenceDevice[]
}

export async function getSessionsPresence(): Promise<PresenceResponse> {
  return request<PresenceResponse>("/auth/sessions/presence")
}

// ─── Q.6 (#300) Per-user draft composer slots ───────────────

/**
 * Q.6 (#300) draft response shape.
 *
 * ``updated_at`` is ``number`` on a hit (server-committed wall-clock
 * seconds) and ``null`` on the "never typed here" miss path so the
 * restore flow can treat both cases uniformly without a separate
 * 404 branch. PUT always returns a number (fresh write); GET returns
 * ``null`` only when no row has been written for the slot yet.
 */
export interface DraftResponse {
  slot_key: string
  content: string
  updated_at: number | null
}

/**
 * Q.6 (#300, checkbox 1) — write a draft slot. Called by the INVOKE
 * command bar (`invoke:main`) and the workspace chat composer
 * (`chat:main`) on every change after a 500 ms debounce.
 *
 * The call is `skipGlobalErrorHandler: true` because the operator is
 * actively typing — we must not pop a toast on a transient 5xx /
 * offline; the next debounce tick will retry naturally and the GET
 * /user/drafts/{slot_key} restore on the next page load will fix any
 * lost ticks.
 *
 * Returns the server-committed `updated_at` so the caller can echo
 * it into local storage for the Q.6 conflict-detection check on
 * restore (checkbox 4).
 */
export async function putUserDraft(
  slotKey: string, content: string,
): Promise<DraftResponse> {
  return request<DraftResponse>(`/user/drafts/${encodeURIComponent(slotKey)}`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  }, { skipGlobalErrorHandler: true })
}

/**
 * Q.6 (#300, checkbox 2) — restore on new device.
 *
 * Fetch the server-stored draft for this slot once on composer mount.
 * When the slot has never been written, the backend returns
 * ``{content: "", updated_at: null}`` (shaped empty, not 404) so the
 * frontend can assign ``content`` straight into the editor without
 * needing to distinguish "never existed" from "empty string" — the
 * composer starts blank either way.
 *
 * ``skipGlobalErrorHandler: true`` because a flaky restore on page
 * load should not block the operator from typing; the composer
 * falls back to the local-storage cache (Q.6 checkbox 4 wiring) and
 * the next 500 ms debounce tick will re-sync.
 */
export async function getUserDraft(slotKey: string): Promise<DraftResponse> {
  return request<DraftResponse>(
    `/user/drafts/${encodeURIComponent(slotKey)}`,
    { method: "GET" },
    { skipGlobalErrorHandler: true },
  )
}

// ─── Self-service password change ───────────────────────────

export interface ChangePasswordResponse {
  status: string
  must_change_password: boolean
  csrf_token?: string
}

/**
 * Call ``POST /auth/change-password``. Rotates the current device's
 * session + kicks every peer session (Q.1 security red line). On
 * success, the backend sets the new session + CSRF cookies and
 * returns ``{status: "password_changed", ...}``.
 *
 * Q.1 UI follow-up (2026-04-24): callers MUST treat a resolved
 * promise as success — the caller's own session was refreshed
 * via ``Set-Cookie`` in the same response, so no re-login flow is
 * needed on the device that initiated the change. Only *peer*
 * devices get 401'd on their next request (handled by the global
 * /login redirect with a ``reason=user_security_event`` query
 * param that renders a dedicated banner — see ``_handleTerminalError``
 * above and ``app/login/page.tsx``).
 *
 * Rejection maps to the standard ``ApiError`` — 401 "current
 * password is incorrect", 422 password-strength / history-reuse,
 * etc. Those surface on the caller's own form, NOT on the global
 * toast center.
 */
export async function changePassword(
  currentPassword: string, newPassword: string,
): Promise<ChangePasswordResponse> {
  return request<ChangePasswordResponse>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  }, { skipGlobalErrorHandler: true })
}

// ─── MFA (K5) ───────────────────────────────────────────────

export interface MfaMethod {
  id: string
  method: "totp" | "webauthn"
  name: string
  verified: boolean
  created_at: string
  last_used: string | null
}

export interface MfaStatusResponse {
  methods: MfaMethod[]
  has_mfa: boolean
  require_mfa: boolean
}

export interface LoginResponse {
  user?: AuthUser
  csrf_token?: string
  mfa_required?: boolean
  mfa_token?: string
  mfa_methods?: string[]
}

export async function mfaStatus(): Promise<MfaStatusResponse> {
  return request<MfaStatusResponse>("/auth/mfa/status")
}

export async function mfaTotpEnroll(): Promise<{
  mfa_id: string; secret: string; uri: string; qr_png_b64: string
}> {
  return request("/auth/mfa/totp/enroll", { method: "POST" })
}

export async function mfaTotpConfirm(code: string): Promise<{
  status: string; backup_codes: string[]
}> {
  return request("/auth/mfa/totp/confirm", {
    method: "POST",
    body: JSON.stringify({ code }),
  })
}

export async function mfaTotpDisable(): Promise<{ status: string }> {
  return request("/auth/mfa/totp/disable", { method: "POST" })
}

export async function mfaBackupCodesStatus(): Promise<{
  total: number; remaining: number
}> {
  return request("/auth/mfa/backup-codes/status")
}

export async function mfaBackupCodesRegenerate(): Promise<{
  codes: string[]; count: number
}> {
  return request("/auth/mfa/backup-codes/regenerate", { method: "POST" })
}

export async function mfaChallenge(mfaToken: string, code: string): Promise<{
  user: AuthUser; csrf_token: string; mfa_verified: boolean
}> {
  return request("/auth/mfa/challenge", {
    method: "POST",
    body: JSON.stringify({ mfa_token: mfaToken, code }),
  })
}

export async function mfaWebauthnRegisterBegin(name?: string): Promise<Record<string, unknown>> {
  return request("/auth/mfa/webauthn/register/begin", {
    method: "POST",
    body: JSON.stringify({ name: name || "" }),
  })
}

export async function mfaWebauthnRegisterComplete(credential: unknown, name?: string): Promise<{ status: string }> {
  return request("/auth/mfa/webauthn/register/complete", {
    method: "POST",
    body: JSON.stringify({ credential, name: name || "" }),
  })
}

export async function mfaWebauthnRemove(mfaId: string): Promise<{ status: string }> {
  return request(`/auth/mfa/webauthn/${encodeURIComponent(mfaId)}`, { method: "DELETE" })
}

export async function mfaWebauthnChallengeBegin(mfaToken: string): Promise<Record<string, unknown>> {
  return request("/auth/mfa/webauthn/challenge/begin", {
    method: "POST",
    body: JSON.stringify({ mfa_token: mfaToken }),
  })
}

export async function mfaWebauthnChallengeComplete(mfaToken: string, credential: unknown): Promise<{
  user: AuthUser; csrf_token: string; mfa_verified: boolean
}> {
  return request("/auth/mfa/webauthn/challenge/complete", {
    method: "POST",
    body: JSON.stringify({ mfa_token: mfaToken, credential }),
  })
}

// ─── Audit log (J6) ──────────────────────────────────────────

export interface AuditEntry {
  id: number
  ts: number
  actor: string
  action: string
  entity_kind: string
  entity_id: string
  before: Record<string, unknown>
  after: Record<string, unknown>
  prev_hash: string
  curr_hash: string
  session_id: string | null
  session_ip: string | null
  session_ua: string | null
}

export interface AuditFilters {
  since?: number
  actor?: string
  entity_kind?: string
  session_id?: string
  limit?: number
}

export async function listAuditEntries(
  filters?: AuditFilters,
): Promise<{ items: AuditEntry[]; count: number; filtered_to_self: boolean }> {
  const params = new URLSearchParams()
  if (filters?.since) params.set("since", String(filters.since))
  if (filters?.actor) params.set("actor", filters.actor)
  if (filters?.entity_kind) params.set("entity_kind", filters.entity_kind)
  if (filters?.session_id) params.set("session_id", filters.session_id)
  if (filters?.limit) params.set("limit", String(filters.limit))
  const qs = params.toString()
  return request<{ items: AuditEntry[]; count: number; filtered_to_self: boolean }>(
    `/audit${qs ? `?${qs}` : ""}`,
  )
}

// ─── User preferences (J4) ───────────────────────────────────

export async function getUserPreferences(): Promise<{ items: Record<string, string> }> {
  return request<{ items: Record<string, string> }>("/user-preferences")
}

export async function getUserPreference(key: string): Promise<{ key: string; value: string } | null> {
  try {
    return await request<{ key: string; value: string }>(`/user-preferences/${encodeURIComponent(key)}`)
  } catch {
    return null
  }
}

export async function setUserPreference(key: string, value: string): Promise<void> {
  await request<{ key: string; value: string }>(`/user-preferences/${encodeURIComponent(key)}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  })
}

// ─── Ops Summary (L1-04) ─────────────────────────────────────

export interface OpsSummary {
  checked_at: number
  uptime_s: number | null
  daily_cost_usd: number
  hourly_cost_usd: number
  token_frozen: boolean
  budget_level: string
  decisions_pending: number
  sse_subscribers: number
  watchdog_age_s: number | null
  /** Phase 64-C-LOCAL UX-6: T3 runner dispatch breakdown. local + bundle
   * always present; ssh / qemu populated once those runners land. */
  t3_runners?: { local: number; ssh: number; qemu: number; bundle: number }
  /** R2 (#308): the single agent with the highest current semantic-entropy
   * score, or null if the monitor hasn't produced a measurement yet. */
  highest_entropy_agent?: {
    agent_id: string
    score: number
    verdict: "ok" | "warning" | "deadlock"
  } | null
  /** H3 row 1524: DRF coordinator pressure — queue depth (tasks waiting
   * for a slot), deferred count in the last 5 minutes, and the effective
   * concurrency budget (may be < capacity_max when derated). */
  coordinator?: {
    capacity_max: number
    effective_budget: number
    queue_depth: number
    deferred_5m: number
    derated: boolean
    derate_reason?: string | null
  } | null
  /** H4a row 2583: Adaptive (AIMD) budget transparency. ``budget`` is
   * the current host-level concurrency token count derived by the AIMD
   * controller; ``trace`` is the last 5 min of state-changing events
   * (init / additive_increase / multiplicative_decrease / hard_cap /
   * floor / hold) so the UI can render the rise/fall history. */
  aimd?: {
    budget: number
    capacity_max: number
    floor: number
    init_budget: number
    last_reason: string
    last_ai_at: number
    pressure_clock_started_at: number | null
    thresholds: {
      cpu_ai_pct: number
      mem_ai_pct: number
      cpu_md_pct: number
      mem_md_pct: number
      ai_interval_s: number
      md_persistence_s: number
    }
    trace: Array<{
      timestamp: number
      budget: number
      reason: string
      cpu_percent: number
      mem_percent: number
    }>
  } | null
}

export async function getOpsSummary(): Promise<OpsSummary> {
  return request<OpsSummary>("/ops/summary")
}

/** H3 row 1527 — operator force-turbo override.
 *
 * Clears the H2 auto-derate state machine and resets the DRF sandbox
 * capacity derate ratio back to 1.0 so the effective budget returns
 * to CAPACITY_MAX. Requires `confirm=true` (server enforces it) so
 * the frontend confirm dialog's OOM warning can't be silently skipped
 * by a curl / CLI caller. Backend writes a Phase-53 hash-chain audit
 * row and broadcasts a `coordinator.force_turbo_override` SSE event. */
export interface ForceTurboResult {
  applied: boolean
  cleared_turbo_derate: boolean
  reset_capacity_derate: boolean
  before: {
    turbo_derate_active: boolean
    capacity_derate_ratio: number
    capacity_derate_reason?: string | null
    effective_capacity_max?: number
    capacity_max?: number
  }
  after: {
    turbo_derate_active: boolean
    capacity_derate_ratio: number
    effective_capacity_max?: number
    restored_to_budget: number
    manual_override: boolean
    operator_reason?: string | null
    at: number
  }
}

export async function forceTurboOverride(
  opts: { confirm: boolean; reason?: string } = { confirm: true },
): Promise<ForceTurboResult> {
  return request<ForceTurboResult>("/coordinator/force-turbo", {
    method: "POST",
    body: JSON.stringify({
      confirm: opts.confirm,
      ...(opts.reason ? { reason: opts.reason } : {}),
    }),
  })
}

// ─── R2 (#308) Semantic Entropy Monitor ──────────────────────

export interface AgentEntropySnapshot {
  agent_id: string
  entropy_score: number
  verdict: "ok" | "warning" | "deadlock"
  sparkline: number[]
  recent_outputs: string[]
  round_counter: number
  loop_count: number
  loop_max: number
  last_updated: number
  deadlock_events: number
}

export interface EntropyAgentsResponse {
  agents: AgentEntropySnapshot[]
  highest: AgentEntropySnapshot | null
}

export async function getEntropyAgents(): Promise<EntropyAgentsResponse> {
  return request<EntropyAgentsResponse>("/entropy/agents")
}

export async function getEntropyAgent(agentId: string): Promise<AgentEntropySnapshot> {
  return request<AgentEntropySnapshot>(`/entropy/agents/${encodeURIComponent(agentId)}`)
}

// ─── O9 (#272) Orchestration Observability ───────────────────

export interface OrchestrationQueueSnapshot {
  by_priority: Record<string, number>   // P0..P3
  by_state: Record<string, number>      // Queued / Ready / Claimed / ...
  total: number
}

export interface OrchestrationLockBucket {
  task_id: string
  paths: string[]
  oldest_acquired_at: number
  earliest_expiry: number
}

export interface OrchestrationLockSnapshot {
  by_task: Record<string, OrchestrationLockBucket>
  total_paths: number
  total_tasks: number
}

export interface OrchestrationMergerSnapshot {
  plus_two_total: number
  abstain_total: number
  security_refusal_total: number
  total_votes: number
  plus_two_rate: number          // 0..1
  abstain_rate: number           // 0..1
  security_refusal_rate: number  // 0..1
}

export interface OrchestrationWorkerSnapshot {
  active: number
  inflight: number
  capacity: number
  utilisation: number            // 0..1, 0 if capacity unset
}

export interface AwaitingHumanEntry {
  change_id: string
  project: string
  file_path: string
  merger_confidence: number
  merger_rationale: string
  review_url: string
  push_sha: string
  awaiting_since: number
  jira_ticket: string
  age_seconds: number
}

export interface OrchestrationSnapshot {
  checked_at: number
  queue: OrchestrationQueueSnapshot
  locks: OrchestrationLockSnapshot
  merger: OrchestrationMergerSnapshot
  workers: OrchestrationWorkerSnapshot
  awaiting_human_plus_two: AwaitingHumanEntry[]
  awaiting_human_warn_hours: number
}

export async function getOrchestrationSnapshot(): Promise<OrchestrationSnapshot> {
  return request<OrchestrationSnapshot>("/orchestration/snapshot")
}

export async function getAwaitingHumanList(): Promise<{
  items: AwaitingHumanEntry[]
  warn_hours: number
}> {
  return request("/orchestration/awaiting-human")
}

// ─── Workflow runs (RunHistory panel) ───

export interface WorkflowRunSummary {
  id: string
  kind: string
  status: string
  started_at: number | null
  completed_at: number | null
  last_step_id: string | null
  metadata: Record<string, unknown>
  version: number
}

export interface WorkflowStepDetail {
  id: string
  key: string
  started_at: number | null
  completed_at: number | null
  is_done: boolean
  error: string | null
  output: string | null
}

export interface WorkflowRunDetail {
  run: WorkflowRunSummary
  steps: WorkflowStepDetail[]
  in_flight: boolean
}

export async function getWorkflowRun(runId: string): Promise<WorkflowRunDetail> {
  return request<WorkflowRunDetail>(`/workflow/runs/${encodeURIComponent(runId)}`)
}

export async function listWorkflowRuns(opts: { status?: string; limit?: number } = {}): Promise<WorkflowRunSummary[]> {
  const params = new URLSearchParams()
  if (opts.status) params.set("status", opts.status)
  if (opts.limit) params.set("limit", String(opts.limit))
  const qs = params.toString()
  const out = await request<{ runs: WorkflowRunSummary[]; count: number }>(
    `/workflow/runs${qs ? `?${qs}` : ""}`,
  )
  return out.runs
}

export async function retryWorkflowRun(runId: string, version: number): Promise<{ id: string; status: string; version: number }> {
  return request(`/workflow/runs/${encodeURIComponent(runId)}/retry`, {
    method: "POST",
    headers: { "If-Match": String(version) },
  })
}

export async function cancelWorkflowRun(runId: string, version: number): Promise<{ id: string; status: string; version: number }> {
  return request(`/workflow/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    headers: { "If-Match": String(version) },
  })
}

export async function updateWorkflowRun(runId: string, version: number, metadata: Record<string, unknown>): Promise<{ id: string; version: number }> {
  return request(`/workflow/runs/${encodeURIComponent(runId)}`, {
    method: "PATCH",
    headers: { "If-Match": String(version) },
    body: JSON.stringify({ metadata }),
  })
}

// ─── Project Runs — B7 (#207) aggregation ───

export interface ProjectRunSummary {
  total: number
  running: number
  completed: number
  failed: number
  halted: number
}

export interface ProjectRun {
  id: string
  project_id: string
  label: string
  created_at: number
  workflow_run_ids: string[]
  children: WorkflowRunSummary[]
  summary: ProjectRunSummary
}

export async function listProjectRuns(projectId: string, opts: { limit?: number } = {}): Promise<ProjectRun[]> {
  const params = new URLSearchParams()
  if (opts.limit) params.set("limit", String(opts.limit))
  const qs = params.toString()
  const out = await request<{ project_runs: ProjectRun[]; count: number }>(
    `/projects/${encodeURIComponent(projectId)}/runs${qs ? `?${qs}` : ""}`,
  )
  return out.project_runs
}

// ─── Intent Parser (Phase 68-A/B/C) ───

export interface IntentField {
  value: string
  confidence: number
}

export interface IntentConflictOption {
  id: string
  label: string
  desc?: string
}

export interface IntentConflict {
  id: string
  message: string
  fields: string[]
  options: IntentConflictOption[]
  severity: "info" | "routine" | "risky" | "destructive"
  /** Phase 68-D: backend annotates this when the operator resolved
   * the same conflict on a similar prompt before. UI pre-highlights
   * the matching option; click still counts as a fresh decision. */
  prior_choice?: { option_id: string; quality: number; memory_id: string }
}

export interface ParsedSpec {
  project_type:       IntentField
  runtime_model:      IntentField
  target_arch:        IntentField
  target_os:          IntentField
  framework:          IntentField
  persistence:        IntentField
  deploy_target:      IntentField
  hardware_required:  IntentField
  raw_text:           string
  conflicts:          IntentConflict[]
}

export async function parseIntent(text: string, useLlm = true): Promise<ParsedSpec> {
  return request<ParsedSpec>("/intent/parse", {
    method: "POST",
    body: JSON.stringify({ text, use_llm: useLlm }),
  })
}

export async function clarifyIntent(
  parsed: ParsedSpec,
  conflictId: string,
  optionId: string,
): Promise<ParsedSpec> {
  return request<ParsedSpec>("/intent/clarify", {
    method: "POST",
    body: JSON.stringify({ parsed, conflict_id: conflictId, option_id: optionId }),
  })
}

// ─── Repo Ingest + Doc Upload (B5/UX-01) ───

export interface IngestMeta {
  detected_files: string[]
  has_package_json: boolean
  has_readme: boolean
  has_requirements: boolean
  has_cargo: boolean
}

export interface IngestRepoResponse extends ParsedSpec {
  _ingest_meta?: IngestMeta
}

export async function ingestRepo(url: string): Promise<IngestRepoResponse> {
  return request<IngestRepoResponse>("/intent/ingest-repo", {
    method: "POST",
    body: JSON.stringify({ url }),
  })
}

export interface DocFileResult {
  name: string
  status: "parsed" | "rejected" | "error"
  reason?: string
  size?: number
}

export interface UploadDocsResponse {
  spec: ParsedSpec | null
  files: DocFileResult[]
}

export async function uploadDocs(files: File[]): Promise<UploadDocsResponse> {
  const form = new FormData()
  for (const f of files) form.append("files", f)

  const method = "POST"
  const baseHeaders: Record<string, string> = {}
  if (typeof document !== "undefined") {
    const csrf = document.cookie
      .split("; ")
      .find((c) => c.startsWith("omnisight_csrf="))
      ?.split("=")[1]
    if (csrf) baseHeaders["X-CSRF-Token"] = csrf
  }

  const res = await fetch(`${API_V1}/intent/upload-docs`, {
    method,
    credentials: "include",
    headers: baseHeaders,
    body: form,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => "")
    throw new Error(`upload-docs failed (${res.status}): ${text}`)
  }
  return res.json()
}

// ─── DAG Authoring (Phase 56-DAG-E) ───

export interface DAGValidationError {
  rule: string          // schema | cycle | unknown_dep | duplicate_id | tier_violation | io_entity | dep_closure | mece
  task_id: string | null
  message: string
}

export interface DAGValidateResponse {
  ok: boolean
  stage: "schema" | "semantic"
  errors: DAGValidationError[]
  warnings?: DAGValidationError[]
  task_count?: number
  t3_runner?: "local" | "bundle" | "ssh" | "qemu"
  target_platform?: string
}

export interface ToolchainsResponse {
  all: string[]
  by_platform: Record<string, string>
  by_tier: Record<string, string[]>
}

export interface DAGSubmitResponse {
  run_id: string
  plan_id: number | null
  status: string
  validation_errors: DAGValidationError[]
  mutation_rounds?: number
  supersedes_run_id?: string
}

export async function fetchToolchains(): Promise<ToolchainsResponse> {
  return request<ToolchainsResponse>("/runtime/platforms/toolchains")
}

// ─── ZZ.B1 (#304-1) checkbox 3 — turn.complete history backfill ───

export interface TurnCompletePayload {
  turn_id: string
  provider: string | null
  model: string
  agent_type: string | null
  task_id: string | null
  input_tokens: number
  output_tokens: number
  tokens_used: number
  context_limit: number | null
  context_usage_pct: number | null
  latency_ms: number
  cache_read_tokens: number
  cache_create_tokens: number
  cost_usd: number | null
  started_at: string | null
  ended_at: string | null
  summary: string | null
  messages: Array<{
    role: "system" | "user" | "assistant" | "tool"
    content: string
    tokens?: number | null
    tool_name?: string | null
  }>
  tool_calls: Array<{
    name: string
    success: boolean
    args?: Record<string, unknown> | null
    result?: string | null
    duration_ms?: number | null
  }>
  tool_call_count: number
  tool_failure_count: number
  timestamp: string
}

export interface TurnHistoryResponse {
  turns: TurnCompletePayload[]
  count: number
}

/**
 * ZZ.B1 #304-1 checkbox 3: fetch persisted `turn.complete` records so
 * a freshly-mounted `<TurnTimeline>` can seed its ring buffer without
 * waiting for the next live SSE emit. ``limit`` is clamped server-side
 * to [1, 100]. ``sessionId`` narrows to events with matching
 * ``_session_id`` on the persisted payload; omit to get the last N
 * turns across all sessions for the current tenant.
 */
export async function fetchTurnHistory(
  opts: { limit?: number; sessionId?: string } = {},
): Promise<TurnHistoryResponse> {
  const params = new URLSearchParams()
  if (opts.limit !== undefined) params.set("limit", String(opts.limit))
  if (opts.sessionId) params.set("session_id", opts.sessionId)
  const qs = params.toString()
  return request<TurnHistoryResponse>(
    `/runtime/turns${qs ? `?${qs}` : ""}`,
  )
}

export async function validateDag(
  dag: unknown,
  targetPlatform?: string,
): Promise<DAGValidateResponse> {
  return request<DAGValidateResponse>("/dag/validate", {
    method: "POST",
    body: JSON.stringify({ dag, target_platform: targetPlatform }),
  })
}

/**
 * ZZ.B2 #304-2 checkbox 1 — chat-session row returned by
 * `GET /chat/sessions`. `metadata.auto_title` is the LLM-generated
 * descriptive title; `metadata.user_title` is the operator override
 * (reserved for checkbox 2's fallback chain). Rendering precedence is
 * owned by the frontend: `user_title ?? auto_title ?? hash`.
 */
export interface ChatSessionMetadata {
  auto_title?: string
  user_title?: string
  [k: string]: unknown
}

export interface ChatSessionItem {
  session_id: string
  user_id: string
  tenant_id: string
  metadata: ChatSessionMetadata
  created_at: number
  updated_at: number
}

export interface ChatSessionListResponse {
  items: ChatSessionItem[]
  count: number
}

export async function fetchChatSessions(
  opts: { limit?: number } = {},
): Promise<ChatSessionListResponse> {
  const params = new URLSearchParams()
  if (opts.limit !== undefined) params.set("limit", String(opts.limit))
  const qs = params.toString()
  return request<ChatSessionListResponse>(
    `/chat/sessions${qs ? `?${qs}` : ""}`,
  )
}

/**
 * ZZ.B2 #304-2 checkbox 2 — operator rename for a chat session.
 *
 * Writes (or clears, when `title` is `null` / empty) the
 * `metadata.user_title` field on the backend and triggers a
 * `session.titled` SSE event with `source="user"` so other devices of
 * the same operator relabel the sidebar row in-place.
 *
 * Empty `title` intentionally clears the override so the fallback
 * chain drops to `auto_title` / hash — the sidebar does not need a
 * separate "revert" endpoint.
 */
export interface ChatSessionRenameResponse {
  session_id: string
  metadata: ChatSessionMetadata
}

export async function renameChatSession(
  sessionId: string,
  title: string | null,
): Promise<ChatSessionRenameResponse> {
  return request<ChatSessionRenameResponse>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/title`,
    {
      method: "PATCH",
      body: JSON.stringify({ title }),
    },
  )
}

export async function submitDag(
  dag: unknown,
  opts: {
    mutate?: boolean
    metadata?: Record<string, unknown>
    /** Phase 68 → 64-C-LOCAL integration: pass the target platform
     * profile name (e.g. "host_native", "aarch64") so the backend
     * resolver can decide LOCAL vs BUNDLE. When omitted, backend
     * falls back to hardware_manifest → host_native. */
    targetPlatform?: string
  } = {},
): Promise<DAGSubmitResponse> {
  return request<DAGSubmitResponse>("/dag", {
    method: "POST",
    body: JSON.stringify({
      dag,
      mutate: !!opts.mutate,
      metadata: opts.metadata,
      target_platform: opts.targetPlatform,
    }),
  })
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
  return request<NPIData>("/runtime/npi")
}

export async function updateNPIState(updates: { business_model?: string; current_phase_id?: string }) {
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(updates)) {
    if (v !== undefined) params.set(k, v)
  }
  return request<NPIData>(`/runtime/npi?${params.toString()}`, { method: "PUT" })
}

export async function updateNPIMilestone(milestoneId: string, status: string) {
  return request<NPIMilestone>(`/runtime/npi/milestones/${milestoneId}?status=${status}`, { method: "PATCH" })
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
  return request<TokenBudgetInfo>("/runtime/token-budget")
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
  return request<TokenBudgetInfo>(`/runtime/token-budget?${params.toString()}`, { method: "PUT" })
}

export async function resetTokenFreeze() {
  return request<{ status: string }>("/runtime/token-budget/reset", { method: "POST" })
}

// ─── Notifications ───

export type NotificationSeverity = "P1" | "P2" | "P3"

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
  // R9 row 2935 / 2946 (#315): orthogonal P1/P2/P3 tag from
  // backend.severity. ``null`` / absent for legacy callers.
  severity?: NotificationSeverity | null
}

export async function getNotifications(limit: number = 50, level?: string) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (level) params.set("level", level)
  return request<NotificationItem[]>(`/runtime/notifications?${params.toString()}`)
}

export async function markNotificationRead(id: string) {
  return request<{ status: string }>(`/runtime/notifications/${id}/read`, { method: "POST" })
}

export async function getUnreadCount() {
  return request<{ count: number }>("/runtime/notifications/unread-count")
}

// ─── Invoke (Singularity Sync) ───

export interface InvokeAction {
  type: "command" | "assign" | "retry" | "report" | "health" | "coach"
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
  // R20-B (2026-04-25): coach action — orchestrator-led guidance when
  // the planner has nothing real to do (empty workspace / stale PEP
  // HOLDs). `message` is the rendered text (LLM-generated or templated
  // fallback); `triggers` is the set of keys the frontend should mark
  // as already-coached in sessionStorage so the same triggers are
  // suppressed on the next INVOKE this session.
  message?: string
  triggers?: string[]
  pending_count?: number
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
  command?: string,
  /** R20-B: trigger keys already coached this session (sessionStorage-backed). */
  suppressCoach?: readonly string[],
): AsyncGenerator<InvokeEvent> {
  const qs = new URLSearchParams()
  if (command) qs.set("command", command)
  if (suppressCoach && suppressCoach.length > 0) {
    qs.set("suppress_coach", suppressCoach.join(","))
  }
  const params = qs.toString()
  const res = await fetch(
    `${API_V1}/invoke/stream${params ? `?${params}` : ""}`,
    { method: "POST", headers: { "Content-Type": "application/json" } },
  )
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
  // R20 Part A (2026-04-25): per-decision coaching content rendered as
  // an inline expandable card on the PEP toast — explains in plain
  // English what the tool does, why the call was held, and what each
  // button does. Backend builds this in `pep_gateway._build_coaching`
  // when `kind === "pep_tool_intercept"`; absent for other decision
  // kinds (operator gating, cost gating, etc.).
  coaching?: {
    what: string
    why: string
    if_approve: string
    if_reject: string
  }
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

// ─── R0 (#306) PEP Gateway ───────────────────────────────────

export type PepAction = "auto_allow" | "hold" | "deny"
export type PepImpactScope = "local" | "prod" | "destructive" | ""

export interface PepDecisionEvent {
  id: string
  ts: number
  agent_id: string
  tool: string
  command: string
  tier: string
  action: PepAction
  rule: string
  reason: string
  impact_scope: PepImpactScope
  decision_id: string | null
  degraded: boolean
  timestamp?: string
  _broadcast_scope?: string
  _session_id?: string
  _tenant_id?: string
}

export interface PepStats {
  auto_allowed: number
  held: number
  denied: number
  total: number
}

export interface PepBreakerStatus {
  open: boolean
  consecutive_failures: number
  opened_at: number
  last_failure: number
  last_reason: string
  cooldown_remaining: number
}

export interface PepLiveSnapshot {
  recent: PepDecisionEvent[]
  held: PepDecisionEvent[]
  stats: PepStats
  breaker: PepBreakerStatus
}

export async function getPepLive(limit = 100): Promise<PepLiveSnapshot> {
  return request<PepLiveSnapshot>(`/pep/live?limit=${limit}`)
}

export async function listPepDecisions(limit = 100) {
  return request<{ items: PepDecisionEvent[]; count: number }>(
    `/pep/decisions?limit=${limit}`,
  )
}

export async function getPepHeld() {
  return request<{ items: PepDecisionEvent[]; count: number }>(`/pep/held`)
}

export async function getPepPolicy() {
  return request<{
    tiers: { t1: string[]; t2: string[]; t3: string[] }
    destructive_rule_count: number
    prod_hold_rule_count: number
    destructive_rules: string[]
    prod_hold_rules: string[]
  }>("/pep/policy")
}

export async function getPepStatus() {
  return request<{ breaker: PepBreakerStatus; stats: PepStats; held_count: number }>(
    "/pep/status",
  )
}

export async function resetPepBreaker() {
  return request<{ ok: boolean; breaker: PepBreakerStatus }>(
    "/pep/breaker/reset",
    { method: "POST" },
  )
}

// ─── R1 (#307) ChatOps Interactive ───────────────────────────────

export type ChatOpsDirection = "outbound" | "inbound"
export type ChatOpsChannel = "discord" | "teams" | "line" | "dashboard"

export interface ChatOpsButton {
  id: string
  label: string
  style?: "primary" | "secondary" | "danger" | "success"
  value?: string
}

export interface ChatOpsMessageEvent {
  id: string
  ts: number
  direction: ChatOpsDirection
  channel: ChatOpsChannel | string
  title?: string
  body?: string
  author?: string
  user_id?: string
  kind?: string
  button_id?: string
  command?: string
  command_args?: string
  buttons?: ChatOpsButton[]
  meta?: Record<string, unknown>
  errors?: string[]
  timestamp?: string
}

export interface ChatOpsAdapterStatus {
  configured: boolean
  reason: string
}

export interface ChatOpsMirrorSnapshot {
  items: ChatOpsMessageEvent[]
  status: Record<string, ChatOpsAdapterStatus>
}

export async function getChatOpsMirror(limit = 100): Promise<ChatOpsMirrorSnapshot> {
  return request<ChatOpsMirrorSnapshot>(`/chatops/mirror?limit=${limit}`)
}

export async function getChatOpsStatus() {
  return request<{
    adapters: Record<string, ChatOpsAdapterStatus>
    buttons: string[]
    commands: string[]
    pending_hints: Array<{ agent_id: string; text: string; author: string; channel: string; ts: number }>
  }>("/chatops/status")
}

export async function injectAgentHint(agent_id: string, text: string, author = "dashboard") {
  return request<{ ok: boolean; hint: { agent_id: string; text: string; author: string; channel: string; ts: number } }>(
    "/chatops/inject",
    { method: "POST", body: JSON.stringify({ agent_id, text, author }) },
  )
}

export async function sendChatOpsInteractive(
  channel: string, body: string,
  opts: { title?: string; buttons?: ChatOpsButton[]; meta?: Record<string, unknown> } = {},
) {
  return request<{ ok: boolean; message: ChatOpsMessageEvent }>(
    "/chatops/send",
    {
      method: "POST",
      body: JSON.stringify({
        channel, body,
        title: opts.title ?? "OmniSight",
        buttons: opts.buttons ?? [],
        meta: opts.meta ?? {},
      }),
    },
  )
}

export async function decidePepFromChatOps(pep_id: string, decision: "approve" | "reject") {
  return request<{ ok: boolean; pep_id: string; decision: DecisionPayload }>(
    `/pep/decision/${encodeURIComponent(pep_id)}`,
    { method: "POST", body: JSON.stringify({ decision }) },
  )
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
  return request<PipelineTimeline>("/runtime/pipeline/timeline")
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

// ─── Project Report (B6/UX-04) ───

export interface ReportResponse {
  report_id: string
  title: string
  generated_at: string
  markdown: string
}

export interface ShareReportResponse {
  url: string
  expires_in: number
}

export async function generateReport(runId: string, title?: string): Promise<ReportResponse> {
  return request<ReportResponse>("/report/generate", {
    method: "POST",
    body: JSON.stringify({ run_id: runId, title }),
  })
}

export async function getReport(reportId: string): Promise<ReportResponse> {
  return request<ReportResponse>(`/report/${encodeURIComponent(reportId)}`)
}

export async function shareReport(
  reportId: string,
  baseUrl?: string,
  expiresIn?: number,
): Promise<ShareReportResponse> {
  return request<ShareReportResponse>("/report/share", {
    method: "POST",
    body: JSON.stringify({
      report_id: reportId,
      base_url: baseUrl ?? "",
      expires_in: expiresIn ?? 86400,
    }),
  })
}

// ─── API Keys (K6) ─────────────────────────────────────────

export interface ApiKeyItem {
  id: string
  name: string
  key_prefix: string
  scopes: string[]
  created_by: string
  last_used_ip: string | null
  last_used_at: number | null
  enabled: boolean
  created_at: string
}

export async function listApiKeys(): Promise<{ items: ApiKeyItem[]; count: number }> {
  return request<{ items: ApiKeyItem[]; count: number }>("/api-keys")
}

export async function createApiKey(name: string, scopes: string[]): Promise<{ key: ApiKeyItem; secret: string }> {
  return request<{ key: ApiKeyItem; secret: string }>("/api-keys", {
    method: "POST",
    body: JSON.stringify({ name, scopes }),
  })
}

export async function rotateApiKey(keyId: string): Promise<{ key: ApiKeyItem; secret: string }> {
  return request<{ key: ApiKeyItem; secret: string }>(`/api-keys/${encodeURIComponent(keyId)}/rotate`, {
    method: "POST",
  })
}

export async function revokeApiKey(keyId: string): Promise<{ revoked: boolean; id: string }> {
  return request<{ revoked: boolean; id: string }>(`/api-keys/${encodeURIComponent(keyId)}/revoke`, {
    method: "POST",
  })
}

export async function enableApiKey(keyId: string): Promise<{ enabled: boolean; id: string }> {
  return request<{ enabled: boolean; id: string }>(`/api-keys/${encodeURIComponent(keyId)}/enable`, {
    method: "POST",
  })
}

export async function deleteApiKey(keyId: string): Promise<{ deleted: boolean; id: string }> {
  return request<{ deleted: boolean; id: string }>(`/api-keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
  })
}

export async function updateApiKeyScopes(keyId: string, scopes: string[]): Promise<{ id: string; scopes: string[] }> {
  return request<{ id: string; scopes: string[] }>(`/api-keys/${encodeURIComponent(keyId)}/scopes`, {
    method: "PATCH",
    body: JSON.stringify({ scopes }),
  })
}

// ─── L1 — Bootstrap wizard ─────────────────────────────────────────────────

export interface BootstrapGates {
  admin_password_default: boolean
  llm_provider_configured: boolean
  cf_tunnel_configured: boolean
  smoke_passed: boolean
}

export interface BootstrapStatusResponse {
  status: BootstrapGates
  all_green: boolean
  finalized: boolean
  missing_steps: string[]
}

export interface BootstrapFinalizeResponse {
  finalized: boolean
  status: BootstrapGates
  actor_user_id: string
}

export async function getBootstrapStatus(): Promise<BootstrapStatusResponse> {
  return request<BootstrapStatusResponse>("/bootstrap/status")
}

export async function finalizeBootstrap(reason?: string): Promise<BootstrapFinalizeResponse> {
  return request<BootstrapFinalizeResponse>("/bootstrap/finalize", {
    method: "POST",
    body: JSON.stringify(reason ? { reason } : {}),
  })
}

// ─── L2 — Step 1 (force admin password rotation) ───────────────────

export interface BootstrapAdminPasswordResponse {
  status: string
  admin_password_default: boolean
  user_id: string
}

/**
 * Machine-readable kinds emitted by ``POST /bootstrap/admin-password``.
 * Each maps to a distinct wizard banner so the operator sees a targeted
 * remediation path rather than a generic "something failed" string.
 */
export type BootstrapAdminPasswordKind =
  | "password_too_short"
  | "password_too_weak"
  | "current_password_wrong"
  | "already_rotated"

/**
 * Typed error raised by {@link bootstrapSetAdminPassword} on any backend
 * error response. Carries the ``kind`` tag + server-supplied ``detail``
 * so the UI can pick a matching banner without parsing the detail
 * string.
 */
export class BootstrapAdminPasswordError extends Error {
  kind: BootstrapAdminPasswordKind
  detail: string
  status: number
  constructor(
    kind: BootstrapAdminPasswordKind,
    detail: string,
    status: number,
  ) {
    super(detail)
    this.name = "BootstrapAdminPasswordError"
    this.kind = kind
    this.detail = detail
    this.status = status
  }
}

function _isAdminPwKind(v: unknown): v is BootstrapAdminPasswordKind {
  return (
    v === "password_too_short" ||
    v === "password_too_weak" ||
    v === "current_password_wrong" ||
    v === "already_rotated"
  )
}

/**
 * User-facing copy for each {@link BootstrapAdminPasswordKind}. Keep
 * these short — they render in a ≤3-line banner and sit alongside the
 * server-supplied ``detail`` which typically carries the zxcvbn warning
 * + suggestions for the ``password_too_weak`` path.
 */
export const BOOTSTRAP_ADMIN_PASSWORD_KIND_COPY: Record<
  BootstrapAdminPasswordKind,
  { title: string; hint: string }
> = {
  password_too_short: {
    title: "New password too short",
    hint: "Server enforces the 12-character minimum before hashing. Extend the password and submit again.",
  },
  password_too_weak: {
    title: "New password too guessable",
    hint: "Server re-ran zxcvbn and scored the password below the K7 threshold. Mix classes (upper/lower/digit/symbol), avoid dictionary words, and try again.",
  },
  current_password_wrong: {
    title: "Current password rejected",
    hint: "The shipping default is `omnisight-admin`. If you've already rotated it elsewhere, use that rotated credential — the wizard will not accept a bypass.",
  },
  already_rotated: {
    title: "Admin password already rotated",
    hint: "No admin still carries the must_change_password flag. Refresh the wizard — Step 1 should already be green.",
  },
}

export async function bootstrapSetAdminPassword(
  currentPassword: string,
  newPassword: string,
): Promise<BootstrapAdminPasswordResponse> {
  const method = "POST"
  const baseHeaders: Record<string, string> = {
    "Content-Type": "application/json",
  }
  if (_currentTenantId) baseHeaders["X-Tenant-Id"] = _currentTenantId
  if (typeof document !== "undefined") {
    const csrf = readCookie("omnisight_csrf")
    if (csrf) baseHeaders["X-CSRF-Token"] = csrf
  }
  // Straight fetch — the shared ``request<T>`` helper buries the
  // structured error body inside the Error message. For admin-password
  // we want the ``{kind, detail}`` payload so the UI can pick a
  // banner per kind (weak vs short vs wrong vs already_rotated).
  let res: Response
  try {
    res = await fetch(`${API_V1}/bootstrap/admin-password`, {
      method,
      credentials: "include",
      headers: baseHeaders,
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    })
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    // Network-unreachable is not one of the classified kinds — surface as
    // plain Error so the caller falls back to the generic error display.
    throw new Error(`Cannot reach OmniSight API: ${msg}`)
  }
  if (!res.ok) {
    let kind: BootstrapAdminPasswordKind | null = null
    let detail = `API ${res.status}`
    try {
      const body = await res.json()
      if (_isAdminPwKind(body?.kind)) kind = body.kind
      if (typeof body?.detail === "string" && body.detail.trim()) {
        detail = body.detail
      }
    } catch {
      try {
        const text = await res.text()
        if (text.trim()) detail = text.trim()
      } catch {
        /* ignore */
      }
    }
    if (kind !== null) {
      throw new BootstrapAdminPasswordError(kind, detail, res.status)
    }
    throw new Error(`API ${res.status}: ${detail}`)
  }
  return (await res.json()) as BootstrapAdminPasswordResponse
}

// ─── L3 — Step 2 Ollama local reachability probe ───────────────────

export interface BootstrapOllamaDetectResponse {
  reachable: boolean
  base_url: string
  latency_ms: number
  models: string[]
  kind: string
  detail: string
}

export async function bootstrapDetectOllama(
  baseUrl?: string,
): Promise<BootstrapOllamaDetectResponse> {
  const qs = baseUrl ? `?base_url=${encodeURIComponent(baseUrl)}` : ""
  return request<BootstrapOllamaDetectResponse>(`/bootstrap/ollama-detect${qs}`)
}

// ─── L3 — Step 2 LLM provider provisioning ─────────────────────────

export type BootstrapLlmProvisionKind =
  | "key_invalid"
  | "quota_exceeded"
  | "network_unreachable"
  | "bad_request"
  | "provider_error"

export interface BootstrapLlmProvisionRequest {
  provider: "anthropic" | "openai" | "ollama" | "azure"
  api_key?: string
  model?: string
  base_url?: string
  azure_deployment?: string
}

export interface BootstrapLlmProvisionResponse {
  status: string
  provider: string
  model: string
  fingerprint: string
  latency_ms: number
  models: string[]
}

/**
 * Typed error raised by {@link bootstrapLlmProvision} when the backend's
 * `provider.ping()` verdict is anything other than success. Carries the
 * machine-readable ``kind`` so the UI can pick a matching banner copy +
 * icon without parsing ``detail`` strings.
 */
export class BootstrapLlmProvisionError extends Error {
  kind: BootstrapLlmProvisionKind
  detail: string
  status: number
  constructor(kind: BootstrapLlmProvisionKind, detail: string, status: number) {
    super(detail)
    this.name = "BootstrapLlmProvisionError"
    this.kind = kind
    this.detail = detail
    this.status = status
  }
}

function _isProvisionKind(v: unknown): v is BootstrapLlmProvisionKind {
  return (
    v === "key_invalid" ||
    v === "quota_exceeded" ||
    v === "network_unreachable" ||
    v === "bad_request" ||
    v === "provider_error"
  )
}

export async function bootstrapLlmProvision(
  req: BootstrapLlmProvisionRequest,
): Promise<BootstrapLlmProvisionResponse> {
  const method = "POST"
  const baseHeaders: Record<string, string> = {
    "Content-Type": "application/json",
  }
  if (_currentTenantId) baseHeaders["X-Tenant-Id"] = _currentTenantId
  if (typeof document !== "undefined") {
    const csrf = readCookie("omnisight_csrf")
    if (csrf) baseHeaders["X-CSRF-Token"] = csrf
  }
  // Straight fetch — the shared ``request<T>`` helper retries 429/503 and
  // buries the response body inside the Error message. For provisioning
  // we want 1) no retry on quota_exceeded (429) so the operator gets an
  // immediate actionable message, and 2) the structured ``{kind, detail}``
  // payload the backend returns, not a flattened string.
  let res: Response
  try {
    res = await fetch(`${API_V1}/bootstrap/llm-provision`, {
      method,
      credentials: "include",
      headers: baseHeaders,
      body: JSON.stringify(req),
    })
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    throw new BootstrapLlmProvisionError(
      "network_unreachable",
      `Browser could not reach the OmniSight API — ${msg}`,
      0,
    )
  }
  if (!res.ok) {
    let kind: BootstrapLlmProvisionKind = "provider_error"
    let detail = `API ${res.status}`
    try {
      const body = await res.json()
      if (_isProvisionKind(body?.kind)) kind = body.kind
      if (typeof body?.detail === "string" && body.detail.trim()) detail = body.detail
    } catch {
      // Non-JSON body — fall back to raw text.
      try {
        const text = await res.text()
        if (text.trim()) detail = text.trim()
      } catch {
        /* ignore */
      }
    }
    throw new BootstrapLlmProvisionError(kind, detail, res.status)
  }
  return (await res.json()) as BootstrapLlmProvisionResponse
}

/**
 * User-facing copy for each {@link BootstrapLlmProvisionKind}. The wizard
 * uses the kind to choose a banner headline; the backend's ``detail`` is
 * shown beneath it for the precise reason (e.g. which provider, which
 * HTTP status). Keep these short — they render in a ≤3-line banner.
 */
export const BOOTSTRAP_PROVISION_KIND_COPY: Record<
  BootstrapLlmProvisionKind,
  { title: string; hint: string }
> = {
  key_invalid: {
    title: "API key rejected",
    hint: "The provider returned 401/403 — copy a fresh key from the provider dashboard and try again.",
  },
  quota_exceeded: {
    title: "Quota or rate limit exceeded",
    hint: "The provider returned 429 — wait for the quota to reset, or upgrade the account tier.",
  },
  network_unreachable: {
    title: "Cannot reach the provider",
    hint: "The network did not respond within 10s — check DNS, firewall, VPN, or an upstream proxy.",
  },
  bad_request: {
    title: "Request rejected",
    hint: "The provider rejected the request shape — check endpoint, deployment name, or base URL.",
  },
  provider_error: {
    title: "Provider error",
    hint: "The provider returned 5xx — retry in a few minutes or pick another provider.",
  },
}

/**
 * Provider → dashboard URL where a fresh API key can be minted. Shown
 * only on the ``key_invalid`` banner so the operator has a direct
 * one-click remediation path. ``ollama`` is intentionally absent — it
 * does not use keys and cannot emit ``key_invalid``.
 */
export const BOOTSTRAP_PROVIDER_KEY_URL: Record<string, string> = {
  anthropic: "https://console.anthropic.com/settings/keys",
  openai: "https://platform.openai.com/api-keys",
  azure: "https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI",
}

// ─── Y7 #283 — Step 2.5 (initialize organization: tenant + super-admin) ───

export type BootstrapInitTenantKind =
  | "invalid_display_name"
  | "invalid_slug"
  | "enterprise_license_required"
  | "password_too_short"
  | "password_too_weak"
  | "tenant_already_exists"
  | "non_default_tenant_already_exists"
  | "email_already_exists"
  | "validation_error"

export interface BootstrapInitTenantRequest {
  display_name: string
  plan: "free" | "starter" | "pro" | "enterprise"
  license_key?: string
  admin_email: string
  admin_password: string
  admin_name?: string
}

export interface BootstrapInitTenantResponse {
  status: string
  tenant_id: string
  tenant_name: string
  plan: string
  super_admin_user_id: string
  super_admin_email: string
  project_id: string
  env_write_warning: string
}

/**
 * Typed error raised by {@link bootstrapInitTenant} on any backend error
 * response. Carries the ``kind`` tag + server-supplied ``detail`` so the
 * UI can pick a matching banner without parsing the detail string.
 */
export class BootstrapInitTenantError extends Error {
  kind: BootstrapInitTenantKind
  detail: string
  status: number
  constructor(kind: BootstrapInitTenantKind, detail: string, status: number) {
    super(detail)
    this.name = "BootstrapInitTenantError"
    this.kind = kind
    this.detail = detail
    this.status = status
  }
}

function _isInitTenantKind(v: unknown): v is BootstrapInitTenantKind {
  return (
    v === "invalid_display_name" ||
    v === "invalid_slug" ||
    v === "enterprise_license_required" ||
    v === "password_too_short" ||
    v === "password_too_weak" ||
    v === "tenant_already_exists" ||
    v === "non_default_tenant_already_exists" ||
    v === "email_already_exists" ||
    v === "validation_error"
  )
}

export const BOOTSTRAP_INIT_TENANT_KIND_COPY: Record<
  BootstrapInitTenantKind,
  { title: string; hint: string }
> = {
  invalid_display_name: {
    title: "Organization name not usable",
    hint: "The name slugifies to an empty / too-short id. Use plain ASCII letters or digits — e.g. 'Acme Robotics' → t-acme-robotics.",
  },
  invalid_slug: {
    title: "Generated tenant id rejected",
    hint: "Pick a name whose slug starts with a letter or digit and is at least 2 chars. Hyphens are allowed; punctuation is stripped.",
  },
  enterprise_license_required: {
    title: "Enterprise license key required",
    hint: "The enterprise plan needs a license key (8-256 chars: letters, digits, dash, underscore). Pick a lower plan or paste your key.",
  },
  password_too_short: {
    title: "Super-admin password too short",
    hint: "Server enforces the 12-character minimum before hashing. Extend the password and submit again.",
  },
  password_too_weak: {
    title: "Super-admin password too guessable",
    hint: "Server re-ran zxcvbn and scored the password below the K7 threshold. Mix classes (upper/lower/digit/symbol), avoid dictionary words.",
  },
  tenant_already_exists: {
    title: "Tenant id already exists",
    hint: "Pick a different organization name — its slug collides with an existing tenant in the database.",
  },
  non_default_tenant_already_exists: {
    title: "Wizard Step 2.5 already ran",
    hint: "A non-default tenant already exists. Manage further tenants via /admin/tenants after finalize, not here.",
  },
  email_already_exists: {
    title: "Email already in use",
    hint: "A user row with this email already exists. Pick a different email for the super-admin, or use that account.",
  },
  validation_error: {
    title: "Request rejected",
    hint: "The wizard payload failed validation. Check the field shapes and try again.",
  },
}

/**
 * Y7 row 1 — initialize the operator's real tenant + super-admin during
 * the bootstrap wizard's Step 2.5. Optional step; skipping leaves the
 * install on ``t-default``. Side-effects on success: tenants row,
 * super_admin user, owner membership, default project, and a best-effort
 * ``OMNISIGHT_PRIMARY_TENANT_ID`` write into ``.env``.
 */
export async function bootstrapInitTenant(
  req: BootstrapInitTenantRequest,
): Promise<BootstrapInitTenantResponse> {
  const baseHeaders: Record<string, string> = {
    "Content-Type": "application/json",
  }
  if (_currentTenantId) baseHeaders["X-Tenant-Id"] = _currentTenantId
  if (typeof document !== "undefined") {
    const csrf = readCookie("omnisight_csrf")
    if (csrf) baseHeaders["X-CSRF-Token"] = csrf
  }
  let res: Response
  try {
    res = await fetch(`${API_V1}/bootstrap/init-tenant`, {
      method: "POST",
      credentials: "include",
      headers: baseHeaders,
      body: JSON.stringify(req),
    })
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    throw new Error(`Cannot reach OmniSight API: ${msg}`)
  }
  if (!res.ok) {
    let kind: BootstrapInitTenantKind = "validation_error"
    let detail = `API ${res.status}`
    try {
      const body = await res.json()
      if (_isInitTenantKind(body?.kind)) kind = body.kind
      if (typeof body?.detail === "string" && body.detail.trim()) {
        detail = body.detail
      }
    } catch {
      try {
        const text = await res.text()
        if (text.trim()) detail = text.trim()
      } catch {
        /* ignore */
      }
    }
    throw new BootstrapInitTenantError(kind, detail, res.status)
  }
  return (await res.json()) as BootstrapInitTenantResponse
}

// ─── L5 — Step 4 (parallel health check / 4 live ticks) ───────────

export type BootstrapHealthCheckStatus = "green" | "red" | "skipped"

export interface BootstrapHealthCheckResult {
  ok: boolean
  status: BootstrapHealthCheckStatus
  detail: string | null
  latency_ms: number | null
}

export interface BootstrapParallelHealthCheckResponse {
  all_green: boolean
  elapsed_ms: number
  backend: BootstrapHealthCheckResult
  frontend: BootstrapHealthCheckResult
  db_migration: BootstrapHealthCheckResult
  cf_tunnel: BootstrapHealthCheckResult
}

export interface BootstrapParallelHealthCheckRequest {
  timeout_secs?: number
  backend_url?: string
  frontend_url?: string
}

/**
 * Run the four Step-4 readiness probes in parallel
 * (backend / frontend / DB migration / CF tunnel connector).
 * The body is optional — bare POST uses backend defaults.
 */
export async function bootstrapParallelHealthCheck(
  req?: BootstrapParallelHealthCheckRequest,
): Promise<BootstrapParallelHealthCheckResponse> {
  return request<BootstrapParallelHealthCheckResponse>(
    "/bootstrap/parallel-health-check",
    {
      method: "POST",
      body: JSON.stringify(req ?? {}),
    },
  )
}

// ─── L6 — Step 5 (smoke test subset — compile-flash host_native + aarch64 DAGs) ──

export type BootstrapSmokeSubsetKey = "dag1" | "dag2" | "both"

export interface BootstrapSmokeSubsetRunSummary {
  /** Catalogue key ("dag1" or "dag2") — empty string on legacy backends. */
  key: string
  label: string
  dag_id: string
  ok: boolean
  validation_errors: Array<{
    rule: string
    task_id: string | null
    message: string
  }>
  run_id: string | null
  plan_id: number | null
  plan_status: string | null
  task_count: number
  t3_runner: string | null
  target_platform: string | null
}

export interface BootstrapSmokeAuditSummary {
  ok: boolean
  first_bad_id: number | null
  detail: string
  /** Total tenants whose audit chain was verified. */
  tenant_count: number
  /** Tenant ids whose chain verification failed (empty when ok=true). */
  bad_tenants: string[]
}

export interface BootstrapSmokeSubsetResponse {
  smoke_passed: boolean
  subset: string
  elapsed_ms: number
  runs: BootstrapSmokeSubsetRunSummary[]
  audit_chain: BootstrapSmokeAuditSummary
}

/**
 * Run the wizard's L6 Step-5 smoke subset — runs the compile-flash
 * host_native DAG and/or the aarch64 cross-compile DAG from
 * ``scripts/prod_smoke_test.py`` plus a full audit hash-chain
 * verification. ``subset`` defaults to ``both`` so the wizard can
 * display run summaries for both DAGs; external callers can pin to
 * ``dag1`` to keep the fast ~60s path.
 */
export async function bootstrapSmokeSubset(
  subset: BootstrapSmokeSubsetKey = "both",
): Promise<BootstrapSmokeSubsetResponse> {
  return request<BootstrapSmokeSubsetResponse>("/bootstrap/smoke-subset", {
    method: "POST",
    body: JSON.stringify({ subset }),
  })
}

// ─── L5 / L7 — Step 4 (start-services launcher + kind-keyed errors) ──

export type BootstrapStartServicesMode = "systemd" | "docker-compose" | "dev"

export interface BootstrapStartServicesRequest {
  mode?: string
  compose_file?: string
}

export interface BootstrapStartServicesResponse {
  status: string
  mode: BootstrapStartServicesMode
  command: string[]
  returncode: number
  stdout_tail: string
  stderr_tail: string
}

/**
 * Machine-readable kinds emitted by ``POST /bootstrap/start-services``.
 * Each maps to a distinct wizard banner so an operator whose sudoers
 * rule is missing sees a different remediation than one whose
 * docker-compose binary is absent.
 */
export type BootstrapStartServicesKind =
  | "bad_mode"
  | "binary_missing"
  | "timeout"
  | "sudoers_missing"
  | "unit_missing"
  | "unit_failed"

/**
 * Typed error raised by {@link bootstrapStartServices} on any backend
 * error response. Carries ``kind`` + server-supplied ``detail`` +
 * ``stderr_tail`` (when present) so the UI can render a targeted
 * banner with the raw failure tail for copy/paste debugging.
 */
export class BootstrapStartServicesError extends Error {
  kind: BootstrapStartServicesKind
  detail: string
  status: number
  mode: string
  command: string[]
  returncode: number | null
  stdout_tail: string
  stderr_tail: string
  constructor(init: {
    kind: BootstrapStartServicesKind
    detail: string
    status: number
    mode?: string
    command?: string[]
    returncode?: number | null
    stdout_tail?: string
    stderr_tail?: string
  }) {
    super(init.detail)
    this.name = "BootstrapStartServicesError"
    this.kind = init.kind
    this.detail = init.detail
    this.status = init.status
    this.mode = init.mode ?? ""
    this.command = init.command ?? []
    this.returncode = init.returncode ?? null
    this.stdout_tail = init.stdout_tail ?? ""
    this.stderr_tail = init.stderr_tail ?? ""
  }
}

function _isStartKind(v: unknown): v is BootstrapStartServicesKind {
  return (
    v === "bad_mode" ||
    v === "binary_missing" ||
    v === "timeout" ||
    v === "sudoers_missing" ||
    v === "unit_missing" ||
    v === "unit_failed"
  )
}

/**
 * User-facing copy for each {@link BootstrapStartServicesKind}. Remedy
 * strings deliberately link to concrete install artefacts ship with
 * the repo (``docs/ops/bootstrap_modes.md``, the sudoers snippet from
 * ``generate_sudoers_snippet()``) so the operator has a copy/paste
 * path out of the failure rather than a dead-end "try again".
 */
export const BOOTSTRAP_START_SERVICES_KIND_COPY: Record<
  BootstrapStartServicesKind,
  { title: string; hint: string }
> = {
  bad_mode: {
    title: "Unknown deploy mode",
    hint: "Only `systemd`, `docker-compose`, and `dev` are supported — check the auto-detection override or leave the field blank.",
  },
  binary_missing: {
    title: "Launcher binary not found on PATH",
    hint: "For systemd install `systemd` + `sudo`. For docker-compose install the Docker Engine + Compose v2. See docs/ops/bootstrap_modes.md.",
  },
  timeout: {
    title: "Launcher timed out",
    hint: "The launcher is still running after 120s — inspect `journalctl -u omnisight-backend` or `docker compose ps` on the host and retry once the service finishes.",
  },
  sudoers_missing: {
    title: "K1 sudoers grant missing",
    hint: "systemd mode needs a NOPASSWD rule for `systemctl start omnisight-*`. Install the snippet emitted by `generate_sudoers_snippet()` into `/etc/sudoers.d/omnisight-bootstrap` (validate with `visudo -c -f`).",
  },
  unit_missing: {
    title: "systemd unit not installed",
    hint: "`omnisight-backend.service` / `omnisight-frontend.service` were not found. Run `deploy/install_units.sh` (or copy the unit files from deploy/systemd/) and `systemctl daemon-reload`.",
  },
  unit_failed: {
    title: "Launcher exited with a non-zero code",
    hint: "Inspect stderr_tail below for the precise failure. Common fixes: clear a stale lock file, free the target port, re-run migrations.",
  },
}

/**
 * Launch the OmniSight services for the wizard's Step 4 (L5/L7). The
 * backend auto-detects the deploy mode (systemd / docker-compose / dev)
 * unless the caller pins one via ``mode``. On error the rejection
 * carries a {@link BootstrapStartServicesError} with ``kind`` so the
 * UI can pick a targeted banner (``sudoers_missing`` vs
 * ``binary_missing`` vs ``timeout`` etc.).
 */
export async function bootstrapStartServices(
  req?: BootstrapStartServicesRequest,
): Promise<BootstrapStartServicesResponse> {
  const method = "POST"
  const baseHeaders: Record<string, string> = {
    "Content-Type": "application/json",
  }
  if (_currentTenantId) baseHeaders["X-Tenant-Id"] = _currentTenantId
  if (typeof document !== "undefined") {
    const csrf = readCookie("omnisight_csrf")
    if (csrf) baseHeaders["X-CSRF-Token"] = csrf
  }
  let res: Response
  try {
    res = await fetch(`${API_V1}/bootstrap/start-services`, {
      method,
      credentials: "include",
      headers: baseHeaders,
      body: JSON.stringify(req ?? {}),
    })
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    throw new BootstrapStartServicesError({
      kind: "binary_missing",
      detail: `Cannot reach OmniSight API: ${msg}`,
      status: 0,
    })
  }
  if (!res.ok) {
    let kind: BootstrapStartServicesKind = "unit_failed"
    let detail = `API ${res.status}`
    let mode = ""
    let command: string[] = []
    let returncode: number | null = null
    let stdout_tail = ""
    let stderr_tail = ""
    try {
      const body = await res.json()
      if (_isStartKind(body?.kind)) kind = body.kind
      if (typeof body?.detail === "string" && body.detail.trim()) {
        detail = body.detail
      }
      if (typeof body?.mode === "string") mode = body.mode
      if (Array.isArray(body?.command)) command = body.command as string[]
      if (typeof body?.returncode === "number") returncode = body.returncode
      if (typeof body?.stdout_tail === "string") stdout_tail = body.stdout_tail
      if (typeof body?.stderr_tail === "string") stderr_tail = body.stderr_tail
    } catch {
      try {
        const text = await res.text()
        if (text.trim()) detail = text.trim()
      } catch {
        /* ignore */
      }
    }
    throw new BootstrapStartServicesError({
      kind,
      detail,
      status: res.status,
      mode,
      command,
      returncode,
      stdout_tail,
      stderr_tail,
    })
  }
  return (await res.json()) as BootstrapStartServicesResponse
}

// ─── L4 — Step 3 (Cloudflare Tunnel skip / LAN-only) ──────────────

export interface BootstrapCfTunnelSkipResponse {
  status: string
  cf_tunnel_configured: boolean
}

/**
 * Record an operator-driven "skip Cloudflare tunnel" decision during
 * wizard Step 3. The backend writes an audit row with warning
 * severity and marks the gate satisfied so finalize can proceed —
 * this is the documented LAN-only escape hatch, not a silent bypass.
 */
export async function bootstrapCfTunnelSkip(
  reason?: string,
): Promise<BootstrapCfTunnelSkipResponse> {
  return request<BootstrapCfTunnelSkipResponse>("/bootstrap/cf-tunnel-skip", {
    method: "POST",
    body: JSON.stringify({ reason: reason ?? "" }),
  })
}

// ─── BS.9.5 — Step 5.5 (Vertical setup commit, optional) ──────────

/** Mirror of ``components/omnisight/bootstrap-vertical-step.tsx``
 *  ``BootstrapVerticalId``. Re-typed inline to avoid a frontend → API
 *  client → component dependency cycle (the component imports from
 *  ``lib/api`` for ``createInstallJob``). */
export type BootstrapVerticalIdWire =
  | "mobile"
  | "embedded"
  | "web"
  | "software"
  | "cross-toolchain"

/** Mirror of ``AndroidApiSelection`` in
 *  ``components/omnisight/android-api-selector.tsx``. ``compile_target``
 *  / ``min_api`` are restricted to the closed set of supported levels
 *  by the component before they reach this wire shape. */
export interface BootstrapVerticalAndroidApi {
  compile_target: number
  min_api: number
  emulator_preset: string
  google_play_services: boolean
}

export interface BootstrapVerticalSetupRequest {
  verticals_selected: BootstrapVerticalIdWire[]
  install_job_ids: string[]
  android_api?: BootstrapVerticalAndroidApi | null
}

export interface BootstrapVerticalSetupResponse {
  status: string
  verticals_selected: BootstrapVerticalIdWire[]
  install_job_ids: string[]
}

/**
 * Canonical vertical → primary catalog ``entry_id`` map. The wizard's
 * BS.9.5 commit path POSTs ONE ``/installer/jobs`` per selected
 * vertical, picking each vertical's "headline" SDK so the install
 * drawer surfaces a single in-flight row per pick. Operators can add
 * the rest of the family from ``Settings → Platforms`` later. Pinned
 * to ids that ship via ``backend/alembic/versions/0052_catalog_seed.py``
 * — drift tests in BS.9.6 will lock this map against the seed.
 *
 *   mobile         → android-sdk-platform-tools (adb / fastboot;
 *                    BS.9.4 Android API selection rides on
 *                    ``metadata.android_api``)
 *   embedded       → espressif-esp-idf-v5     (most-deployed embedded SDK)
 *   web            → nodejs-lts-20            (every web stack needs Node)
 *   software       → python-uv                (fast-pull Python toolchain)
 *   cross-toolchain → arm-gnu-toolchain-13    (most-common ARM cross-gcc)
 */
export const BOOTSTRAP_VERTICAL_PRIMARY_ENTRY: Readonly<
  Record<BootstrapVerticalIdWire, string>
> = {
  mobile: "android-sdk-platform-tools",
  embedded: "espressif-esp-idf-v5",
  web: "nodejs-lts-20",
  software: "python-uv",
  "cross-toolchain": "arm-gnu-toolchain-13",
}

/**
 * BS.9.5 — Record the operator's BS.9.3 vertical pick + BS.9.4 Android
 * API config + the install_job ids the wizard already enqueued via
 * ``/installer/jobs``. The backend writes a single row into
 * ``bootstrap_state.metadata`` so audit / re-runs can see which jobs
 * the wizard kicked off. Re-commit overwrites the prior payload (the
 * underlying SQL runs ``ON CONFLICT DO UPDATE``) so re-opening the
 * step and adding a vertical the operator skipped on the first pass
 * is a non-destructive idempotent flow.
 *
 * The endpoint refuses payloads where ``android_api`` is omitted
 * while ``"mobile"`` is selected (or sent without it), so the wizard
 * UI never lands a half-built install metadata row in PG.
 */
export async function bootstrapRecordVerticalSetup(
  body: BootstrapVerticalSetupRequest,
): Promise<BootstrapVerticalSetupResponse> {
  return request<BootstrapVerticalSetupResponse>("/bootstrap/vertical-setup", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

// ─── BS.7.1 — Installer (POST /installer/jobs + PEP HOLD) ────────────────
//
// Wires the catalog-card "Install" button to the existing installer router
// (backend/routers/installer.py). The POST runs through the existing R20-A
// PEP gateway HOLD path automatically: ``tool="install_entry"`` is not on
// any tier whitelist, so ``pep_gateway.classify`` returns ``hold`` via
// the ``tier_unlisted`` rule and the request blocks until the operator
// approves / rejects via the global ToastCenter coaching card. On approve
// → 201 + queued job row. On deny / timeout → 403 ``pep_denied``. On
// idempotency_key collision (operator double-clicked, or retried from a
// stale tab) → 200 + the existing row (no second HOLD).
//
// The shape of ``InstallJob`` mirrors backend ``_row_to_install_job``
// 1:1 so frontend tests can assert against the same field names that
// ``backend/tests/test_installer_api.py`` does. ``createInstallJob``
// generates an idempotency_key when the caller omits one — a UUID v4 is
// 36 chars (matches the backend's 16..64 ASCII pattern).

export type InstallJobState =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"

export interface InstallJob {
  id: string
  tenant_id: string
  entry_id: string
  state: InstallJobState
  idempotency_key: string
  sidecar_id: string | null
  protocol_version: number
  bytes_done: number
  bytes_total: number | null
  eta_seconds: number | null
  log_tail: string
  result_json: Record<string, unknown> | null
  error_reason: string | null
  pep_decision_id: string | null
  requested_by: string
  queued_at: string
  claimed_at: string | null
  started_at: string | null
  completed_at: string | null
}

export interface CreateInstallJobOptions {
  /** Override the auto-generated idempotency_key. Pass the same value to
   *  retry without producing a second PEP HOLD or a duplicate row.
   *  Must match the backend's ``^[A-Za-z0-9_\-]{16,64}$`` pattern. */
  idempotencyKey?: string
  /** Optional ``bytes_total`` hint surfaced to the sidecar progress feed
   *  before the real download size is known (BS.7.4 SSE will overwrite). */
  bytesTotal?: number
  /** Free-form metadata stored on the install_jobs row. Used by the BS.7
   *  install pipeline to remember version-channel / vendor preferences. */
  metadata?: Record<string, unknown>
}

/** Generate a fresh idempotency_key for an install POST. Uses
 *  ``crypto.randomUUID`` when available (modern browsers + Node 19+);
 *  falls back to a 32-hex-char timestamp+random concat when the crypto
 *  binding is missing (jsdom test environment, very old browsers). */
export function generateInstallIdempotencyKey(): string {
  const c =
    typeof globalThis !== "undefined"
      ? (globalThis as { crypto?: Crypto }).crypto
      : undefined
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID()
  }
  // Fallback: 32-hex chars (timestamp + Math.random pad). Deterministic
  // entropy < UUID v4 but still well above the backend's 16-char floor.
  const ts = Date.now().toString(16).padStart(12, "0")
  const rand = Math.floor(Math.random() * 0xffffffffffff)
    .toString(16)
    .padStart(12, "0")
  const tail = Math.floor(Math.random() * 0xffffffff)
    .toString(16)
    .padStart(8, "0")
  return `${ts}${rand}${tail}`.slice(0, 32)
}

/** POST /installer/jobs — create an install job for the given catalog
 *  entry. Blocks until the PEP HOLD resolves (operator approves /
 *  rejects via the global ToastCenter, or the gateway times out at 10
 *  min). Returns the queued job row on approve, throws ``ApiError`` on
 *  PEP denial / timeout / idempotency_key cross-tenant collision. */
export async function createInstallJob(
  entryId: string,
  options?: CreateInstallJobOptions,
): Promise<InstallJob> {
  const body: {
    entry_id: string
    idempotency_key: string
    bytes_total?: number
    metadata: Record<string, unknown>
  } = {
    entry_id: entryId,
    idempotency_key:
      options?.idempotencyKey ?? generateInstallIdempotencyKey(),
    metadata: options?.metadata ?? {},
  }
  if (typeof options?.bytesTotal === "number") {
    body.bytes_total = options.bytesTotal
  }
  return request<InstallJob>("/installer/jobs", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** BS.7.6 — POST /installer/jobs/{id}/retry — clone a non-running
 *  install job into a fresh ``queued`` row. The source row must be in
 *  a terminal state (``failed`` / ``cancelled`` / ``completed``);
 *  retrying a still-active row 409s on the backend.
 *
 *  Like ``createInstallJob`` the retry POST goes through the same R20-A
 *  PEP gateway HOLD path, so the operator gets a fresh coaching card
 *  before the install actually starts. ``idempotencyKey`` defaults to a
 *  freshly-generated key — caller may pass one explicitly when guarding
 *  against double-click on the retry button itself. */
export async function retryInstallJob(
  jobId: string,
  options?: { idempotencyKey?: string },
): Promise<InstallJob> {
  const body = {
    idempotency_key:
      options?.idempotencyKey ?? generateInstallIdempotencyKey(),
  }
  return request<InstallJob>(
    `/installer/jobs/${encodeURIComponent(jobId)}/retry`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  )
}

/** BS.7.6 — GET /installer/jobs/{id} — fetch a single install job row
 *  including its full ``log_tail`` (max 4 KiB) and ``error_reason``.
 *  Used by the failed-state "view log" modal so the operator can read
 *  the tail even after the row has fallen out of the in-memory SSE
 *  snapshot (e.g. opening the page after the install already failed). */
export async function getInstallJob(jobId: string): Promise<InstallJob> {
  return request<InstallJob>(
    `/installer/jobs/${encodeURIComponent(jobId)}`,
    { method: "GET" },
  )
}

// ─── BS.8.2 — Installed entries + Cleanup unused ──────────────────────────
//
// The Installed tab (`<InstalledTab />`) needs a server-derived list of
// "currently installed" catalog entries — i.e. entries where the latest
// install_jobs row is state='completed' AND not flagged as an uninstall
// record. The backend's `GET /installer/installed` does that derivation
// (see `backend/routers/installer.py::list_installed_entries`); this client
// helper just unwraps the response.
//
// Bulk uninstall flows through the same R20-A PEP gateway HOLD path the
// install side uses, with `tool="uninstall_entry"` (also tier_unlisted →
// HOLD). One POST per click — backend dedupes the entry_ids and runs ONE
// PEP HOLD covering the whole batch, so the operator sees a single
// coaching card listing all selected entries instead of N cards.

/** A single installed catalog entry, as derived by the backend from the
 *  install_jobs table. Mirrors the `InstalledEntry` UI surface in
 *  `components/omnisight/installed-tab.tsx` (snake_case wire ↔ camelCase
 *  UI bridge happens in `useInstalledEntries`). */
export interface InstalledEntryRow {
  entry_id: string
  display_name: string
  vendor: string
  family: string
  version: string | null
  description: string | null
  disk_usage_bytes: number | null
  used_by_workspace_count: number
  last_used_at: string | null
  installed_at: string | null
  update_available: boolean
  available_version: string | null
  source: "shipped" | "operator" | "override" | "subscription" | null
}

export interface ListInstalledEntriesResponse {
  items: InstalledEntryRow[]
  count: number
}

/** GET /installer/installed — list installed catalog entries for the
 *  caller's tenant. Tenant scope + RBAC are handled by the request
 *  layer. */
export async function listInstalledEntries(): Promise<ListInstalledEntriesResponse> {
  return request<ListInstalledEntriesResponse>("/installer/installed", {
    method: "GET",
  })
}

/** Per-entry result returned by the bulk-uninstall endpoint. ``action``
 *  is ``"approved"`` when the PEP HOLD resolved to allow + the row was
 *  inserted with ``state='completed'``; ``"denied"`` when PEP rejected
 *  the batch (every entry in the batch shares the same denial reason);
 *  ``"error"`` for the future case where a per-entry validation fails
 *  even though the batch was approved (today the dedupe + regex check
 *  happens before the HOLD so this code path is reserved). */
export interface BulkUninstallResultItem {
  entry_id: string
  job_id: string
  action: "approved" | "denied" | "error"
  state: "completed" | "cancelled" | "failed"
  reason: string | null
  pep_decision_id: string | null
}

export interface BulkUninstallResponse {
  items: BulkUninstallResultItem[]
  approved_count: number
  denied_count: number
  pep_decision_id: string | null
}

export interface BulkUninstallOptions {
  /** Optional free-text justification recorded on every audit row +
   *  surfaced to the PEP coaching card. Capped at 256 chars on the
   *  backend (BulkUninstallBody.reason). */
  reason?: string | null
}

/** POST /installer/uninstall — one PEP HOLD covering every entry id in
 *  the batch. On approve → 200 + per-entry approved rows. On deny → 403
 *  with the PEP rule on every entry's ``reason`` so the
 *  ``<ApiErrorToastCenter />`` can surface a single denial card. */
export async function bulkUninstallEntries(
  entryIds: ReadonlyArray<string>,
  options?: BulkUninstallOptions,
): Promise<BulkUninstallResponse> {
  const body: { entry_ids: string[]; reason?: string } = {
    entry_ids: [...entryIds],
  }
  if (typeof options?.reason === "string" && options.reason.length > 0) {
    body.reason = options.reason
  }
  return request<BulkUninstallResponse>("/installer/uninstall", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** Response shape for ``GET /installer/installed/{entry_id}/dependents``.
 *  Every item shares the same ``InstalledEntryRow`` shape the
 *  cleanup-unused list returns so the frontend can reuse the existing
 *  snake→camel marshaller (``installedEntryFromRow``). */
export interface ListEntryDependentsResponse {
  entry_id: string
  items: InstalledEntryRow[]
  count: number
}

/** GET /installer/installed/{entry_id}/dependents — list currently-
 *  installed entries that declare *entryId* as a dependency. Used by
 *  BS.8.4's per-row uninstall confirm modal to surface a hard warning
 *  when other installed entries still depend on the one the operator
 *  is about to remove.
 *
 *  Empty list ⇒ no dependents ⇒ the modal proceeds without a warning.
 *  Any non-empty list ⇒ the modal renders the dependents and forces
 *  the operator to explicitly confirm the destructive action.
 *
 *  Throws :class:`ApiError` on:
 *    • 422 — malformed entry_id (does not match
 *      ``backend/routers/installer.ENTRY_ID_PATTERN``).
 *    • 403 — caller lacks ``operator`` role. */
export async function listEntryDependents(
  entryId: string,
): Promise<ListEntryDependentsResponse> {
  return request<ListEntryDependentsResponse>(
    `/installer/installed/${encodeURIComponent(entryId)}/dependents`,
    { method: "GET" },
  )
}

/** 30 days, in milliseconds. Anything idle longer than this surfaces in
 *  the cleanup-unused candidate list. The threshold lives next to the
 *  helper so tests can rely on a single source of truth, and so a
 *  future "configurable per tenant" rollout has a single migration
 *  point. */
export const INSTALLED_ENTRY_IDLE_THRESHOLD_MS = 30 * 24 * 60 * 60 * 1000

/** Predicate used by the cleanup-unused modal to decide whether an
 *  installed entry is a 30-day-idle candidate.
 *
 *  An entry is a candidate when:
 *    1. ``usedByWorkspaceCount`` is zero (or unknown) — entries with
 *       active dependants are never candidates regardless of when they
 *       were last touched. BS.8.4 layers a hard "depends-on" gate on
 *       top of this for the per-row uninstall path; the cleanup modal
 *       conservatively excludes them too.
 *    2. ``lastUsedAt`` is older than 30 days, OR is missing AND
 *       ``installedAt`` is older than 30 days, OR both are missing
 *       (operator-installed before BS.8 started recording timestamps,
 *       so we err on the side of "show it so the operator can decide").
 *
 *  ``now`` is injectable so unit tests can pin the clock. */
export function isCleanupCandidate(
  entry: {
    lastUsedAt?: string | null
    installedAt?: string | null
    usedByWorkspaceCount?: number
  },
  now: Date = new Date(),
): boolean {
  const used = entry.usedByWorkspaceCount ?? 0
  if (used > 0) return false
  const ref = entry.lastUsedAt || entry.installedAt
  if (!ref) {
    // No timestamps at all — surface as a candidate so the operator
    // can make the call manually.
    return true
  }
  const t = Date.parse(ref)
  if (!Number.isFinite(t)) return true
  const idleMs = now.getTime() - t
  return idleMs >= INSTALLED_ENTRY_IDLE_THRESHOLD_MS
}

// ─── BS.8.5 — Catalog sources (catalog_subscriptions) admin CRUD ──────────
//
// The Sources tab (`<SourcesTab />`) needs admin-only CRUD over the
// per-tenant feed of third-party catalogs:
//   • GET    /catalog/sources               — list this tenant's subscriptions
//   • POST   /catalog/sources               — add a new subscription
//   • PATCH  /catalog/sources/{sub_id}      — patch (rename URL, toggle enabled,
//                                             change refresh interval, …)
//   • DELETE /catalog/sources/{sub_id}      — remove
//   • POST   /catalog/sources/{sub_id}/sync — request immediate refresh
//
// Wire shape mirrors `_row_to_subscription()` on the backend
// (backend/routers/catalog.py). Auth is `require_admin` on every route — the
// global `<ApiErrorToastCenter />` handles the 403 path automatically.

/** Auth method literal mirrors the alembic 0051 CHECK constraint
 *  (`auth_method IN ('none','basic','bearer','signed_url')`). */
export type CatalogSourceAuthMethod =
  | "none"
  | "basic"
  | "bearer"
  | "signed_url"

/** Wire shape for `_row_to_subscription()`. Snake_case as returned by
 *  FastAPI; the `<SourcesTab />` consumes this directly (no marshaller —
 *  one shape, one consumer). */
export interface CatalogSource {
  id: string
  tenant_id: string
  feed_url: string
  auth_method: CatalogSourceAuthMethod
  auth_secret_ref: string | null
  refresh_interval_s: number
  last_synced_at: string | null
  last_sync_status: string | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface ListCatalogSourcesResponse {
  items: CatalogSource[]
  count: number
}

/** Refresh-interval bounds mirror `SubscriptionCreate.refresh_interval_s`
 *  on the backend (`Field(ge=60, le=30 * 86400)`). Exported so the form
 *  validation in `<SourcesTab />` and its tests share one source of truth. */
export const CATALOG_SOURCE_REFRESH_MIN_S = 60
export const CATALOG_SOURCE_REFRESH_MAX_S = 30 * 86400  // 30 days
export const CATALOG_SOURCE_REFRESH_DEFAULT_S = 86400  // 24 hours
export const CATALOG_SOURCE_FEED_URL_MAX_LEN = 2048
export const CATALOG_SOURCE_AUTH_SECRET_REF_MAX_LEN = 256
export const CATALOG_SOURCE_AUTH_METHODS: ReadonlyArray<CatalogSourceAuthMethod> = [
  "none",
  "basic",
  "bearer",
  "signed_url",
]

export interface CreateCatalogSourceRequest {
  feed_url: string
  auth_method?: CatalogSourceAuthMethod
  auth_secret_ref?: string | null
  refresh_interval_s?: number
  enabled?: boolean
}

export interface PatchCatalogSourceRequest {
  feed_url?: string
  auth_method?: CatalogSourceAuthMethod
  auth_secret_ref?: string | null
  refresh_interval_s?: number
  enabled?: boolean
}

export interface DeleteCatalogSourceResponse {
  status: "deleted"
  id: string
  tenant_id: string
}

/** GET /catalog/sources — list the caller's tenant's catalog
 *  subscriptions. Pass ``enabledOnly=true`` to ask the backend to filter
 *  rows where ``enabled = TRUE``; the default returns the full list so
 *  the admin UI can render disabled rows greyed out instead of hiding
 *  them. */
export async function listCatalogSources(
  options?: { enabledOnly?: boolean },
): Promise<ListCatalogSourcesResponse> {
  const qs = options?.enabledOnly ? "?enabled_only=true" : ""
  return request<ListCatalogSourcesResponse>(`/catalog/sources${qs}`, {
    method: "GET",
  })
}

/** POST /catalog/sources — add a new subscription. Throws
 *  :class:`ApiError` with ``status=409`` when the (tenant, feed_url)
 *  unique constraint is violated, ``status=422`` when the body fails
 *  validation (e.g. whitespace in ``auth_secret_ref``). */
export async function createCatalogSource(
  body: CreateCatalogSourceRequest,
): Promise<CatalogSource> {
  return request<CatalogSource>("/catalog/sources", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** PATCH /catalog/sources/{sub_id} — partial update. The backend
 *  rejects an empty body with 422; the frontend should only call this
 *  with at least one field set. */
export async function patchCatalogSource(
  subId: string,
  body: PatchCatalogSourceRequest,
): Promise<CatalogSource> {
  return request<CatalogSource>(
    `/catalog/sources/${encodeURIComponent(subId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    },
  )
}

/** DELETE /catalog/sources/{sub_id} — hard-delete (no tombstone).
 *  Throws :class:`ApiError` with ``status=404`` when the row does not
 *  exist or belongs to a different tenant. */
export async function deleteCatalogSource(
  subId: string,
): Promise<DeleteCatalogSourceResponse> {
  return request<DeleteCatalogSourceResponse>(
    `/catalog/sources/${encodeURIComponent(subId)}`,
    { method: "DELETE" },
  )
}

/** POST /catalog/sources/{sub_id}/sync — request an immediate refresh.
 *  Backend stamps ``last_sync_status='pending_manual'`` and clears
 *  ``last_synced_at`` so the row jumps to the top of the feed-sync cron
 *  queue. The response is the updated subscription row. */
export async function syncCatalogSource(
  subId: string,
): Promise<CatalogSource> {
  return request<CatalogSource>(
    `/catalog/sources/${encodeURIComponent(subId)}/sync`,
    { method: "POST" },
  )
}

/** Coerce a free-text feed URL input to a normalised string for
 *  validation. Only trims surrounding whitespace — does not touch the
 *  scheme / path / query so the backend sees the operator's exact intent.
 *  Exported for unit tests + the form to share one definition. */
export function normaliseCatalogSourceFeedUrl(raw: string): string {
  return raw.trim()
}

/** Validate a candidate feed URL on the frontend so an obviously-bad
 *  input fails before the round-trip. The backend still owns the final
 *  word (length / uniqueness / scheme), but a free regex here keeps the
 *  form responsive. Returns ``null`` when valid, or a short user-facing
 *  message when not. */
export function validateCatalogSourceFeedUrl(raw: string): string | null {
  const v = normaliseCatalogSourceFeedUrl(raw)
  if (v.length === 0) return "feed URL is required"
  if (v.length > CATALOG_SOURCE_FEED_URL_MAX_LEN) {
    return `feed URL must be ≤ ${CATALOG_SOURCE_FEED_URL_MAX_LEN} chars`
  }
  // Accept http(s):// only — the backend also accepts these in
  // production; admins who need an exotic scheme can curl directly.
  if (!/^https?:\/\//i.test(v)) {
    return "feed URL must start with http:// or https://"
  }
  return null
}

/** Validate the auth_secret_ref input. The backend rejects whitespace
 *  via a Pydantic field_validator; we mirror that here for instant
 *  feedback. Empty / null values are allowed (no auth ref). */
export function validateCatalogSourceAuthSecretRef(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null
  if (raw.length === 0) return null
  if (raw.length > CATALOG_SOURCE_AUTH_SECRET_REF_MAX_LEN) {
    return `auth_secret_ref must be ≤ ${CATALOG_SOURCE_AUTH_SECRET_REF_MAX_LEN} chars`
  }
  if (/\s/.test(raw)) {
    return "auth_secret_ref must be a secret-store reference, not a literal secret (no whitespace)"
  }
  return null
}

/** Validate the refresh_interval_s value. Mirrors the backend's
 *  ``Field(ge=60, le=30 * 86400)`` so the form fails fast. */
export function validateCatalogSourceRefreshInterval(
  seconds: number,
): string | null {
  if (!Number.isFinite(seconds)) return "refresh interval must be a number"
  if (!Number.isInteger(seconds)) return "refresh interval must be an integer"
  if (seconds < CATALOG_SOURCE_REFRESH_MIN_S) {
    return `refresh interval must be ≥ ${CATALOG_SOURCE_REFRESH_MIN_S}s (1 min)`
  }
  if (seconds > CATALOG_SOURCE_REFRESH_MAX_S) {
    return `refresh interval must be ≤ ${CATALOG_SOURCE_REFRESH_MAX_S}s (30 days)`
  }
  return null
}

// ─── BS.8.6 — Catalog entry admin CRUD (custom entries) ───────────────────
//
// The Custom tab (`<CustomEntryForm />`) needs admin-only CRUD over the
// per-tenant catalog_entries surface where `source ∈ {operator, override}`.
// The backend routes already exist and guard with `require_admin`:
//   • GET    /catalog/entries[?...]      — operator-readable, used to list
//                                          existing entries + populate the
//                                          depends_on multi-select.
//   • POST   /catalog/entries            — admin only (operator/override).
//   • PATCH  /catalog/entries/{id}       — admin only (override layer or
//                                          in-place update on existing
//                                          operator/override rows).
//   • DELETE /catalog/entries/{id}       — admin only (soft-delete /
//                                          tombstone).
//
// Wire shapes mirror `_row_to_entry()` on the backend
// (backend/routers/catalog.py:305) — snake_case directly off the wire so
// the form can pass through without a marshaller.

/** Catalog entry families per backend `ENTRY_FAMILIES` (alembic 0051
 *  CHECK constraint). The UI catalog-tab `<CatalogTab />` collapses
 *  `rtos` and `cross-toolchain` into the 5-bucket palette, but the admin
 *  custom form must surface every backend-accepted family so an admin
 *  can create RTOS or cross-toolchain entries explicitly. */
export const CATALOG_ENTRY_FAMILIES: ReadonlyArray<string> = [
  "mobile",
  "embedded",
  "web",
  "software",
  "rtos",
  "cross-toolchain",
  "custom",
]
export type CatalogEntryFamily = (typeof CATALOG_ENTRY_FAMILIES)[number]

/** Catalog entry install methods per backend `ENTRY_INSTALL_METHODS`. */
export const CATALOG_ENTRY_INSTALL_METHODS: ReadonlyArray<string> = [
  "noop",
  "docker_pull",
  "shell_script",
  "vendor_installer",
]
export type CatalogEntryInstallMethod =
  (typeof CATALOG_ENTRY_INSTALL_METHODS)[number]

/** Sources that admin can create — `shipped` lives in alembic seeds and
 *  `subscription` lands via the BS.8.5 feed worker; the admin form only
 *  writes `operator` (standalone tenant entry) or `override` (overlay
 *  on an existing shipped row). */
export const CATALOG_ENTRY_WRITABLE_SOURCES: ReadonlyArray<string> = [
  "operator",
  "override",
]
export type CatalogEntryWritableSource =
  (typeof CATALOG_ENTRY_WRITABLE_SOURCES)[number]

/** Mirror `ENTRY_ID_PATTERN` (backend/routers/catalog.py:120) — kebab-case,
 *  alphanumeric groups separated by single hyphens, no leading / trailing
 *  hyphen, no double hyphens. */
export const CATALOG_ENTRY_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
export const CATALOG_ENTRY_ID_MAX_LEN = 64
export const CATALOG_ENTRY_VENDOR_MAX_LEN = 128
export const CATALOG_ENTRY_DISPLAY_NAME_MAX_LEN = 256
export const CATALOG_ENTRY_VERSION_MAX_LEN = 64
export const CATALOG_ENTRY_INSTALL_URL_MAX_LEN = 2048
export const CATALOG_ENTRY_SHA256_PATTERN = /^[0-9a-f]{64}$/
export const CATALOG_ENTRY_SIZE_BYTES_MAX = 2 ** 40 // 1 TiB — mirrors backend SIZE_BYTES_MAX (`1 << 40` overflows 32-bit shift)

/** Wire shape for `_row_to_entry()`. Snake_case as returned by FastAPI;
 *  the `<CustomEntryForm />` consumes this directly. The `metadata` /
 *  `depends_on` JSONB columns come back already deserialised. */
export interface CatalogEntryDetail {
  id: string
  source: "shipped" | "operator" | "override" | "subscription"
  schema_version: number
  tenant_id: string | null
  vendor: string | null
  family: CatalogEntryFamily | null
  display_name: string | null
  version: string | null
  install_method: CatalogEntryInstallMethod | null
  install_url: string | null
  sha256: string | null
  size_bytes: number | null
  depends_on: string[]
  metadata: Record<string, unknown>
  hidden: boolean
  created_at: string
  updated_at: string
}

export interface ListCatalogEntriesResponse {
  items: CatalogEntryDetail[]
  count: number
  total: number
  limit: number
  offset: number
}

export interface CreateCatalogEntryRequest {
  id: string
  source?: CatalogEntryWritableSource
  vendor?: string | null
  family?: CatalogEntryFamily | null
  display_name?: string | null
  version?: string | null
  install_method?: CatalogEntryInstallMethod | null
  install_url?: string | null
  sha256?: string | null
  size_bytes?: number | null
  depends_on?: string[]
  metadata?: Record<string, unknown>
}

export interface PatchCatalogEntryRequest {
  vendor?: string | null
  family?: CatalogEntryFamily | null
  display_name?: string | null
  version?: string | null
  install_method?: CatalogEntryInstallMethod | null
  install_url?: string | null
  sha256?: string | null
  size_bytes?: number | null
  depends_on?: string[] | null
  metadata?: Record<string, unknown> | null
  hidden?: boolean | null
}

export interface DeleteCatalogEntryResponse {
  status: "deleted"
  id: string
  tenant_id: string
}

export interface ListCatalogEntriesOptions {
  family?: CatalogEntryFamily
  source?: string
  vendor?: string
  installMethod?: CatalogEntryInstallMethod
  q?: string
  sort?: "id" | "vendor" | "family" | "display_name" | "created_at" | "updated_at"
  order?: "asc" | "desc"
  includeHidden?: boolean
  limit?: number
  offset?: number
}

/** GET /catalog/entries — list entries visible to the caller. The full
 *  response is paginated; the BS.8.6 form pulls everything (limit=500
 *  hard-cap on the backend) so the depends_on multi-select can search
 *  across the entire catalog without a per-keystroke round-trip. */
export async function listCatalogEntries(
  options?: ListCatalogEntriesOptions,
): Promise<ListCatalogEntriesResponse> {
  const params = new URLSearchParams()
  if (options?.family) params.set("family", options.family)
  if (options?.source) params.set("source", options.source)
  if (options?.vendor) params.set("vendor", options.vendor)
  if (options?.installMethod) params.set("install_method", options.installMethod)
  if (options?.q) params.set("q", options.q)
  if (options?.sort) params.set("sort", options.sort)
  if (options?.order) params.set("order", options.order)
  if (options?.includeHidden) params.set("include_hidden", "true")
  if (typeof options?.limit === "number") params.set("limit", String(options.limit))
  if (typeof options?.offset === "number") params.set("offset", String(options.offset))
  const qs = params.toString()
  return request<ListCatalogEntriesResponse>(
    `/catalog/entries${qs ? `?${qs}` : ""}`,
    { method: "GET" },
  )
}

/** POST /catalog/entries — create a new operator/override row. Throws
 *  :class:`ApiError` with ``status=409`` on duplicate ``(id, source,
 *  tenant_id)`` per ``uq_catalog_entries_visible``, ``status=422`` on
 *  validation failure (e.g. malformed sha256), ``status=404`` when an
 *  override is being created against a non-existent shipped base. */
export async function createCatalogEntry(
  body: CreateCatalogEntryRequest,
): Promise<CatalogEntryDetail> {
  return request<CatalogEntryDetail>("/catalog/entries", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** PATCH /catalog/entries/{id} — partial update. The backend rejects an
 *  empty body with 422; the frontend should only call this with at
 *  least one field set. Encodes `id` so a slash never escapes the path. */
export async function patchCatalogEntry(
  entryId: string,
  body: PatchCatalogEntryRequest,
): Promise<CatalogEntryDetail> {
  return request<CatalogEntryDetail>(
    `/catalog/entries/${encodeURIComponent(entryId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
    },
  )
}

/** DELETE /catalog/entries/{id} — per-tenant soft-delete (operator/
 *  override row) or tombstone (shipped row gets an override with
 *  hidden=TRUE). Returns 404 if the row does not resolve in the
 *  caller's tenant scope. */
export async function deleteCatalogEntry(
  entryId: string,
): Promise<DeleteCatalogEntryResponse> {
  return request<DeleteCatalogEntryResponse>(
    `/catalog/entries/${encodeURIComponent(entryId)}`,
    { method: "DELETE" },
  )
}

/** Trim + lowercase the candidate id. Mirrors what the backend's
 *  Pydantic field accepts (raw kebab-case alphanumeric); the frontend
 *  does not auto-rewrite invalid characters — it just trims. */
export function normaliseCatalogEntryId(raw: string): string {
  return raw.trim()
}

/** Validate a candidate catalog entry id. Returns ``null`` when valid,
 *  otherwise a short user-facing message. Mirrors the backend's
 *  ``ENTRY_ID_PATTERN`` and ``ENTRY_ID_MAX_LEN``. */
export function validateCatalogEntryId(raw: string): string | null {
  const v = normaliseCatalogEntryId(raw)
  if (v.length === 0) return "id is required"
  if (v.length > CATALOG_ENTRY_ID_MAX_LEN) {
    return `id must be ≤ ${CATALOG_ENTRY_ID_MAX_LEN} chars`
  }
  if (!CATALOG_ENTRY_ID_PATTERN.test(v)) {
    return "id must be kebab-case (lowercase alphanumerics + single hyphens)"
  }
  return null
}

/** Validate a candidate install URL. Empty / null is allowed (the
 *  backend column is nullable for `noop` / `vendor_installer` install
 *  methods); when present, must be http(s) and within the byte cap. */
export function validateCatalogEntryInstallUrl(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null
  const v = raw.trim()
  if (v.length === 0) return null
  if (v.length > CATALOG_ENTRY_INSTALL_URL_MAX_LEN) {
    return `install URL must be ≤ ${CATALOG_ENTRY_INSTALL_URL_MAX_LEN} chars`
  }
  if (!/^https?:\/\//i.test(v)) {
    return "install URL must start with http:// or https://"
  }
  return null
}

/** Validate a candidate sha256 hex digest. Empty / null is allowed (the
 *  backend column is nullable when the install method does not require
 *  a checksum, e.g. `vendor_installer`); when present must match the
 *  fixed 64-char lowercase-hex pattern. */
export function validateCatalogEntrySha256(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null
  const v = raw.trim()
  if (v.length === 0) return null
  if (!CATALOG_ENTRY_SHA256_PATTERN.test(v)) {
    return "sha256 must be 64 lowercase hex characters"
  }
  return null
}

/** Validate a candidate size_bytes value. Empty / null is allowed; when
 *  present must be a non-negative integer below the 1 TiB ceiling. */
export function validateCatalogEntrySizeBytes(
  raw: number | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null
  if (!Number.isFinite(raw)) return "size_bytes must be a number"
  if (!Number.isInteger(raw)) return "size_bytes must be an integer"
  if (raw < 0) return "size_bytes must be ≥ 0"
  if (raw > CATALOG_ENTRY_SIZE_BYTES_MAX) {
    return `size_bytes must be ≤ ${CATALOG_ENTRY_SIZE_BYTES_MAX} (1 TiB)`
  }
  return null
}

/** Validate a candidate vendor / display_name / version length. Empty
 *  / null is permitted (override-source rows may carry NULL on the
 *  inherit-from-shipped path); when present must respect the column
 *  length. */
export function validateCatalogEntryVendor(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null
  const v = raw.trim()
  if (v.length === 0) return null
  if (v.length > CATALOG_ENTRY_VENDOR_MAX_LEN) {
    return `vendor must be ≤ ${CATALOG_ENTRY_VENDOR_MAX_LEN} chars`
  }
  return null
}

export function validateCatalogEntryDisplayName(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null
  const v = raw.trim()
  if (v.length === 0) return null
  if (v.length > CATALOG_ENTRY_DISPLAY_NAME_MAX_LEN) {
    return `display_name must be ≤ ${CATALOG_ENTRY_DISPLAY_NAME_MAX_LEN} chars`
  }
  return null
}

export function validateCatalogEntryVersion(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null
  const v = raw.trim()
  if (v.length === 0) return null
  if (v.length > CATALOG_ENTRY_VERSION_MAX_LEN) {
    return `version must be ≤ ${CATALOG_ENTRY_VERSION_MAX_LEN} chars`
  }
  return null
}

/** BS.7.7 — POST /installer/jobs/{id}/cancel — flip a queued / running
 *  install job's PG row to ``state='cancelled'``. The sidecar's next
 *  ``report_progress`` round-trip sees the new state and the in-flight
 *  install method aborts via :class:`InstallCancelled` (BS.4.2 contract).
 *
 *  The optional ``reason`` is recorded on the row's ``error_reason``
 *  column when one was not previously set; it is surfaced in the audit
 *  log + the ``installer.job_cancelled`` event. Pass ``null`` /
 *  ``undefined`` to use the backend default ``"operator_cancelled"``.
 *
 *  Throws :class:`ApiError` on:
 *    • 404 — row not found / wrong tenant.
 *    • 409 — row is already terminal (completed / failed / cancelled).
 *    • 422 — malformed job_id (does not match
 *      ``backend/routers/installer._INSTALL_JOB_ID_RE``).
 *    • 403 — caller lacks ``operator`` role.
 *
 *  The frontend cancel UX is optimistic: the caller normally drops the
 *  row from local SSE state the moment this helper resolves (or even
 *  before, since the SSE feed re-confirms in ~10–50 ms via the
 *  ``installer_progress`` ``state="cancelled"`` event the backend emits
 *  inside :func:`backend.routers.installer.cancel_job`). */
export async function cancelInstallJob(
  jobId: string,
  options?: { reason?: string | null },
): Promise<InstallJob> {
  const reason = options?.reason
  // Body is optional — backend accepts ``null`` / empty body. Only send
  // a body when a non-empty reason is supplied so the typical
  // operator-clicks-cancel flow is a zero-byte POST.
  const init: RequestInit = { method: "POST" }
  if (typeof reason === "string" && reason.length > 0) {
    init.body = JSON.stringify({ reason })
  }
  return request<InstallJob>(
    `/installer/jobs/${encodeURIComponent(jobId)}/cancel`,
    init,
  )
}

// ─── N3 — OpenAPI compile-time contract tripwire ──────────────────────────
// These type aliases reach into `lib/generated/api-types.ts` (auto-generated
// from the FastAPI app's OpenAPI schema). The moment any of the referenced
// routes or schemas is renamed, removed, or reshaped on the backend, `tsc
// --noEmit` in CI fails — exactly the "FastAPI schema drifts → frontend
// compile blows up" contract N3 promises.
//
// Full replacement of the hand-rolled `ApiAgent` / `ApiTask` etc. with
// generated equivalents is intentionally out of scope (too much surface to
// migrate in one pass). Over time, prefer `GetResponse<"/api/v1/...">` from
// `./generated/openapi` for new endpoints.
import type {
  AgentSchema as _N3_AgentSchema,
  TaskSchema as _N3_TaskSchema,
  GetResponse as _N3_GetResponse,
  PostBody as _N3_PostBody,
} from "./generated/openapi"

// The four route probes below cover the "load-bearing" endpoints of the
// app (agents + tasks list and create). If any disappears, this file
// stops compiling.
type _N3_AgentsListResp = _N3_GetResponse<"/api/v1/agents">
type _N3_TasksListResp = _N3_GetResponse<"/api/v1/tasks">
type _N3_AgentCreateBody = _N3_PostBody<"/api/v1/agents">
type _N3_TaskCreateBody = _N3_PostBody<"/api/v1/tasks">

// `satisfies never extends …` keeps these aliases load-bearing without
// leaking into the public module signature.
export type _N3_ContractProbes = [
  _N3_AgentSchema,
  _N3_TaskSchema,
  _N3_AgentsListResp,
  _N3_TasksListResp,
  _N3_AgentCreateBody,
  _N3_TaskCreateBody,
]

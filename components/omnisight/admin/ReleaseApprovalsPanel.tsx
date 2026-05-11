/**
 * OP-949 H4 — Operator approval admin panel.
 *
 * Replaces the legacy JIRA +2 sign-off gate (L1 R8 / ADR-0018) with a
 * backend-mediated approval surface. The panel:
 *
 *   1. Loads `/api/v1/release-approvals/pending` on first paint.
 *   2. Renders one row per release awaiting sign-off: version, current
 *      canary %, SLO snapshot, "approve" / "abort" buttons.
 *   3. Re-fetches on the shared SSE bus `release.dashboard.updated`
 *      event (the H3 state machine emits this on every transition log
 *      write — including the approval-request / approval-decision
 *      sub-state updates added in this ticket).
 *   4. Per-row race-protection: clicking either button disables both
 *      until the in-flight call resolves; backend race surfaces as a
 *      typed `RaceConditionApproval` error in the row.
 *   5. Auth refusal (401/403 with `error="auth_refused"`) redirects to
 *      `/login?next=/admin/release-approvals` per the AC error catalog.
 *   6. Backend dispatch failure (502 with `error="dispatch_failed"`)
 *      keeps the row in place and surfaces a retry button — the
 *      decision is already recorded server-side, only the H2 advance
 *      event needs to be re-queued.
 *
 * Test seams:
 *   - `fetchPending`: replaces the default GET against
 *     `/api/v1/release-approvals/pending`.
 *   - `submitDecision`: replaces the default POST against
 *     `/api/v1/release-approvals/{release_id}/{approve|abort}`.
 *   - `eventTransport`: replaces `subscribeEvents` for SSE wiring.
 *   - `redirectToLogin`: replaces the default `window.location.assign`
 *     call so tests can assert auth-refusal routing.
 *   - `nowImpl`: pins the relative-time clock.
 */
"use client"

import * as React from "react"
import {
  AlertOctagon,
  CheckCircle2,
  CircleDashed,
  CircleSlash,
  Clock,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Wifi,
  WifiOff,
  XOctagon,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"

import { subscribeEvents } from "@/lib/api"

// ─── Wire shapes ──────────────────────────────────────────────────────────

export const RELEASE_DASHBOARD_EVENT = "release.dashboard.updated" as const

/** SLO snapshot embedded in the approval-request log entry. */
export interface SloSnapshot {
  error_rate?: number | null
  p95_latency_ms?: number | null
  observed_window_seconds?: number | null
  [key: string]: unknown
}

/** One pending row, mirroring `PendingApprovalRow` on the backend. */
export interface PendingApprovalRow {
  release_id: string
  version: string
  state: string
  canary_percent: number | null
  slo_snapshot: SloSnapshot | null
  reason: string | null
  requested_at: string | null
  row_version: number
}

export interface PendingApprovalsResponse {
  pending: PendingApprovalRow[]
  generated_at: string
}

export type DecisionKind = "approve" | "abort"

export interface DecisionRequest {
  releaseId: string
  decision: DecisionKind
  reason?: string
}

export interface DecisionResponse {
  release_id: string
  version: string
  decision: string
  operator: string
  dispatched_event_row_id: number | null
  row_version: number
}

export interface DecisionErrorBody {
  error: string
  reason?: string
  prior?: Record<string, unknown> | null
}

/** Thrown so the panel can branch on the AC-named error catalog. */
export class ApprovalApiError extends Error {
  status: number
  body: DecisionErrorBody | null
  constructor(status: number, body: DecisionErrorBody | null, message: string) {
    super(message)
    this.name = "ApprovalApiError"
    this.status = status
    this.body = body
  }
}

export type FetchPending = () => Promise<PendingApprovalsResponse>
export type SubmitDecision = (req: DecisionRequest) => Promise<DecisionResponse>

export interface ReleaseEvent {
  event: string
  data: Record<string, unknown>
}
export interface EventTransportHandle {
  close: () => void
}
export type EventTransport = (
  onEvent: (event: ReleaseEvent) => void,
  onError?: () => void,
) => EventTransportHandle

// ─── Pure helpers (exported for tests) ────────────────────────────────────

export function emptyPending(): PendingApprovalsResponse {
  return { pending: [], generated_at: new Date(0).toISOString() }
}

export function formatRelativeAge(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso) return "—"
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return String(iso)
  const deltaSec = Math.max(0, Math.floor((now - t) / 1000))
  if (deltaSec < 60) return "just now"
  if (deltaSec < 3600) return `${Math.floor(deltaSec / 60)}m ago`
  if (deltaSec < 86400) return `${Math.floor(deltaSec / 3600)}h ago`
  if (deltaSec < 86400 * 7) return `${Math.floor(deltaSec / 86400)}d ago`
  return `${Math.floor(deltaSec / (86400 * 7))}w ago`
}

export function shouldShowStaleBanner(
  lastFetchAt: number,
  now: number,
  sseError: boolean,
  staleAfterMs: number = 60_000,
): boolean {
  if (sseError) return true
  if (lastFetchAt === 0) return false
  return now - lastFetchAt > staleAfterMs
}

export function summariseSlo(slo: SloSnapshot | null | undefined): string {
  if (!slo) return "no SLO snapshot"
  const parts: string[] = []
  if (typeof slo.error_rate === "number") {
    parts.push(`err=${(slo.error_rate * 100).toFixed(2)}%`)
  }
  if (typeof slo.p95_latency_ms === "number") {
    parts.push(`p95=${slo.p95_latency_ms}ms`)
  }
  if (typeof slo.observed_window_seconds === "number") {
    parts.push(`window=${slo.observed_window_seconds}s`)
  }
  return parts.length > 0 ? parts.join(" · ") : "no SLO snapshot"
}

export function normaliseRow(
  raw: Partial<PendingApprovalRow> | null | undefined,
): PendingApprovalRow | null {
  if (!raw || !raw.release_id || !raw.version) return null
  return {
    release_id: String(raw.release_id),
    version: String(raw.version),
    state: typeof raw.state === "string" ? raw.state : "unknown",
    canary_percent:
      typeof raw.canary_percent === "number" ? raw.canary_percent : null,
    slo_snapshot:
      raw.slo_snapshot && typeof raw.slo_snapshot === "object"
        ? raw.slo_snapshot
        : null,
    reason: typeof raw.reason === "string" ? raw.reason : null,
    requested_at: typeof raw.requested_at === "string" ? raw.requested_at : null,
    row_version: typeof raw.row_version === "number" ? raw.row_version : 0,
  }
}

export function normalisePending(
  payload: Partial<PendingApprovalsResponse> | null | undefined,
): PendingApprovalsResponse {
  const rows = Array.isArray(payload?.pending) ? payload!.pending : []
  const out: PendingApprovalRow[] = []
  for (const r of rows) {
    const n = normaliseRow(r as Partial<PendingApprovalRow>)
    if (n) out.push(n)
  }
  return {
    pending: out,
    generated_at: payload?.generated_at ?? new Date().toISOString(),
  }
}

async function defaultFetchPending(): Promise<PendingApprovalsResponse> {
  const res = await fetch("/api/v1/release-approvals/pending", {
    credentials: "include",
  })
  if (res.status === 401 || res.status === 403) {
    const body = await readErrorBody(res)
    throw new ApprovalApiError(
      res.status,
      body,
      body?.error ?? `auth refused: ${res.status}`,
    )
  }
  if (!res.ok) {
    throw new Error(`pending-approvals fetch failed: ${res.status}`)
  }
  const payload = (await res.json()) as Partial<PendingApprovalsResponse>
  return normalisePending(payload)
}

async function defaultSubmitDecision(
  req: DecisionRequest,
): Promise<DecisionResponse> {
  const url = `/api/v1/release-approvals/${encodeURIComponent(
    req.releaseId,
  )}/${req.decision}`
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: req.reason ?? "" }),
  })
  if (!res.ok) {
    const body = await readErrorBody(res)
    throw new ApprovalApiError(
      res.status,
      body,
      body?.error ?? `decision failed: ${res.status}`,
    )
  }
  return (await res.json()) as DecisionResponse
}

async function readErrorBody(res: Response): Promise<DecisionErrorBody | null> {
  try {
    const raw = await res.json()
    if (raw && typeof raw === "object") {
      const detail = (raw as { detail?: unknown }).detail
      if (detail && typeof detail === "object") {
        return detail as DecisionErrorBody
      }
      if (typeof detail === "string") {
        return { error: detail }
      }
      return raw as DecisionErrorBody
    }
  } catch {
    // body wasn't JSON — fall through to null
  }
  return null
}

// ─── Sub-components ──────────────────────────────────────────────────────

interface RowProps {
  row: PendingApprovalRow
  inFlight: boolean
  errorBody: DecisionErrorBody | null
  onDecide: (row: PendingApprovalRow, decision: DecisionKind) => void
  onRetry: (row: PendingApprovalRow) => void
  now: number
  testId: string
}

function ApprovalRow({
  row,
  inFlight,
  errorBody,
  onDecide,
  onRetry,
  now,
  testId,
}: RowProps) {
  const canaryLabel =
    typeof row.canary_percent === "number"
      ? `${row.canary_percent}% canary`
      : "no canary stage"
  const sloLabel = summariseSlo(row.slo_snapshot)
  const dispatchFailed = errorBody?.error === "dispatch_failed"
  const alreadyResolved = errorBody?.error === "already_resolved"
  const priorOperator =
    alreadyResolved && errorBody?.prior && typeof errorBody.prior === "object"
      ? (errorBody.prior as { operator?: string }).operator ?? null
      : null

  return (
    <li
      data-testid={`${testId}-row-${row.release_id}`}
      data-state={row.state}
      data-canary={row.canary_percent ?? ""}
      className="flex flex-col gap-1.5 rounded-md border border-amber-400/40 bg-amber-500/5 px-3 py-2"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge
          variant="outline"
          data-testid={`${testId}-row-${row.release_id}-version`}
          className="h-5 gap-1 px-1.5 text-[11px]"
        >
          <ShieldCheck className="size-3" aria-hidden="true" />
          {row.version}
        </Badge>
        <code
          data-testid={`${testId}-row-${row.release_id}-release-id`}
          className="rounded-sm bg-muted/50 px-1.5 py-0.5 font-mono text-[10px]"
        >
          {row.release_id}
        </code>
        <Badge
          variant="secondary"
          data-testid={`${testId}-row-${row.release_id}-state`}
          className="h-5 px-1.5 text-[10px]"
        >
          {row.state}
        </Badge>
        <Badge
          variant="outline"
          data-testid={`${testId}-row-${row.release_id}-canary`}
          className="h-5 px-1.5 text-[10px]"
        >
          {canaryLabel}
        </Badge>
        <span
          data-testid={`${testId}-row-${row.release_id}-slo`}
          className="font-mono text-[10px] text-muted-foreground"
        >
          {sloLabel}
        </span>
        <span
          data-testid={`${testId}-row-${row.release_id}-requested-at`}
          className="ml-auto font-mono text-[10px] text-muted-foreground"
        >
          requested {formatRelativeAge(row.requested_at, now)}
        </span>
      </div>

      {row.reason && (
        <div
          data-testid={`${testId}-row-${row.release_id}-reason`}
          className="text-[11px] text-muted-foreground"
        >
          {row.reason}
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="secondary"
          data-testid={`${testId}-row-${row.release_id}-approve`}
          onClick={() => onDecide(row, "approve")}
          disabled={inFlight}
          className="h-7 gap-1 px-2 text-xs"
        >
          {inFlight ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : (
            <CheckCircle2 className="size-3" aria-hidden="true" />
          )}
          Approve
        </Button>
        <Button
          type="button"
          size="sm"
          variant="destructive"
          data-testid={`${testId}-row-${row.release_id}-abort`}
          onClick={() => onDecide(row, "abort")}
          disabled={inFlight}
          className="h-7 gap-1 px-2 text-xs"
        >
          {inFlight ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : (
            <XOctagon className="size-3" aria-hidden="true" />
          )}
          Abort
        </Button>
        {dispatchFailed && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-testid={`${testId}-row-${row.release_id}-retry`}
            onClick={() => onRetry(row)}
            disabled={inFlight}
            className="h-7 gap-1 px-2 text-xs"
          >
            <RefreshCw className="size-3" aria-hidden="true" />
            Retry dispatch
          </Button>
        )}
        {errorBody && (
          <span
            data-testid={`${testId}-row-${row.release_id}-error`}
            role="alert"
            className="inline-flex items-center gap-1 text-[10px] text-rose-300"
          >
            <CircleSlash className="size-3" aria-hidden="true" />
            {alreadyResolved
              ? `already resolved${priorOperator ? ` by ${priorOperator}` : ""}`
              : dispatchFailed
                ? "backend dispatch failed — retry"
                : errorBody.reason ?? errorBody.error}
          </span>
        )}
      </div>
    </li>
  )
}

// ─── Main panel ──────────────────────────────────────────────────────────

export interface ReleaseApprovalsPanelProps {
  /** Override the network fetch. */
  fetchPending?: FetchPending
  /** Override the decision POST. */
  submitDecision?: SubmitDecision
  /** Override SSE subscription. */
  eventTransport?: EventTransport
  /** Pin "now" for relative-time formatting. */
  nowImpl?: () => number
  /** Initial payload, useful when the host hydrates from SSR. */
  initialPending?: PendingApprovalsResponse | null
  /** Override the auth-refused redirect (default: `window.location.assign`). */
  redirectToLogin?: (next: string) => void
  /** `data-testid` root. */
  testId?: string
}

interface DecisionInFlightState {
  inFlight: Set<string>
  errorByReleaseId: Record<string, DecisionErrorBody | null>
  lastDecisionByReleaseId: Record<string, DecisionKind>
}

function defaultRedirectToLogin(next: string): void {
  if (typeof window === "undefined") return
  window.location.assign(`/login?next=${encodeURIComponent(next)}`)
}

export function ReleaseApprovalsPanel(props: ReleaseApprovalsPanelProps) {
  const {
    fetchPending = defaultFetchPending,
    submitDecision = defaultSubmitDecision,
    eventTransport,
    nowImpl,
    initialPending = null,
    redirectToLogin = defaultRedirectToLogin,
    testId = "release-approvals-panel",
  } = props

  const [pending, setPending] = React.useState<PendingApprovalsResponse>(
    () => initialPending ?? emptyPending(),
  )
  const [loading, setLoading] = React.useState<boolean>(initialPending == null)
  const [error, setError] = React.useState<string | null>(null)
  const [lastFetchAt, setLastFetchAt] = React.useState<number>(
    initialPending ? Date.now() : 0,
  )
  const [sseError, setSseError] = React.useState<boolean>(false)
  const [decision, setDecision] = React.useState<DecisionInFlightState>({
    inFlight: new Set(),
    errorByReleaseId: {},
    lastDecisionByReleaseId: {},
  })

  // Race protection — synchronous ref so triple-clicks before React
  // commits the disabled state still coalesce to one in-flight call.
  const inFlightRef = React.useRef<Set<string>>(new Set())

  // ── Data loader ────────────────────────────────────────────────────────
  const reload = React.useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await fetchPending()
      setPending(next)
      setLastFetchAt(Date.now())
      setSseError(false)
    } catch (e) {
      if (e instanceof ApprovalApiError && (e.status === 401 || e.status === 403)) {
        redirectToLogin("/admin/release-approvals")
        return
      }
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [fetchPending, redirectToLogin])

  React.useEffect(() => {
    if (initialPending) return
    void reload()
  }, [reload, initialPending])

  // ── SSE wire-up ────────────────────────────────────────────────────────
  React.useEffect(() => {
    const handler = (event: ReleaseEvent) => {
      if (event.event !== RELEASE_DASHBOARD_EVENT) return
      setSseError(false)
      void reload()
    }
    const onError = () => setSseError(true)
    if (eventTransport) {
      const h = eventTransport(handler, onError)
      return () => h?.close?.()
    }
    const h = subscribeEvents(
      (ev) =>
        handler({
          event: ev.event,
          data: (ev.data ?? {}) as Record<string, unknown>,
        }),
      onError,
    )
    return () => h?.close?.()
  }, [eventTransport, reload])

  // ── Decision handler ───────────────────────────────────────────────────
  const submit = React.useCallback(
    async (row: PendingApprovalRow, kind: DecisionKind) => {
      if (inFlightRef.current.has(row.release_id)) return
      inFlightRef.current.add(row.release_id)
      setDecision((prev) => ({
        inFlight: new Set([...prev.inFlight, row.release_id]),
        errorByReleaseId: { ...prev.errorByReleaseId, [row.release_id]: null },
        lastDecisionByReleaseId: {
          ...prev.lastDecisionByReleaseId,
          [row.release_id]: kind,
        },
      }))
      try {
        await submitDecision({ releaseId: row.release_id, decision: kind })
        await reload()
      } catch (e) {
        if (
          e instanceof ApprovalApiError &&
          (e.status === 401 || e.status === 403) &&
          e.body?.error === "auth_refused"
        ) {
          redirectToLogin("/admin/release-approvals")
          return
        }
        const body =
          e instanceof ApprovalApiError
            ? e.body ?? { error: "unknown", reason: e.message }
            : { error: "unknown", reason: e instanceof Error ? e.message : String(e) }
        setDecision((prev) => ({
          ...prev,
          errorByReleaseId: { ...prev.errorByReleaseId, [row.release_id]: body },
        }))
      } finally {
        inFlightRef.current.delete(row.release_id)
        setDecision((prev) => {
          const next = new Set(prev.inFlight)
          next.delete(row.release_id)
          return { ...prev, inFlight: next }
        })
      }
    },
    [submitDecision, reload, redirectToLogin],
  )

  const retryDispatch = React.useCallback(
    (row: PendingApprovalRow) => {
      const kind = decision.lastDecisionByReleaseId[row.release_id] ?? "approve"
      void submit(row, kind)
    },
    [decision.lastDecisionByReleaseId, submit],
  )

  // ── Render-time derived state ──────────────────────────────────────────
  const now = nowImpl ? nowImpl() : Date.now()
  const showStale = shouldShowStaleBanner(lastFetchAt, now, sseError)
  const rows = pending.pending

  return (
    <section
      data-testid={testId}
      data-loading={loading ? "true" : "false"}
      data-sse-error={sseError ? "true" : "false"}
      data-pending-count={rows.length}
      className="flex min-h-0 flex-col gap-3 rounded-md border border-border bg-background/60 p-3"
    >
      {/* ─── Header ─────────────────────────────────────────────────────── */}
      <header
        data-testid={`${testId}-header`}
        className="flex flex-wrap items-center gap-2"
      >
        <Badge
          variant="outline"
          data-testid={`${testId}-pending-count`}
          className="h-5 gap-1 px-1.5 text-[11px]"
        >
          <AlertOctagon className="size-3" aria-hidden="true" />
          {rows.length} pending approval{rows.length === 1 ? "" : "s"}
        </Badge>
        <span
          data-testid={`${testId}-generated-at`}
          className="ml-auto font-mono text-[10px] text-muted-foreground"
        >
          updated {formatRelativeAge(pending.generated_at, now)}
        </span>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          data-testid={`${testId}-refresh`}
          onClick={() => void reload()}
          disabled={loading}
          className="h-7 gap-1 px-2 text-xs"
        >
          {loading ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : (
            <RefreshCw className="size-3" aria-hidden="true" />
          )}
          Refresh
        </Button>
      </header>

      {/* ─── Banners ────────────────────────────────────────────────────── */}
      {showStale && (
        <div
          data-testid={`${testId}-sse-stale`}
          role="status"
          className="flex items-center gap-2 rounded-md border border-amber-400/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200"
        >
          {sseError ? (
            <WifiOff className="size-3.5" aria-hidden="true" />
          ) : (
            <Clock className="size-3.5" aria-hidden="true" />
          )}
          <span>
            {sseError
              ? "Real-time updates disconnected — auto-reconnecting. Data may be stale."
              : "Approval queue has not refreshed recently. Press Refresh."}
          </span>
        </div>
      )}
      {!showStale && !loading && (
        <div
          data-testid={`${testId}-sse-ok`}
          className="inline-flex w-fit items-center gap-1 text-[10px] text-emerald-400"
        >
          <Wifi className="size-3" aria-hidden="true" />
          live
        </div>
      )}
      {error && (
        <div
          data-testid={`${testId}-fetch-error`}
          role="alert"
          className="flex items-center gap-2 rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300"
        >
          <CircleSlash className="size-3.5" aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}

      <Separator />

      {/* ─── Pending list ───────────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-section-pending`}
        className="flex flex-col gap-1"
      >
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Awaiting operator approval
        </h3>
        {rows.length === 0 ? (
          <div
            data-testid={`${testId}-pending-empty`}
            className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground"
          >
            {loading
              ? "Loading pending approvals…"
              : "No releases are awaiting approval right now."}
          </div>
        ) : (
          <ul
            data-testid={`${testId}-pending-list`}
            className="flex flex-col gap-2"
          >
            {rows.map((row) => (
              <ApprovalRow
                key={row.release_id}
                row={row}
                inFlight={decision.inFlight.has(row.release_id)}
                errorBody={
                  decision.errorByReleaseId[row.release_id] ?? null
                }
                onDecide={submit}
                onRetry={retryDispatch}
                now={now}
                testId={testId}
              />
            ))}
          </ul>
        )}
      </section>

      {/* ─── Footer ─────────────────────────────────────────────────────── */}
      <footer
        data-testid={`${testId}-footer`}
        className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        <span className="inline-flex items-center gap-1">
          <CircleDashed className="size-3" aria-hidden="true" />
          OP-949 H4 operator approval gate
        </span>
        <span data-testid={`${testId}-footer-generated`}>
          generated <code className="font-mono">{pending.generated_at}</code>
        </span>
      </footer>
    </section>
  )
}

export default ReleaseApprovalsPanel

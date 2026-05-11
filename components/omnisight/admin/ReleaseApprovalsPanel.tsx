/**
 * OP-949 H4 — Operator approval web panel (replaces the JIRA +2 gate).
 *
 * Renders the queue of releases currently sitting in the
 * ``pending_approval`` sub-state (Postgres ``release_state.state ==
 * staging``) plus the already-approved-but-still-abortable rows in
 * ``canary_5``. Per AC #3, each row shows version, current canary %,
 * SLO snapshot and an Approve / Abort pair.
 *
 * Wire contract — backed by ``backend/api/release_approval.py``:
 *
 *   GET    /api/v1/release-conductor/approvals
 *   POST   /api/v1/release-conductor/approvals/approve
 *   POST   /api/v1/release-conductor/approvals/abort
 *
 * Each POST echoes the ``row_version`` the panel saw on the last GET
 * so the backend's optimistic-locking handshake can refuse stale
 * clicks with ``RaceConditionApproval`` (AC #4 error catalog).
 *
 * Test seams (all injectable via props so the test file never touches
 * the real network / SSE bus):
 *
 *   - ``fetchApprovals``  : replaces ``GET /approvals``
 *   - ``submitApprove``   : replaces the approve POST
 *   - ``submitAbort``     : replaces the abort POST
 *   - ``eventTransport``  : replaces the shared ``subscribeEvents``
 *   - ``nowImpl``         : pins the relative-time clock
 *
 * SSE wire-up
 * -----------
 * The panel subscribes to the shared event stream and re-fetches on
 * ``release.dashboard.updated`` (the same topic the deployments panel
 * uses); we deliberately do not introduce a new topic so the AC #4
 * "UI updates via SSE" point lands without backend wiring churn.
 *
 * Error catalog (mirrors ticket description)
 * ------------------------------------------
 *   ApprovalAuthRefused      → redirect to /login?next=/admin/release-approvals
 *   RaceConditionApproval    → inline row banner "already approved";
 *                              the next re-fetch reconciles state.
 *   BackendDispatchFailed    → inline row banner + retry button.
 */
"use client"

import * as React from "react"
import {
  AlertOctagon,
  CheckCircle2,
  CircleSlash,
  Clock,
  Loader2,
  RefreshCw,
  ShieldCheck,
  ShieldOff,
  Wifi,
  WifiOff,
  XCircle,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"

import { subscribeEvents } from "@/lib/api"

// ─── Wire shapes ─────────────────────────────────────────────────────────

export const RELEASE_DASHBOARD_EVENT = "release.dashboard.updated" as const

export interface SLOSnapshot {
  halted: boolean
  breach: Record<string, unknown> | null
}

export interface ApprovalRow {
  release_id: string
  version: string
  state: string
  sub_state: string
  row_version: number
  canary_percent: number | null
  slo_snapshot: SLOSnapshot
  last_transition_at: string
  created_at: string
}

export interface ApprovalsListPayload {
  releases: ApprovalRow[]
  generated_at: string
}

export interface ApprovalActionPayload {
  release_id: string
  state: string
  row_version: number
  last_transition_at: string
  audit_event_row_id: number
}

export interface ApprovalActionRequest {
  release_id: string
  row_version: number
}

export type FetchApprovals = () => Promise<ApprovalsListPayload>
export type SubmitApprovalAction = (
  req: ApprovalActionRequest,
) => Promise<ApprovalActionPayload>

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

// ─── HTTP error shape ────────────────────────────────────────────────────

/**
 * Lightweight error wrapper so the panel can distinguish:
 *   - 401 (auth refused — redirect to login)
 *   - 409 (race — show inline banner, re-fetch)
 *   - 5xx (backend dispatch failed — show retry button)
 *   - everything else — generic fetch-error banner
 *
 * Default network transports throw this; the test injects whatever
 * shape it likes via the prop overrides.
 */
export class ApprovalApiError extends Error {
  readonly status: number
  readonly detail: string
  constructor(status: number, detail: string) {
    super(`approval API error ${status}: ${detail}`)
    this.status = status
    this.detail = detail
  }
}

// ─── Pure helpers (exported for tests) ───────────────────────────────────

export function emptyApprovalsPayload(): ApprovalsListPayload {
  return { releases: [], generated_at: new Date(0).toISOString() }
}

export function formatRelativeAge(
  isoTimestamp: string,
  now: number = Date.now(),
): string {
  const t = Date.parse(isoTimestamp)
  if (!Number.isFinite(t)) return "—"
  const deltaMs = now - t
  const sec = Math.max(0, Math.floor(deltaMs / 1000))
  if (sec < 60) return `${sec}s ago`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}h ago`
  return `${Math.floor(hr / 24)}d ago`
}

export function isAbortable(row: ApprovalRow): boolean {
  return row.state === "staging" || row.state === "canary_5"
}

export function isApprovable(row: ApprovalRow): boolean {
  return row.state === "staging"
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

/**
 * Classify a fetch / submit error so the surrounding UI knows whether
 * to redirect, show "already approved", or surface a retry button.
 *
 * Centralised so the test file can poke specific status codes through
 * the injected submit shim and watch the row banner classification.
 */
export function classifyApprovalError(
  err: unknown,
): "auth_refused" | "race" | "backend_failed" | "unknown" {
  if (err instanceof ApprovalApiError) {
    if (err.status === 401) return "auth_refused"
    if (err.status === 409) return "race"
    if (err.status >= 500) return "backend_failed"
  }
  return "unknown"
}

// ─── Default network transports ──────────────────────────────────────────

async function _readErrorDetail(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail
      if (typeof d === "string") return d
      try {
        return JSON.stringify(d)
      } catch {
        return String(d)
      }
    }
  } catch {
    /* fall through to text */
  }
  try {
    return await res.text()
  } catch {
    return res.statusText
  }
}

async function defaultFetchApprovals(): Promise<ApprovalsListPayload> {
  const res = await fetch("/api/v1/release-conductor/approvals", {
    credentials: "include",
  })
  if (!res.ok) {
    throw new ApprovalApiError(res.status, await _readErrorDetail(res))
  }
  return (await res.json()) as ApprovalsListPayload
}

function _csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {}
  for (const part of document.cookie.split(";")) {
    const [k, ...v] = part.trim().split("=")
    if (k === "omnisight_csrf") {
      return { "X-CSRF-Token": decodeURIComponent(v.join("=")) }
    }
  }
  return {}
}

async function _postAction(
  path: string,
  body: ApprovalActionRequest,
): Promise<ApprovalActionPayload> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ..._csrfHeader(),
    },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new ApprovalApiError(res.status, await _readErrorDetail(res))
  }
  return (await res.json()) as ApprovalActionPayload
}

const defaultSubmitApprove: SubmitApprovalAction = (req) =>
  _postAction("/api/v1/release-conductor/approvals/approve", req)

const defaultSubmitAbort: SubmitApprovalAction = (req) =>
  _postAction("/api/v1/release-conductor/approvals/abort", req)

// ─── Main panel ──────────────────────────────────────────────────────────

export interface ReleaseApprovalsPanelProps {
  /** Override the GET /approvals fetch. */
  fetchApprovals?: FetchApprovals
  /** Override the approve POST. */
  submitApprove?: SubmitApprovalAction
  /** Override the abort POST. */
  submitAbort?: SubmitApprovalAction
  /** Override SSE subscription (default: shared `subscribeEvents`). */
  eventTransport?: EventTransport
  /** Pin "now" for relative-time formatting. */
  nowImpl?: () => number
  /** Callback fired when the API reports auth-refused (UI then redirects). */
  onAuthRefused?: () => void
  /** Initial payload (useful for SSR hydration). */
  initialPayload?: ApprovalsListPayload | null
  /** `data-testid` root. */
  testId?: string
}

interface RowActionState {
  /** Which (release_id, action) tuples are currently in-flight. */
  inFlight: Set<string>
  /** Inline banner per release_id: classification + detail message. */
  errorByReleaseId: Record<
    string,
    { kind: "auth_refused" | "race" | "backend_failed" | "unknown"; detail: string }
  >
}

function _actionKey(releaseId: string, action: "approve" | "abort"): string {
  return `${action}:${releaseId}`
}

export function ReleaseApprovalsPanel(props: ReleaseApprovalsPanelProps) {
  const {
    fetchApprovals = defaultFetchApprovals,
    submitApprove = defaultSubmitApprove,
    submitAbort = defaultSubmitAbort,
    eventTransport,
    nowImpl,
    onAuthRefused,
    initialPayload = null,
    testId = "release-approvals-panel",
  } = props

  const [payload, setPayload] = React.useState<ApprovalsListPayload>(
    () => initialPayload ?? emptyApprovalsPayload(),
  )
  const [loading, setLoading] = React.useState<boolean>(initialPayload == null)
  const [error, setError] = React.useState<string | null>(null)
  const [lastFetchAt, setLastFetchAt] = React.useState<number>(
    initialPayload ? Date.now() : 0,
  )
  const [sseError, setSseError] = React.useState<boolean>(false)
  const [rowState, setRowState] = React.useState<RowActionState>({
    inFlight: new Set(),
    errorByReleaseId: {},
  })

  // Race-protection: in-progress set in a ref so the click handler can
  // short-circuit synchronously before React commits the disabled state.
  const inFlightRef = React.useRef<Set<string>>(new Set())

  const reload = React.useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await fetchApprovals()
      setPayload(next)
      setLastFetchAt(Date.now())
      setSseError(false)
    } catch (e) {
      const kind = classifyApprovalError(e)
      if (kind === "auth_refused") {
        // AC #5 — surface the redirect-to-login signal upward. The
        // panel itself doesn't router-push (that's a host-page concern).
        onAuthRefused?.()
      }
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [fetchApprovals, onAuthRefused])

  React.useEffect(() => {
    if (initialPayload) return
    void reload()
  }, [reload, initialPayload])

  // SSE — re-fetch on release.dashboard.updated. The deployments
  // dashboard fires this topic on every transition so the approval
  // queue stays live without a backend wire change.
  React.useEffect(() => {
    const handler = (ev: ReleaseEvent) => {
      if (ev.event !== RELEASE_DASHBOARD_EVENT) return
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

  const handleAction = React.useCallback(
    async (row: ApprovalRow, action: "approve" | "abort") => {
      const key = _actionKey(row.release_id, action)
      if (inFlightRef.current.has(key)) return
      inFlightRef.current.add(key)
      setRowState((prev) => {
        const nextInFlight = new Set(prev.inFlight)
        nextInFlight.add(key)
        const nextErrors = { ...prev.errorByReleaseId }
        delete nextErrors[row.release_id]
        return { inFlight: nextInFlight, errorByReleaseId: nextErrors }
      })
      const submit = action === "approve" ? submitApprove : submitAbort
      try {
        await submit({
          release_id: row.release_id,
          row_version: row.row_version,
        })
        await reload()
      } catch (e) {
        const kind = classifyApprovalError(e)
        if (kind === "auth_refused") {
          onAuthRefused?.()
        }
        const detail =
          e instanceof ApprovalApiError
            ? e.detail
            : e instanceof Error
              ? e.message
              : String(e)
        setRowState((prev) => ({
          ...prev,
          errorByReleaseId: {
            ...prev.errorByReleaseId,
            [row.release_id]: { kind, detail },
          },
        }))
      } finally {
        inFlightRef.current.delete(key)
        setRowState((prev) => {
          const nextInFlight = new Set(prev.inFlight)
          nextInFlight.delete(key)
          return { ...prev, inFlight: nextInFlight }
        })
      }
    },
    [submitApprove, submitAbort, reload, onAuthRefused],
  )

  const handleRetry = React.useCallback(
    (row: ApprovalRow, action: "approve" | "abort") => {
      void handleAction(row, action)
    },
    [handleAction],
  )

  const now = nowImpl ? nowImpl() : Date.now()
  const releases = payload.releases
  const pendingCount = releases.filter((r) => r.state === "staging").length
  const canaryCount = releases.filter((r) => r.state === "canary_5").length
  const showStale = shouldShowStaleBanner(lastFetchAt, now, sseError)

  return (
    <section
      data-testid={testId}
      data-loading={loading ? "true" : "false"}
      data-sse-error={sseError ? "true" : "false"}
      data-pending-count={pendingCount}
      data-canary-count={canaryCount}
      className="flex min-h-0 flex-col gap-3 rounded-md border border-border bg-background/60 p-3"
    >
      <header
        data-testid={`${testId}-header`}
        className="flex flex-wrap items-center gap-2"
      >
        <Badge
          variant="outline"
          data-testid={`${testId}-pending-count`}
          className="h-5 gap-1 px-1.5 text-[11px]"
        >
          <ShieldCheck className="size-3.5" aria-hidden="true" />
          {pendingCount} pending approval{pendingCount === 1 ? "" : "s"}
        </Badge>
        <Badge
          variant="outline"
          data-testid={`${testId}-canary-count`}
          className="h-5 gap-1 px-1.5 text-[11px]"
        >
          <AlertOctagon className="size-3.5" aria-hidden="true" />
          {canaryCount} in canary 5%
        </Badge>
        <span
          data-testid={`${testId}-generated-at`}
          className="ml-auto font-mono text-[10px] text-muted-foreground"
        >
          updated {formatRelativeAge(payload.generated_at, now)}
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
              ? "Real-time updates disconnected — auto-reconnecting. Approval queue may be stale."
              : "Approval queue has not refreshed recently. Press Refresh to pull the latest state."}
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

      <section
        data-testid={`${testId}-section-rows`}
        className="flex flex-col gap-1"
      >
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Pending operator approval
        </h3>
        {releases.length === 0 ? (
          <div
            data-testid={`${testId}-empty`}
            className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground"
          >
            {loading
              ? "Loading approval queue…"
              : "No releases are currently awaiting approval."}
          </div>
        ) : (
          <ul
            data-testid={`${testId}-list`}
            className="flex flex-col gap-1.5"
          >
            {releases.map((row) => {
              const approveBusy = rowState.inFlight.has(
                _actionKey(row.release_id, "approve"),
              )
              const abortBusy = rowState.inFlight.has(
                _actionKey(row.release_id, "abort"),
              )
              const rowErr = rowState.errorByReleaseId[row.release_id]
              return (
                <li
                  key={row.release_id}
                  data-testid={`${testId}-row-${row.release_id}`}
                  data-state={row.state}
                  data-sub-state={row.sub_state}
                  data-row-version={row.row_version}
                  className="flex flex-wrap items-center gap-2 rounded-md border border-border/70 bg-background/60 px-3 py-2 text-xs"
                >
                  <Badge
                    variant="outline"
                    data-testid={`${testId}-row-${row.release_id}-state`}
                    className="h-5 gap-1 px-1.5 text-[11px]"
                  >
                    {row.sub_state}
                  </Badge>
                  <code
                    data-testid={`${testId}-row-${row.release_id}-version`}
                    className="rounded-sm bg-muted/50 px-1.5 py-0.5 font-mono text-[11px]"
                  >
                    {row.version}
                  </code>
                  <code
                    data-testid={`${testId}-row-${row.release_id}-release-id`}
                    className="font-mono text-[10px] text-muted-foreground"
                  >
                    {row.release_id}
                  </code>
                  <span
                    data-testid={`${testId}-row-${row.release_id}-canary`}
                    className="font-mono text-[10px] text-muted-foreground"
                  >
                    canary {row.canary_percent ?? "—"}%
                  </span>
                  <span
                    data-testid={`${testId}-row-${row.release_id}-slo`}
                    data-halted={row.slo_snapshot.halted ? "true" : "false"}
                    className={`inline-flex items-center gap-1 text-[10px] ${
                      row.slo_snapshot.halted
                        ? "text-rose-300"
                        : "text-emerald-400"
                    }`}
                  >
                    {row.slo_snapshot.halted ? (
                      <ShieldOff className="size-3" aria-hidden="true" />
                    ) : (
                      <ShieldCheck className="size-3" aria-hidden="true" />
                    )}
                    SLO {row.slo_snapshot.halted ? "halted" : "ok"}
                  </span>
                  <span
                    data-testid={`${testId}-row-${row.release_id}-age`}
                    className="ml-auto font-mono text-[10px] text-muted-foreground"
                  >
                    {formatRelativeAge(row.last_transition_at, now)}
                  </span>
                  <div className="flex items-center gap-1">
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      data-testid={`${testId}-row-${row.release_id}-approve`}
                      disabled={
                        !isApprovable(row) || approveBusy || abortBusy
                      }
                      onClick={() => void handleAction(row, "approve")}
                      className="h-7 gap-1 px-2 text-xs"
                    >
                      {approveBusy ? (
                        <Loader2
                          className="size-3 animate-spin"
                          aria-hidden="true"
                        />
                      ) : (
                        <CheckCircle2
                          className="size-3"
                          aria-hidden="true"
                        />
                      )}
                      Approve
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      data-testid={`${testId}-row-${row.release_id}-abort`}
                      disabled={
                        !isAbortable(row) || approveBusy || abortBusy
                      }
                      onClick={() => void handleAction(row, "abort")}
                      className="h-7 gap-1 px-2 text-xs"
                    >
                      {abortBusy ? (
                        <Loader2
                          className="size-3 animate-spin"
                          aria-hidden="true"
                        />
                      ) : (
                        <XCircle className="size-3" aria-hidden="true" />
                      )}
                      Abort
                    </Button>
                  </div>
                  {rowErr && (
                    <div
                      data-testid={`${testId}-row-${row.release_id}-error`}
                      data-error-kind={rowErr.kind}
                      role="alert"
                      className="basis-full flex items-center gap-2 text-[10px] text-rose-300"
                    >
                      <CircleSlash className="size-3" aria-hidden="true" />
                      <span>
                        {rowErr.kind === "race"
                          ? "Already actioned — refreshing queue…"
                          : rowErr.kind === "backend_failed"
                            ? "Backend dispatch failed — retry?"
                            : rowErr.kind === "auth_refused"
                              ? "Authentication required — redirecting…"
                              : rowErr.detail}
                      </span>
                      {rowErr.kind === "backend_failed" && (
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          data-testid={`${testId}-row-${row.release_id}-retry`}
                          onClick={() =>
                            handleRetry(
                              row,
                              row.state === "staging" ? "approve" : "abort",
                            )
                          }
                          className="h-5 gap-1 px-1 text-[10px]"
                        >
                          <RefreshCw className="size-3" aria-hidden="true" />
                          Retry
                        </Button>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>

      <footer
        data-testid={`${testId}-footer`}
        className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        <span>OP-949 H4 operator approval queue</span>
        <span data-testid={`${testId}-footer-generated`}>
          generated <code className="font-mono">{payload.generated_at}</code>
        </span>
      </footer>
    </section>
  )
}

export default ReleaseApprovalsPanel

/**
 * OP-943 G7 — "Pending releases" table for the D17 deployments
 * dashboard (mounted as a tab inside {@link DeploymentsPanel}).
 *
 * Renders one row per open RELEASE-* / HOTFIX-* release reported by the
 * G7 aggregator (`GET /api/v1/release-state/pending-releases`):
 *
 *   1. Version + a HOTFIX badge for hotfix releases.
 *   2. Current state — which release child / step is in progress — plus
 *      how long it has been parked there ("blocking for …").
 *   3. Operator-approval-pending highlight (R8 / H2 gate): the whole
 *      row turns amber and an "awaiting approval" chip appears.
 *   4. A 1-click "Approve" button, enabled iff the backend reports
 *      `can_approve` (the `release:approval-pending` marker is set AND
 *      the caller is an authenticated human operator). Clicking it
 *      POSTs to the H4 surface `POST /api/v1/release-approvals/{id}/approve`.
 *
 * Real-time: re-fetches on the shared SSE bus `release.dashboard.updated`
 * event (the H3 state machine emits this on every transition / approval
 * log write).
 *
 * Test seams:
 *   - `fetchReleases`: replaces the default GET against
 *     `/api/v1/release-state/pending-releases`.
 *   - `approveRelease`: replaces the default POST against
 *     `/api/v1/release-approvals/{release_id}/approve`.
 *   - `eventTransport`: replaces `subscribeEvents` for SSE wiring.
 *   - `redirectToLogin`: replaces the default `window.location.assign`
 *     call so tests can assert auth-refusal routing (`OperatorAuthExpired`).
 *   - `nowImpl`: pins the relative-time / duration clock.
 *
 * Error catalog:
 *   - `SSEDisconnect`: the SSE handle's error callback flips a
 *     stale-data banner (`data-testid=…-sse-stale`); the next snapshot
 *     fetch (auto on reconnect, or manual Refresh) clears it.
 *   - `OperatorAuthExpired`: a 401/403 from either the list fetch or
 *     the approve POST routes to `/login?next=/admin/deployments`.
 */
"use client"

import * as React from "react"
import {
  AlertOctagon,
  CheckCircle2,
  CircleDashed,
  CircleSlash,
  Clock,
  Flame,
  Loader2,
  PackageCheck,
  RefreshCw,
  Rocket,
  Wifi,
  WifiOff,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

import { subscribeEvents } from "@/lib/api"

// ─── Wire shapes ──────────────────────────────────────────────────────────

export const RELEASE_DASHBOARD_EVENT = "release.dashboard.updated" as const

/** SLO snapshot embedded in the approval-request marker. */
export interface SloSnapshot {
  error_rate?: number | null
  p95_latency_ms?: number | null
  observed_window_seconds?: number | null
  [key: string]: unknown
}

/** One row, mirroring `PendingReleaseRow` on the backend. */
export interface PendingReleaseRow {
  release_id: string
  version: string
  state: string
  is_hotfix: boolean
  last_transition_at: string | null
  blocking_seconds: number
  approval_pending: boolean
  approval_reason: string | null
  approval_requested_at: string | null
  canary_percent: number | null
  slo_snapshot: SloSnapshot | null
  can_approve: boolean
}

export interface PendingReleasesResponse {
  releases: PendingReleaseRow[]
  generated_at: string
}

export interface ApproveResponse {
  release_id: string
  version: string
  decision: string
  operator: string
  dispatched_event_row_id: number | null
  row_version: number
}

export interface ApiErrorBody {
  error: string
  reason?: string
  prior?: Record<string, unknown> | null
}

/** Thrown so the panel can branch on the AC-named error catalog. */
export class ReleaseStateApiError extends Error {
  status: number
  body: ApiErrorBody | null
  constructor(status: number, body: ApiErrorBody | null, message: string) {
    super(message)
    this.name = "ReleaseStateApiError"
    this.status = status
    this.body = body
  }
}

export type FetchReleases = () => Promise<PendingReleasesResponse>
export type ApproveRelease = (releaseId: string) => Promise<ApproveResponse>

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

export function emptyReleases(): PendingReleasesResponse {
  return { releases: [], generated_at: new Date(0).toISOString() }
}

export function formatRelativeAge(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!iso) return "—"
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return String(iso)
  const deltaSec = Math.max(0, Math.floor((now - t) / 1000))
  return formatDuration(deltaSec) + " ago"
}

/** Compact, human-readable duration: `45s`, `12m`, `3h 20m`, `2d 4h`. */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) {
    const rm = m % 60
    return rm ? `${h}h ${rm}m` : `${h}h`
  }
  const d = Math.floor(h / 24)
  const rh = h % 24
  return rh ? `${d}d ${rh}h` : `${d}d`
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
  raw: Partial<PendingReleaseRow> | null | undefined,
): PendingReleaseRow | null {
  if (!raw || !raw.release_id || !raw.version) return null
  return {
    release_id: String(raw.release_id),
    version: String(raw.version),
    state: typeof raw.state === "string" ? raw.state : "unknown",
    is_hotfix: raw.is_hotfix === true,
    last_transition_at:
      typeof raw.last_transition_at === "string" ? raw.last_transition_at : null,
    blocking_seconds:
      typeof raw.blocking_seconds === "number" && raw.blocking_seconds >= 0
        ? raw.blocking_seconds
        : 0,
    approval_pending: raw.approval_pending === true,
    approval_reason:
      typeof raw.approval_reason === "string" ? raw.approval_reason : null,
    approval_requested_at:
      typeof raw.approval_requested_at === "string"
        ? raw.approval_requested_at
        : null,
    canary_percent:
      typeof raw.canary_percent === "number" ? raw.canary_percent : null,
    slo_snapshot:
      raw.slo_snapshot && typeof raw.slo_snapshot === "object"
        ? raw.slo_snapshot
        : null,
    can_approve: raw.can_approve === true,
  }
}

export function normaliseReleases(
  payload: Partial<PendingReleasesResponse> | null | undefined,
): PendingReleasesResponse {
  const rows = Array.isArray(payload?.releases) ? payload!.releases : []
  const out: PendingReleaseRow[] = []
  for (const r of rows) {
    const n = normaliseRow(r as Partial<PendingReleaseRow>)
    if (n) out.push(n)
  }
  return {
    releases: out,
    generated_at: payload?.generated_at ?? new Date().toISOString(),
  }
}

async function readErrorBody(res: Response): Promise<ApiErrorBody | null> {
  try {
    const raw = await res.json()
    if (raw && typeof raw === "object") {
      const detail = (raw as { detail?: unknown }).detail
      if (detail && typeof detail === "object") return detail as ApiErrorBody
      if (typeof detail === "string") return { error: detail }
      return raw as ApiErrorBody
    }
  } catch {
    // body wasn't JSON — fall through to null
  }
  return null
}

async function defaultFetchReleases(): Promise<PendingReleasesResponse> {
  const res = await fetch("/api/v1/release-state/pending-releases", {
    credentials: "include",
  })
  if (res.status === 401 || res.status === 403) {
    const body = await readErrorBody(res)
    throw new ReleaseStateApiError(
      res.status,
      body,
      body?.error ?? `auth refused: ${res.status}`,
    )
  }
  if (!res.ok) {
    throw new Error(`pending-releases fetch failed: ${res.status}`)
  }
  return normaliseReleases(
    (await res.json()) as Partial<PendingReleasesResponse>,
  )
}

async function defaultApproveRelease(
  releaseId: string,
): Promise<ApproveResponse> {
  const res = await fetch(
    `/api/v1/release-approvals/${encodeURIComponent(releaseId)}/approve`,
    {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reason: `Operator 1-click approve from D17 pending-releases tab (release_id=${releaseId})`,
      }),
    },
  )
  if (!res.ok) {
    const body = await readErrorBody(res)
    throw new ReleaseStateApiError(
      res.status,
      body,
      body?.error ?? `approve failed: ${res.status}`,
    )
  }
  return (await res.json()) as ApproveResponse
}

function defaultRedirectToLogin(next: string): void {
  if (typeof window === "undefined") return
  window.location.assign(`/login?next=${encodeURIComponent(next)}`)
}

const LOGIN_NEXT = "/admin/deployments"

// ─── Sub-components ──────────────────────────────────────────────────────

interface RowProps {
  row: PendingReleaseRow
  inFlight: boolean
  errorBody: ApiErrorBody | null
  onApprove: (row: PendingReleaseRow) => void
  now: number
  testId: string
}

function ReleaseRow({
  row,
  inFlight,
  errorBody,
  onApprove,
  now,
  testId,
}: RowProps) {
  const rid = row.release_id
  const blocking = formatDuration(row.blocking_seconds)
  const sloLabel = row.approval_pending ? summariseSlo(row.slo_snapshot) : ""
  return (
    <TableRow
      data-testid={`${testId}-row-${rid}`}
      data-state={row.state}
      data-approval-pending={row.approval_pending ? "true" : "false"}
      data-hotfix={row.is_hotfix ? "true" : "false"}
      className={
        row.approval_pending
          ? "border-amber-400/40 bg-amber-500/5"
          : undefined
      }
    >
      {/* Version */}
      <TableCell className="align-top">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge
            variant="outline"
            data-testid={`${testId}-row-${rid}-version`}
            className="h-5 gap-1 px-1.5 text-[11px]"
          >
            <Rocket className="size-3" aria-hidden="true" />
            {row.version}
          </Badge>
          {row.is_hotfix && (
            <Badge
              variant="outline"
              data-testid={`${testId}-row-${rid}-hotfix`}
              className="h-5 gap-1 px-1.5 text-[10px] text-orange-400"
            >
              <Flame className="size-3" aria-hidden="true" />
              hotfix
            </Badge>
          )}
          <code
            data-testid={`${testId}-row-${rid}-release-id`}
            className="rounded-sm bg-muted/50 px-1.5 py-0.5 font-mono text-[10px]"
          >
            {rid}
          </code>
        </div>
      </TableCell>

      {/* Current state + how long blocking */}
      <TableCell className="align-top">
        <div className="flex flex-col gap-0.5">
          <Badge
            variant="secondary"
            data-testid={`${testId}-row-${rid}-state`}
            className="h-5 w-fit px-1.5 text-[10px]"
          >
            {row.state}
          </Badge>
          <span
            data-testid={`${testId}-row-${rid}-blocking`}
            data-blocking-seconds={row.blocking_seconds}
            className="font-mono text-[10px] text-muted-foreground"
          >
            in this step {blocking}
          </span>
        </div>
      </TableCell>

      {/* Approval gate */}
      <TableCell className="align-top">
        {row.approval_pending ? (
          <div className="flex flex-col gap-0.5">
            <span
              data-testid={`${testId}-row-${rid}-approval-chip`}
              className="inline-flex w-fit items-center gap-1 text-[10px] text-amber-300"
            >
              <AlertOctagon className="size-3" aria-hidden="true" />
              awaiting operator approval
              {typeof row.canary_percent === "number"
                ? ` · ${row.canary_percent}% canary`
                : ""}
            </span>
            {sloLabel && (
              <span
                data-testid={`${testId}-row-${rid}-slo`}
                className="font-mono text-[10px] text-muted-foreground"
              >
                {sloLabel}
              </span>
            )}
            {row.approval_reason && (
              <span
                data-testid={`${testId}-row-${rid}-reason`}
                className="text-[10px] text-muted-foreground"
              >
                {row.approval_reason}
              </span>
            )}
            {row.approval_requested_at && (
              <span className="font-mono text-[10px] text-muted-foreground">
                requested {formatRelativeAge(row.approval_requested_at, now)}
              </span>
            )}
          </div>
        ) : (
          <span
            data-testid={`${testId}-row-${rid}-no-approval`}
            className="text-[10px] text-muted-foreground"
          >
            —
          </span>
        )}
      </TableCell>

      {/* Action */}
      <TableCell className="align-top">
        <div className="flex flex-col items-start gap-1">
          {row.approval_pending && (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              data-testid={`${testId}-row-${rid}-approve`}
              onClick={() => onApprove(row)}
              disabled={inFlight || !row.can_approve}
              className="h-7 gap-1 px-2 text-xs"
            >
              {inFlight ? (
                <Loader2 className="size-3 animate-spin" aria-hidden="true" />
              ) : (
                <CheckCircle2 className="size-3" aria-hidden="true" />
              )}
              Approve
            </Button>
          )}
          {row.approval_pending && !row.can_approve && (
            <span
              data-testid={`${testId}-row-${rid}-approve-disabled-hint`}
              className="text-[9px] text-muted-foreground"
            >
              sign in as a human operator to approve
            </span>
          )}
          {errorBody && (
            <span
              data-testid={`${testId}-row-${rid}-error`}
              role="alert"
              className="inline-flex items-center gap-1 text-[10px] text-rose-300"
            >
              <CircleSlash className="size-3" aria-hidden="true" />
              {errorBody.error === "already_resolved"
                ? "already resolved by another operator"
                : errorBody.reason ?? errorBody.error}
            </span>
          )}
        </div>
      </TableCell>
    </TableRow>
  )
}

// ─── Main panel ──────────────────────────────────────────────────────────

export interface PendingReleasesTableProps {
  /** Override the network fetch. */
  fetchReleases?: FetchReleases
  /** Override the 1-click approve POST. */
  approveRelease?: ApproveRelease
  /** Override SSE subscription. */
  eventTransport?: EventTransport
  /** Pin "now" for relative-time / duration formatting. */
  nowImpl?: () => number
  /** Initial payload, useful when the host hydrates from SSR. */
  initialReleases?: PendingReleasesResponse | null
  /** Override the auth-refused redirect (default: `window.location.assign`). */
  redirectToLogin?: (next: string) => void
  /** `data-testid` root. */
  testId?: string
}

interface ApproveInFlightState {
  inFlight: Set<string>
  errorByReleaseId: Record<string, ApiErrorBody | null>
}

export function PendingReleasesTable(props: PendingReleasesTableProps) {
  const {
    fetchReleases = defaultFetchReleases,
    approveRelease = defaultApproveRelease,
    eventTransport,
    nowImpl,
    initialReleases = null,
    redirectToLogin = defaultRedirectToLogin,
    testId = "pending-releases-table",
  } = props

  const [data, setData] = React.useState<PendingReleasesResponse>(
    () => initialReleases ?? emptyReleases(),
  )
  const [loading, setLoading] = React.useState<boolean>(initialReleases == null)
  const [error, setError] = React.useState<string | null>(null)
  const [lastFetchAt, setLastFetchAt] = React.useState<number>(
    initialReleases ? Date.now() : 0,
  )
  const [sseError, setSseError] = React.useState<boolean>(false)
  const [approve, setApprove] = React.useState<ApproveInFlightState>({
    inFlight: new Set(),
    errorByReleaseId: {},
  })

  // Race protection — synchronous ref so triple-clicks before React
  // commits the disabled state still coalesce to one in-flight call.
  const inFlightRef = React.useRef<Set<string>>(new Set())

  const isAuthRefused = React.useCallback(
    (e: unknown): e is ReleaseStateApiError =>
      e instanceof ReleaseStateApiError && (e.status === 401 || e.status === 403),
    [],
  )

  // ── Data loader ────────────────────────────────────────────────────────
  const reload = React.useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await fetchReleases()
      setData(next)
      setLastFetchAt(Date.now())
      setSseError(false)
    } catch (e) {
      if (isAuthRefused(e)) {
        redirectToLogin(LOGIN_NEXT)
        return
      }
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [fetchReleases, redirectToLogin, isAuthRefused])

  React.useEffect(() => {
    if (initialReleases) return
    void reload()
  }, [reload, initialReleases])

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

  // ── Approve handler ────────────────────────────────────────────────────
  const onApprove = React.useCallback(
    async (row: PendingReleaseRow) => {
      const rid = row.release_id
      if (inFlightRef.current.has(rid)) return
      inFlightRef.current.add(rid)
      setApprove((prev) => ({
        inFlight: new Set([...prev.inFlight, rid]),
        errorByReleaseId: { ...prev.errorByReleaseId, [rid]: null },
      }))
      try {
        await approveRelease(rid)
        await reload()
      } catch (e) {
        if (isAuthRefused(e)) {
          redirectToLogin(LOGIN_NEXT)
          return
        }
        const body =
          e instanceof ReleaseStateApiError
            ? e.body ?? { error: "unknown", reason: e.message }
            : {
                error: "unknown",
                reason: e instanceof Error ? e.message : String(e),
              }
        setApprove((prev) => ({
          ...prev,
          errorByReleaseId: { ...prev.errorByReleaseId, [rid]: body },
        }))
      } finally {
        inFlightRef.current.delete(rid)
        setApprove((prev) => {
          const next = new Set(prev.inFlight)
          next.delete(rid)
          return { ...prev, inFlight: next }
        })
      }
    },
    [approveRelease, reload, redirectToLogin, isAuthRefused],
  )

  // ── Render-time derived state ──────────────────────────────────────────
  const now = nowImpl ? nowImpl() : Date.now()
  const showStale = shouldShowStaleBanner(lastFetchAt, now, sseError)
  const rows = data.releases
  const pendingApprovalCount = rows.filter((r) => r.approval_pending).length

  return (
    <section
      data-testid={testId}
      data-loading={loading ? "true" : "false"}
      data-sse-error={sseError ? "true" : "false"}
      data-release-count={rows.length}
      data-approval-pending-count={pendingApprovalCount}
      className="flex min-h-0 flex-col gap-3 rounded-md border border-border bg-background/60 p-3"
    >
      {/* ─── Header ─────────────────────────────────────────────────────── */}
      <header
        data-testid={`${testId}-header`}
        className="flex flex-wrap items-center gap-2"
      >
        <Badge
          variant="outline"
          data-testid={`${testId}-release-count`}
          className="h-5 gap-1 px-1.5 text-[11px]"
        >
          <PackageCheck className="size-3" aria-hidden="true" />
          {rows.length} open release{rows.length === 1 ? "" : "s"}
        </Badge>
        <Badge
          variant="outline"
          data-testid={`${testId}-approval-pending-count`}
          className="h-5 gap-1 px-1.5 text-[11px] text-amber-300"
        >
          <AlertOctagon className="size-3" aria-hidden="true" />
          {pendingApprovalCount} awaiting approval
        </Badge>
        <span
          data-testid={`${testId}-generated-at`}
          className="ml-auto font-mono text-[10px] text-muted-foreground"
        >
          updated {formatRelativeAge(data.generated_at, now)}
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
              : "Pending-releases view has not refreshed recently. Press Refresh."}
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

      {/* ─── Releases table ─────────────────────────────────────────────── */}
      {rows.length === 0 ? (
        <div
          data-testid={`${testId}-empty`}
          className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground"
        >
          {loading
            ? "Loading pending releases…"
            : "No open RELEASE-* / HOTFIX-* releases right now."}
        </div>
      ) : (
        <Table data-testid={`${testId}-table`}>
          <TableHeader>
            <TableRow>
              <TableHead className="text-[10px] uppercase tracking-wider">
                Version
              </TableHead>
              <TableHead className="text-[10px] uppercase tracking-wider">
                Current state
              </TableHead>
              <TableHead className="text-[10px] uppercase tracking-wider">
                Approval gate
              </TableHead>
              <TableHead className="text-[10px] uppercase tracking-wider">
                Action
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody data-testid={`${testId}-tbody`}>
            {rows.map((row) => (
              <ReleaseRow
                key={row.release_id}
                row={row}
                inFlight={approve.inFlight.has(row.release_id)}
                errorBody={approve.errorByReleaseId[row.release_id] ?? null}
                onApprove={onApprove}
                now={now}
                testId={testId}
              />
            ))}
          </TableBody>
        </Table>
      )}

      {/* ─── Footer ─────────────────────────────────────────────────────── */}
      <footer
        data-testid={`${testId}-footer`}
        className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        <span className="inline-flex items-center gap-1">
          <CircleDashed className="size-3" aria-hidden="true" />
          OP-943 G7 pending releases
        </span>
        <span data-testid={`${testId}-footer-generated`}>
          generated <code className="font-mono">{data.generated_at}</code>
        </span>
      </footer>
    </section>
  )
}

export default PendingReleasesTable

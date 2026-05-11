/**
 * OP-889 D17 — Deployments admin dashboard.
 *
 * Surfaces, in a single read-mostly panel:
 *
 *   1. Current prod version (`current_prod_tag`).
 *   2. Last 20 deploys with status (`release_history`).
 *   3. Canary state (`canary_snapshot`).
 *   4. SLO breach history — rollback rows in the audit table act as the
 *      surfaceable SLO-breach signal (every rollback was triggered by a
 *      breach or operator-observed regression).
 *   5. Inline operator-approval gate for in-flight deploys.
 *
 * The panel listens to the shared SSE bus for `release.dashboard.updated`
 * (OP-778 RELEASE_DASHBOARD_EVENT). When the event lands, the panel
 * re-fetches the snapshot rather than mutating local state — the backend
 * is the source of truth and the snapshot is cheap (~20 rows).
 *
 * Test seams:
 *   - `fetchSnapshot`: replaces the default fetch against
 *     `/api/v1/admin/deploy-history`. Tests inject a deterministic
 *     payload + assert the call count after SSE ticks.
 *   - `triggerRollback`: replaces the default POST to
 *     `/api/v1/admin/releases/rollback`. Tests assert the `release_id`
 *     argument matches the row clicked and that double-clicks coalesce
 *     into a single in-flight call (`RollbackButtonRaceCondition`).
 *   - `eventTransport`: replaces `subscribeEvents`. Tests fire fake
 *     `release.dashboard.updated` events and observe `fetchSnapshot`
 *     being re-invoked.
 *   - `nowImpl`: pins the relative-time clock.
 *
 * Error catalog:
 *   - `DashboardSSEDisconnect`: when the SSE handle emits an error, the
 *     panel surfaces a stale-data banner (`data-testid=
 *     deployments-panel-sse-stale`). On reconnect, the next snapshot
 *     fetch clears the banner.
 *   - `RollbackButtonRaceCondition`: rollback state is keyed by
 *     `release_id`. The same id cannot be in flight twice; clicking again
 *     is a no-op until the previous call resolves.
 */
"use client"

import * as React from "react"
import {
  AlertOctagon,
  Boxes,
  CheckCircle2,
  CircleDashed,
  CircleSlash,
  Clock,
  History,
  Loader2,
  RefreshCw,
  Rocket,
  ShieldCheck,
  Wifi,
  WifiOff,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"

import { subscribeEvents } from "@/lib/api"
import {
  DeployHistoryRow,
  deployOutcomeColor,
  formatRelativeAge,
  type DeployRecord,
  type DeployRecordOutcome,
} from "./DeployHistoryRow"

// ─── Wire shapes ──────────────────────────────────────────────────────────

export const RELEASE_DASHBOARD_EVENT = "release.dashboard.updated" as const

/** Shape returned by `/api/v1/admin/deploy-history`. */
export interface DeploymentsSnapshot {
  current_prod_tag: string | null
  in_flight: InFlightDeploy[]
  history: DeployRecord[]
  canary: CanarySnapshot | null
  generated_at: string
}

export interface InFlightDeploy {
  tag: string
  status: string
  progress_percent: number
  started_at?: string | null
  approved_by?: string | null
  /** `true` when the deploy still needs operator approval. */
  needs_approval?: boolean
}

export interface CanarySnapshot {
  rollout_id: string
  status: string
  stable_color: string
  canary_color: string
  stage_index: number
  stage: { name: string; canary_percent: number; observe_seconds: number }
  reason?: string | null
  updated_at: number
}

export interface ReleaseEvent {
  event: string
  data: Record<string, unknown>
}

export type FetchSnapshot = () => Promise<DeploymentsSnapshot>
export type TriggerRollback = (args: {
  releaseId: string | number
  tag: string | null
}) => Promise<void>

export interface EventTransportHandle {
  close: () => void
}
export type EventTransport = (
  onEvent: (event: ReleaseEvent) => void,
  onError?: () => void,
) => EventTransportHandle

// ─── Pure helpers (exported for tests) ────────────────────────────────────

export function emptySnapshot(): DeploymentsSnapshot {
  return {
    current_prod_tag: null,
    in_flight: [],
    history: [],
    canary: null,
    generated_at: new Date(0).toISOString(),
  }
}

/**
 * SLO-breach history: every `rollback` row in the audit table is, by
 * convention, the operator-visible signal that an SLO breach or
 * regression happened on the prior deploy. We surface them as a
 * dedicated strip so operators can spot a pattern (`3 rollbacks in 24h`
 * → escalate) at a glance.
 */
export function sloBreachRows(snapshot: DeploymentsSnapshot): DeployRecord[] {
  return snapshot.history.filter((r) => r.kind === "rollback")
}

/** Page the in-memory history list. */
export function paginate<T>(rows: T[], page: number, pageSize: number): T[] {
  if (pageSize <= 0) return rows
  const start = Math.max(0, page) * pageSize
  return rows.slice(start, start + pageSize)
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

export function isRollbackable(record: DeployRecord): boolean {
  return (
    record.kind === "deploy" &&
    record.outcome === "succeeded" &&
    !!record.tag
  )
}

export function normaliseHistory(
  rows: Array<Partial<DeployRecord>> | undefined | null,
): DeployRecord[] {
  if (!rows) return []
  const out: DeployRecord[] = []
  for (const r of rows) {
    if (r.id == null) continue
    out.push({
      id: r.id,
      timestamp: typeof r.timestamp === "string" ? r.timestamp : "",
      tag: typeof r.tag === "string" ? r.tag : null,
      kind: r.kind === "rollback" ? "rollback" : "deploy",
      outcome: (r.outcome as DeployRecordOutcome) ?? "unknown",
      actor: typeof r.actor === "string" ? r.actor : null,
      summary: typeof r.summary === "string" ? r.summary : "",
    })
  }
  return out
}

async function defaultFetchSnapshot(): Promise<DeploymentsSnapshot> {
  const res = await fetch("/api/v1/admin/deploy-history", {
    credentials: "include",
  })
  if (!res.ok) {
    throw new Error(`deploy-history fetch failed: ${res.status}`)
  }
  const payload = (await res.json()) as Partial<DeploymentsSnapshot>
  return {
    current_prod_tag: payload.current_prod_tag ?? null,
    in_flight: Array.isArray(payload.in_flight) ? payload.in_flight : [],
    history: normaliseHistory(payload.history),
    canary: payload.canary ?? null,
    generated_at: payload.generated_at ?? new Date().toISOString(),
  }
}

async function defaultTriggerRollback(args: {
  releaseId: string | number
  tag: string | null
}): Promise<void> {
  const res = await fetch("/api/v1/admin/releases/rollback", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      release_id: args.releaseId,
      tag: args.tag,
      reason: `Operator 1-click rollback from deployments dashboard (release_id=${args.releaseId})`,
    }),
  })
  if (!res.ok) {
    throw new Error(`rollback failed: ${res.status}`)
  }
}

// ─── Sub-components ──────────────────────────────────────────────────────

function CanaryStrip({
  canary,
  testId,
}: {
  canary: CanarySnapshot | null
  testId: string
}) {
  if (!canary) {
    return (
      <div
        data-testid={`${testId}-canary-empty`}
        className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground"
      >
        No canary rollout active.
      </div>
    )
  }
  const colour =
    canary.status === "succeeded"
      ? "var(--validation-emerald)"
      : canary.status === "aborted" || canary.status === "failed"
        ? "var(--critical-red)"
        : "var(--neural-blue)"
  return (
    <div
      data-testid={`${testId}-canary`}
      data-status={canary.status}
      className="flex flex-wrap items-center gap-2 rounded-md border border-border/70 bg-background/60 px-3 py-2"
    >
      <Badge
        data-testid={`${testId}-canary-status`}
        variant="outline"
        className="h-5 gap-1 px-1.5 text-[11px]"
        style={{ color: colour }}
      >
        <Boxes className="size-3.5" aria-hidden="true" />
        {canary.status}
      </Badge>
      <span
        data-testid={`${testId}-canary-stage`}
        className="font-mono text-[11px]"
      >
        stage {canary.stage.name} · {canary.stage.canary_percent}%
      </span>
      <code
        data-testid={`${testId}-canary-rollout-id`}
        className="rounded-sm bg-muted/50 px-1.5 py-0.5 font-mono text-[10px]"
      >
        {canary.rollout_id}
      </code>
      <span className="font-mono text-[10px] text-muted-foreground">
        {canary.stable_color} → {canary.canary_color}
      </span>
      {canary.reason && (
        <span
          data-testid={`${testId}-canary-reason`}
          className="ml-auto text-[10px] text-muted-foreground"
        >
          {canary.reason}
        </span>
      )}
    </div>
  )
}

function InFlightStrip({
  inFlight,
  onApprove,
  approvalInFlight,
  testId,
}: {
  inFlight: InFlightDeploy[]
  onApprove?: (deploy: InFlightDeploy) => void
  approvalInFlight: Set<string>
  testId: string
}) {
  if (inFlight.length === 0) {
    return (
      <div
        data-testid={`${testId}-in-flight-empty`}
        className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground"
      >
        No in-flight deploys.
      </div>
    )
  }
  return (
    <ul
      data-testid={`${testId}-in-flight`}
      className="flex flex-col gap-1.5"
    >
      {inFlight.map((d) => {
        const needsApproval = d.needs_approval || d.status === "pending"
        const isApproving = approvalInFlight.has(d.tag)
        return (
          <li
            key={d.tag}
            data-testid={`${testId}-in-flight-${d.tag}`}
            data-status={d.status}
            className="flex flex-wrap items-center gap-2 rounded-md border border-sky-500/40 bg-sky-500/5 px-3 py-2 text-xs"
          >
            <Badge
              variant="outline"
              data-testid={`${testId}-in-flight-${d.tag}-status`}
              className="h-5 gap-1 px-1.5 text-[11px]"
            >
              <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
              {d.status}
            </Badge>
            <code className="rounded-sm bg-muted/50 px-1.5 py-0.5 font-mono text-[11px]">
              {d.tag}
            </code>
            <span
              data-testid={`${testId}-in-flight-${d.tag}-progress`}
              className="font-mono text-[10px] text-muted-foreground"
            >
              {Math.max(0, Math.min(100, d.progress_percent))}%
            </span>
            {d.approved_by && (
              <span
                data-testid={`${testId}-in-flight-${d.tag}-approver`}
                className="inline-flex items-center gap-1 text-[10px] text-emerald-400"
              >
                <ShieldCheck className="size-3" aria-hidden="true" />
                approved by {d.approved_by}
              </span>
            )}
            {needsApproval && (
              <div
                data-testid={`${testId}-in-flight-${d.tag}-approval-gate`}
                className="ml-auto flex items-center gap-2"
              >
                <span className="inline-flex items-center gap-1 text-[10px] text-amber-300">
                  <AlertOctagon className="size-3" aria-hidden="true" />
                  awaiting operator approval
                </span>
                {onApprove && (
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    data-testid={`${testId}-in-flight-${d.tag}-approve`}
                    onClick={() => onApprove(d)}
                    disabled={isApproving}
                    className="h-7 gap-1 px-2 text-xs"
                  >
                    {isApproving ? (
                      <>
                        <Loader2
                          className="size-3 animate-spin"
                          aria-hidden="true"
                        />
                        Approving…
                      </>
                    ) : (
                      <>
                        <CheckCircle2 className="size-3" aria-hidden="true" />
                        Approve
                      </>
                    )}
                  </Button>
                )}
              </div>
            )}
          </li>
        )
      })}
    </ul>
  )
}

// ─── Main panel ──────────────────────────────────────────────────────────

export interface DeploymentsPanelProps {
  /** Override the network fetch (default: `/api/v1/admin/deploy-history`). */
  fetchSnapshot?: FetchSnapshot
  /** Override the rollback POST (default: `/api/v1/admin/releases/rollback`). */
  triggerRollback?: TriggerRollback
  /** Override the operator-approval POST. */
  triggerApproval?: (deploy: InFlightDeploy) => Promise<void>
  /** Override SSE subscription (default: shared `subscribeEvents`). */
  eventTransport?: EventTransport
  /** Pin "now" for relative-time formatting. */
  nowImpl?: () => number
  /** Page size for the history list (default: 10; cap at 20 per AC). */
  pageSize?: number
  /** Initial snapshot, useful when the host hydrates from SSR. */
  initialSnapshot?: DeploymentsSnapshot | null
  /** `data-testid` root. */
  testId?: string
}

interface RollbackState {
  inFlight: Set<string | number>
  errorByReleaseId: Record<string, string>
}

export function DeploymentsPanel(props: DeploymentsPanelProps) {
  const {
    fetchSnapshot = defaultFetchSnapshot,
    triggerRollback = defaultTriggerRollback,
    triggerApproval,
    eventTransport,
    nowImpl,
    pageSize = 10,
    initialSnapshot = null,
    testId = "deployments-panel",
  } = props

  const [snapshot, setSnapshot] = React.useState<DeploymentsSnapshot>(
    () => initialSnapshot ?? emptySnapshot(),
  )
  const [loading, setLoading] = React.useState<boolean>(initialSnapshot == null)
  const [error, setError] = React.useState<string | null>(null)
  const [page, setPage] = React.useState(0)
  const [lastFetchAt, setLastFetchAt] = React.useState<number>(
    initialSnapshot ? Date.now() : 0,
  )
  const [sseError, setSseError] = React.useState<boolean>(false)
  const [rollback, setRollback] = React.useState<RollbackState>({
    inFlight: new Set(),
    errorByReleaseId: {},
  })
  const [approvalInFlight, setApprovalInFlight] = React.useState<Set<string>>(
    new Set(),
  )

  // ── Race protection — keep an in-progress set in a ref too so the
  //    click handler can short-circuit synchronously when the user
  //    triple-clicks before React commits the disabled state.
  const inFlightRef = React.useRef<Set<string | number>>(new Set())

  // ── Data loader ────────────────────────────────────────────────────────
  const reload = React.useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await fetchSnapshot()
      setSnapshot(next)
      setLastFetchAt(Date.now())
      setSseError(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [fetchSnapshot])

  React.useEffect(() => {
    if (initialSnapshot) return
    void reload()
  }, [reload, initialSnapshot])

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

  // ── Rollback handler ───────────────────────────────────────────────────
  const handleRollback = React.useCallback(
    async (record: DeployRecord) => {
      const key = record.id
      if (inFlightRef.current.has(key)) return
      // Mark in-flight synchronously (ref) AND in state for the disabled
      // prop on the button. Covers `RollbackButtonRaceCondition`.
      inFlightRef.current.add(key)
      setRollback((prev) => ({
        ...prev,
        inFlight: new Set([...prev.inFlight, key]),
        errorByReleaseId: { ...prev.errorByReleaseId, [String(key)]: "" },
      }))
      try {
        await triggerRollback({ releaseId: key, tag: record.tag })
        // Snapshot will be refreshed on the next SSE tick; also re-fetch
        // here so the row updates immediately when SSE is asleep.
        await reload()
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        setRollback((prev) => ({
          ...prev,
          errorByReleaseId: { ...prev.errorByReleaseId, [String(key)]: msg },
        }))
      } finally {
        inFlightRef.current.delete(key)
        setRollback((prev) => {
          const next = new Set(prev.inFlight)
          next.delete(key)
          return { ...prev, inFlight: next }
        })
      }
    },
    [triggerRollback, reload],
  )

  // ── Approval handler ───────────────────────────────────────────────────
  const handleApprove = React.useCallback(
    async (deploy: InFlightDeploy) => {
      if (!triggerApproval) return
      if (approvalInFlight.has(deploy.tag)) return
      setApprovalInFlight((prev) => new Set([...prev, deploy.tag]))
      try {
        await triggerApproval(deploy)
        await reload()
      } finally {
        setApprovalInFlight((prev) => {
          const next = new Set(prev)
          next.delete(deploy.tag)
          return next
        })
      }
    },
    [triggerApproval, reload, approvalInFlight],
  )

  // ── Render-time derived state ──────────────────────────────────────────
  const now = nowImpl ? nowImpl() : Date.now()
  const sloRows = React.useMemo(() => sloBreachRows(snapshot), [snapshot])
  const history = snapshot.history
  const totalPages = Math.max(1, Math.ceil(history.length / pageSize))
  const safePage = Math.min(page, totalPages - 1)
  const pageRows = paginate(history, safePage, pageSize)
  const showStale = shouldShowStaleBanner(lastFetchAt, now, sseError)
  const currentTag = snapshot.current_prod_tag
  const globalRollbackInFlight = rollback.inFlight.size > 0

  return (
    <section
      data-testid={testId}
      data-loading={loading ? "true" : "false"}
      data-sse-error={sseError ? "true" : "false"}
      data-history-count={history.length}
      className="flex min-h-0 flex-col gap-3 rounded-md border border-border bg-background/60 p-3"
    >
      {/* ─── Header ─────────────────────────────────────────────────────── */}
      <header
        data-testid={`${testId}-header`}
        className="flex flex-wrap items-center gap-2"
      >
        <Badge
          variant="outline"
          data-testid={`${testId}-current-version`}
          className="h-5 gap-1 px-1.5 text-[11px]"
          style={{ color: deployOutcomeColor("succeeded") }}
        >
          <Rocket className="size-3.5" aria-hidden="true" />
          {currentTag ?? "no prod tag"}
        </Badge>
        <Badge
          variant="secondary"
          data-testid={`${testId}-history-count`}
          className="h-5 px-1.5 text-[11px]"
        >
          <History className="mr-1 size-3" aria-hidden="true" />
          {history.length} deploys tracked
        </Badge>
        <Badge
          variant="outline"
          data-testid={`${testId}-slo-breach-count`}
          className="h-5 gap-1 px-1.5 text-[11px]"
          style={{ color: deployOutcomeColor(sloRows.length ? "failed" : "succeeded") }}
        >
          <AlertOctagon className="size-3" aria-hidden="true" />
          {sloRows.length} SLO breach{sloRows.length === 1 ? "" : "es"}
        </Badge>
        <span
          data-testid={`${testId}-generated-at`}
          className="ml-auto font-mono text-[10px] text-muted-foreground"
        >
          updated {formatRelativeAge(snapshot.generated_at, now)}
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
              : "Dashboard snapshot has not refreshed recently. Press Refresh to fetch the latest state."}
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

      {/* ─── In-flight + approval gate ──────────────────────────────────── */}
      <section
        data-testid={`${testId}-section-in-flight`}
        className="flex flex-col gap-1"
      >
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          In-flight deploys
        </h3>
        <InFlightStrip
          inFlight={snapshot.in_flight}
          onApprove={triggerApproval ? handleApprove : undefined}
          approvalInFlight={approvalInFlight}
          testId={testId}
        />
      </section>

      {/* ─── Canary ─────────────────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-section-canary`}
        className="flex flex-col gap-1"
      >
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Canary rollout
        </h3>
        <CanaryStrip canary={snapshot.canary} testId={testId} />
      </section>

      {/* ─── SLO breach history ─────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-section-slo`}
        className="flex flex-col gap-1"
      >
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          SLO breach history (rollback events)
        </h3>
        {sloRows.length === 0 ? (
          <div
            data-testid={`${testId}-slo-empty`}
            className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground"
          >
            No rollbacks recorded — SLO history is clean.
          </div>
        ) : (
          <ul
            data-testid={`${testId}-slo-list`}
            className="flex flex-col gap-1"
          >
            {sloRows.slice(0, 5).map((r) => (
              <li
                key={r.id}
                data-testid={`${testId}-slo-row-${r.id}`}
                className="flex items-center gap-2 rounded-sm border border-rose-500/30 bg-rose-500/5 px-2 py-1 text-[11px]"
              >
                <AlertOctagon
                  className="size-3 text-rose-300"
                  aria-hidden="true"
                />
                <code className="font-mono">{r.tag ?? "—"}</code>
                <span className="text-muted-foreground">
                  {formatRelativeAge(r.timestamp, now)}
                </span>
                <span className="ml-auto text-muted-foreground">
                  {r.actor ?? "system"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* ─── Deploy history list ────────────────────────────────────────── */}
      <section
        data-testid={`${testId}-section-history`}
        className="flex flex-col gap-1"
      >
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          Deploy history (last {Math.min(history.length, 20)})
        </h3>
        {history.length === 0 ? (
          <div
            data-testid={`${testId}-history-empty`}
            className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground"
          >
            {loading ? "Loading deploy history…" : "No deploys recorded yet."}
          </div>
        ) : (
          <ul
            data-testid={`${testId}-history-list`}
            data-page={safePage}
            data-page-size={pageSize}
            className="flex flex-col gap-1.5"
          >
            {pageRows.map((r) => {
              const isCurrent =
                !!currentTag &&
                r.kind === "deploy" &&
                r.outcome === "succeeded" &&
                r.tag === currentTag
              const errMsg = rollback.errorByReleaseId[String(r.id)] || ""
              return (
                <React.Fragment key={r.id}>
                  <DeployHistoryRow
                    record={r}
                    inFlight={rollback.inFlight.has(r.id)}
                    globalRollbackInFlight={globalRollbackInFlight}
                    isCurrent={isCurrent}
                    onRollback={handleRollback}
                    now={now}
                    testId={`${testId}-row`}
                  />
                  {errMsg && (
                    <div
                      data-testid={`${testId}-row-${r.id}-error`}
                      role="alert"
                      className="ml-3 inline-flex items-center gap-1 text-[10px] text-rose-300"
                    >
                      <CircleSlash className="size-3" aria-hidden="true" />
                      {errMsg}
                    </div>
                  )}
                </React.Fragment>
              )
            })}
          </ul>
        )}

        {totalPages > 1 && (
          <nav
            data-testid={`${testId}-pagination`}
            className="flex items-center justify-end gap-2 text-[11px]"
          >
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid={`${testId}-pagination-prev`}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={safePage <= 0}
              className="h-7 px-2 text-xs"
            >
              Prev
            </Button>
            <span
              data-testid={`${testId}-pagination-state`}
              className="font-mono text-[10px] text-muted-foreground"
            >
              page {safePage + 1} / {totalPages}
            </span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              data-testid={`${testId}-pagination-next`}
              onClick={() =>
                setPage((p) => Math.min(totalPages - 1, p + 1))
              }
              disabled={safePage >= totalPages - 1}
              className="h-7 px-2 text-xs"
            >
              Next
            </Button>
          </nav>
        )}
      </section>

      {/* ─── Footer ─────────────────────────────────────────────────────── */}
      <footer
        data-testid={`${testId}-footer`}
        className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground"
      >
        <span className="inline-flex items-center gap-1">
          <CircleDashed className="size-3" aria-hidden="true" />
          OP-889 D17 deployments dashboard
        </span>
        <span data-testid={`${testId}-footer-generated`}>
          generated <code className="font-mono">{snapshot.generated_at}</code>
        </span>
      </footer>
    </section>
  )
}

export default DeploymentsPanel

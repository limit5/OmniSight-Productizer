/**
 * OP-913 F15 -- Cross-task awareness admin dashboard.
 *
 * Runtime reads one API-rendered snapshot, then re-fetches on SSE ticks.
 * Tests inject both fetch and event transport, matching OP-889's
 * DeploymentsPanel seams.
 *
 * Error catalog:
 *   - DashboardSSEDisconnect: show stale-data banner; next event reloads.
 *   - ToggleAuthRefused: redirect to login when the D12 PATCH returns 401/403.
 */
"use client"

import * as React from "react"
import {
  CheckCircle2,
  Clock,
  Flag,
  Gauge,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Wifi,
  WifiOff,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"

import {
  ApiError,
  patchFeatureFlag,
  subscribeEvents,
  type FeatureFlagState,
} from "@/lib/api"
import AgentDriftTile, { type AgentDriftTrend } from "./AgentDriftTile"
import DriftAlertsList, { type CogneeDriftAlert } from "./DriftAlertsList"
import MemoryUsageTile, { type MemoryFleetUsage } from "./MemoryUsageTile"

export const CROSS_TASK_AWARENESS_EVENT = "project_state.metrics.updated"
export const PROJECT_STATE_FLAG = "OMNISIGHT_PROJECT_STATE_INJECT"

export interface ProjectStateTraceRow {
  ticket: string
  develop_sha: string
  cache_hit: boolean
  total_latency_sec: number
  axis_latency_sec: Record<"structural" | "temporal" | "causal" | string, number>
  axis_error: Record<string, string>
  budget_exceeded: boolean
  captured_at: string
}

export interface ProjectStateSlo {
  status: "ok" | "warn" | "breach" | "unknown" | string
  p95_latency_sec: number | null
  budget_exceeded_count: number
  axis_error_count: Record<string, number>
}

export interface CrossTaskAwarenessSnapshot {
  memory_usage: MemoryFleetUsage[]
  cognee: {
    entity_count: number
    drift_status: "ok" | "drift" | "unknown" | string
    alerts: CogneeDriftAlert[]
  }
  agent_drift: {
    generated_at?: string | null
    trends: AgentDriftTrend[]
  }
  project_state: {
    slo: ProjectStateSlo
    traces: ProjectStateTraceRow[]
  }
  feature_flag: {
    flag_name: string
    state: FeatureFlagState
    can_toggle: boolean
  }
  generated_at: string
}

export interface AwarenessEvent {
  event: string
  data: Record<string, unknown>
}

export interface EventTransportHandle {
  close: () => void
}

export type EventTransport = (
  onEvent: (event: AwarenessEvent) => void,
  onError?: () => void,
) => EventTransportHandle

export type FetchAwarenessSnapshot = () => Promise<CrossTaskAwarenessSnapshot>
export type PatchProjectStateFlag = (
  next: FeatureFlagState,
) => Promise<{ state: FeatureFlagState }>

export function emptyAwarenessSnapshot(): CrossTaskAwarenessSnapshot {
  return {
    memory_usage: [],
    cognee: { entity_count: 0, drift_status: "unknown", alerts: [] },
    agent_drift: { generated_at: null, trends: [] },
    project_state: {
      slo: {
        status: "unknown",
        p95_latency_sec: null,
        budget_exceeded_count: 0,
        axis_error_count: {},
      },
      traces: [],
    },
    feature_flag: {
      flag_name: PROJECT_STATE_FLAG,
      state: "disabled",
      can_toggle: false,
    },
    generated_at: new Date(0).toISOString(),
  }
}

export function lastProjectStateQueries(
  traces: ProjectStateTraceRow[],
  limit = 10,
): ProjectStateTraceRow[] {
  return traces.slice(-limit).reverse()
}

export function shouldShowStaleBanner(
  lastFetchAt: number,
  now: number,
  sseError: boolean,
  staleAfterMs = 60_000,
): boolean {
  if (sseError) return true
  if (lastFetchAt === 0) return false
  return now - lastFetchAt > staleAfterMs
}

export function nextFlagState(current: FeatureFlagState): FeatureFlagState {
  return current === "enabled" ? "disabled" : "enabled"
}

async function defaultFetchAwarenessSnapshot(): Promise<CrossTaskAwarenessSnapshot> {
  const res = await fetch("/api/v1/admin/cross-task-awareness", {
    credentials: "include",
  })
  if (!res.ok) {
    throw new Error(`cross-task awareness fetch failed: ${res.status}`)
  }
  const payload = (await res.json()) as CrossTaskAwarenessSnapshot
  return {
    ...emptyAwarenessSnapshot(),
    ...payload,
    memory_usage: Array.isArray(payload.memory_usage) ? payload.memory_usage : [],
    cognee: {
      ...emptyAwarenessSnapshot().cognee,
      ...(payload.cognee ?? {}),
      alerts: Array.isArray(payload.cognee?.alerts) ? payload.cognee.alerts : [],
    },
    agent_drift: {
      ...emptyAwarenessSnapshot().agent_drift,
      ...(payload.agent_drift ?? {}),
      trends: Array.isArray(payload.agent_drift?.trends)
        ? payload.agent_drift.trends
        : [],
    },
    project_state: {
      ...emptyAwarenessSnapshot().project_state,
      ...(payload.project_state ?? {}),
      traces: Array.isArray(payload.project_state?.traces)
        ? payload.project_state.traces
        : [],
    },
    feature_flag: {
      ...emptyAwarenessSnapshot().feature_flag,
      ...(payload.feature_flag ?? {}),
    },
  }
}

async function defaultPatchProjectStateFlag(
  next: FeatureFlagState,
): Promise<{ state: FeatureFlagState }> {
  const res = await patchFeatureFlag(PROJECT_STATE_FLAG, { state: next })
  return { state: res.feature_flag.state }
}

function redirectToLogin(path: string) {
  if (typeof window === "undefined") return
  window.location.assign(`/login?next=${encodeURIComponent(path)}`)
}

export interface CrossTaskAwarenessPanelProps {
  fetchSnapshot?: FetchAwarenessSnapshot
  patchFlag?: PatchProjectStateFlag
  eventTransport?: EventTransport
  initialSnapshot?: CrossTaskAwarenessSnapshot | null
  nowImpl?: () => number
  testId?: string
}

type TogglePhase = "idle" | "confirm" | "saving"

export function CrossTaskAwarenessPanel({
  fetchSnapshot = defaultFetchAwarenessSnapshot,
  patchFlag = defaultPatchProjectStateFlag,
  eventTransport,
  initialSnapshot = null,
  nowImpl,
  testId = "cross-task-awareness-panel",
}: CrossTaskAwarenessPanelProps) {
  const [snapshot, setSnapshot] = React.useState<CrossTaskAwarenessSnapshot>(
    () => initialSnapshot ?? emptyAwarenessSnapshot(),
  )
  const [loading, setLoading] = React.useState(initialSnapshot == null)
  const [error, setError] = React.useState<string | null>(null)
  const [lastFetchAt, setLastFetchAt] = React.useState(0)
  const [clockNow, setClockNow] = React.useState(0)
  const [sseError, setSseError] = React.useState(false)
  const [togglePhase, setTogglePhase] = React.useState<TogglePhase>("idle")
  const [toggleError, setToggleError] = React.useState<string | null>(null)

  const readNow = React.useCallback(
    () => (nowImpl ? nowImpl() : Date.now()),
    [nowImpl],
  )

  const reload = React.useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await fetchSnapshot()
      setSnapshot(next)
      const fetchedAt = readNow()
      setLastFetchAt(fetchedAt)
      setClockNow(fetchedAt)
      setSseError(false)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [fetchSnapshot, readNow])

  React.useEffect(() => {
    if (initialSnapshot) return
    const timer = window.setTimeout(() => {
      void reload()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [initialSnapshot, reload])

  React.useEffect(() => {
    const handler = (event: AwarenessEvent) => {
      if (
        event.event !== CROSS_TASK_AWARENESS_EVENT &&
        event.event !== "heartbeat"
      ) {
        return
      }
      setSseError(false)
      void reload()
    }
    const onError = () => {
      setClockNow(readNow())
      setSseError(true)
    }
    if (eventTransport) {
      const handle = eventTransport(handler, onError)
      return () => handle?.close?.()
    }
    const handle = subscribeEvents(
      (ev) =>
        handler({
          event: ev.event,
          data: (ev.data ?? {}) as Record<string, unknown>,
        }),
      onError,
    )
    return () => handle?.close?.()
  }, [eventTransport, readNow, reload])

  const handleToggleIntent = React.useCallback(() => {
    setToggleError(null)
    setTogglePhase((phase) => (phase === "confirm" ? "idle" : "confirm"))
  }, [])

  const handleToggleConfirm = React.useCallback(async () => {
    if (!snapshot.feature_flag.can_toggle || togglePhase === "saving") return
    const next = nextFlagState(snapshot.feature_flag.state)
    setTogglePhase("saving")
    setToggleError(null)
    try {
      const patched = await patchFlag(next)
      setSnapshot((current) => ({
        ...current,
        feature_flag: { ...current.feature_flag, state: patched.state },
      }))
      setTogglePhase("idle")
    } catch (exc) {
      if (exc instanceof ApiError && (exc.status === 401 || exc.status === 403)) {
        setToggleError("ToggleAuthRefused")
        redirectToLogin("/admin/cross-task-awareness")
        setTogglePhase("idle")
        return
      }
      setToggleError(exc instanceof Error ? exc.message : String(exc))
      setTogglePhase("confirm")
    }
  }, [patchFlag, snapshot.feature_flag, togglePhase])

  const showStale = shouldShowStaleBanner(lastFetchAt, clockNow, sseError)
  const traces = lastProjectStateQueries(snapshot.project_state.traces, 10)
  const flagNext = nextFlagState(snapshot.feature_flag.state)
  const slo = snapshot.project_state.slo

  return (
    <section
      data-testid={testId}
      data-loading={loading ? "true" : "false"}
      data-sse-error={sseError ? "true" : "false"}
      className="flex min-h-0 flex-col gap-3 rounded-md border border-border bg-background/60 p-3"
    >
      <header className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">Cross-task awareness</h2>
        <Badge
          variant="outline"
          data-testid={`${testId}-slo-status`}
          style={{
            color:
              slo.status === "ok"
                ? "var(--validation-emerald)"
                : slo.status === "breach"
                  ? "var(--critical-red)"
                  : "var(--warning-amber)",
          }}
        >
          <Gauge className="mr-1 size-3" aria-hidden="true" />
          SLO {slo.status}
        </Badge>
        <Badge variant="secondary" data-testid={`${testId}-generated-at`}>
          {snapshot.generated_at}
        </Badge>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          data-testid={`${testId}-refresh`}
          onClick={() => void reload()}
          disabled={loading}
          className="ml-auto h-7 gap-1 px-2 text-xs"
        >
          {loading ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : (
            <RefreshCw className="size-3" aria-hidden="true" />
          )}
          Refresh
        </Button>
      </header>

      {showStale ? (
        <div
          data-testid={`${testId}-sse-stale`}
          role="status"
          className="flex items-center gap-2 rounded-md border border-amber-400/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200"
        >
          <WifiOff className="size-3.5" aria-hidden="true" />
          Real-time updates disconnected; dashboard is auto-reconnecting.
        </div>
      ) : (
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
          className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-300"
        >
          {error}
        </div>
      )}

      <div
        data-testid={`${testId}-flag`}
        className="flex flex-wrap items-center gap-2 rounded-md border border-border/70 px-3 py-2 text-xs"
      >
        <Flag className="size-4" aria-hidden="true" />
        <span className="font-semibold">{snapshot.feature_flag.flag_name}</span>
        <Badge
          variant="outline"
          data-testid={`${testId}-flag-state`}
          className="font-mono"
        >
          {snapshot.feature_flag.state}
        </Badge>
        {!snapshot.feature_flag.can_toggle && (
          <span
            data-testid={`${testId}-flag-readonly`}
            className="inline-flex items-center gap-1 text-[10px] text-muted-foreground"
          >
            <ShieldAlert className="size-3" aria-hidden="true" />
            admin required
          </span>
        )}
        <Button
          type="button"
          size="sm"
          variant="secondary"
          data-testid={`${testId}-flag-toggle`}
          disabled={!snapshot.feature_flag.can_toggle || togglePhase === "saving"}
          onClick={handleToggleIntent}
          className="ml-auto h-7 gap-1 px-2 text-xs"
        >
          {togglePhase === "saving" ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : (
            <CheckCircle2 className="size-3" aria-hidden="true" />
          )}
          Toggle
        </Button>
        {togglePhase === "confirm" && (
          <Button
            type="button"
            size="sm"
            variant="destructive"
            data-testid={`${testId}-flag-confirm`}
            onClick={() => void handleToggleConfirm()}
            className="h-7 px-2 text-xs"
          >
            Confirm {flagNext}
          </Button>
        )}
        {toggleError && (
          <span
            data-testid={`${testId}-flag-error`}
            className="w-full text-[10px] text-rose-300"
          >
            {toggleError}
          </span>
        )}
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <MemoryUsageTile rows={snapshot.memory_usage} />
        <DriftAlertsList
          entityCount={snapshot.cognee.entity_count}
          status={snapshot.cognee.drift_status}
          alerts={snapshot.cognee.alerts}
        />
        <AgentDriftTile
          rows={snapshot.agent_drift.trends}
          generatedAt={snapshot.agent_drift.generated_at}
        />
      </div>

      <section
        data-testid={`${testId}-project-state`}
        className="flex flex-col gap-2 rounded-md border border-border bg-background/60 p-3"
      >
        <header className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold">Project-state API</h3>
          <Badge variant="outline" data-testid={`${testId}-p95`}>
            p95 {slo.p95_latency_sec == null ? "n/a" : `${slo.p95_latency_sec.toFixed(3)}s`}
          </Badge>
          <Badge variant="outline" data-testid={`${testId}-budget-exceeded`}>
            budget exceeded {slo.budget_exceeded_count}
          </Badge>
        </header>
        <Separator />
        {traces.length === 0 ? (
          <div
            data-testid={`${testId}-queries-empty`}
            className="rounded-md border border-dashed border-border/60 px-3 py-4 text-xs text-muted-foreground"
          >
            No project-state queries recorded.
          </div>
        ) : (
          <ol
            data-testid={`${testId}-queries`}
            className="flex flex-col gap-1.5"
          >
            {traces.map((trace, idx) => (
              <li
                key={`${trace.ticket}:${trace.captured_at}:${idx}`}
                data-testid={`${testId}-query-${idx}`}
                className="rounded-md border border-border/70 px-3 py-2 text-xs"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono">{trace.ticket}</code>
                  <Badge variant={trace.cache_hit ? "secondary" : "outline"}>
                    {trace.cache_hit ? "cache" : "fresh"}
                  </Badge>
                  <span className="ml-auto inline-flex items-center gap-1 font-mono text-[10px] text-muted-foreground">
                    <Clock className="size-3" aria-hidden="true" />
                    {trace.total_latency_sec.toFixed(3)}s
                  </span>
                </div>
                <div
                  data-testid={`${testId}-query-${idx}-axis`}
                  className="mt-1 grid grid-cols-3 gap-2 font-mono text-[10px] text-muted-foreground"
                >
                  <span>structural {trace.axis_latency_sec.structural ?? 0}s</span>
                  <span>temporal {trace.axis_latency_sec.temporal ?? 0}s</span>
                  <span>causal {trace.axis_latency_sec.causal ?? 0}s</span>
                </div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </section>
  )
}

export default CrossTaskAwarenessPanel

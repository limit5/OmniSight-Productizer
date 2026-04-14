"use client"

/**
 * Phase 50A — Pipeline Timeline.
 *
 * Horizontal stepper that shows every pipeline phase with its lifecycle
 * state (idle / active / done / overdue) plus a velocity readout. Polls
 * the /pipeline/timeline endpoint every 10s (cheap, memory-only state
 * on the backend) and refreshes on any pipeline SSE event.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { Activity, AlertTriangle, CheckCircle2, Circle, Clock3, Zap } from "lucide-react"
import { PanelHelp } from "@/components/omnisight/panel-help"
import {
  type PipelineTimeline,
  type PipelineTimelineStep,
  type SSEEvent,
  getPipelineTimeline,
  subscribeEvents,
} from "@/lib/api"

const POLL_MS = 10_000

const STATUS_STYLE: Record<
  PipelineTimelineStep["status"],
  { color: string; Icon: typeof Circle; label: string }
> = {
  idle:    { color: "var(--neural-muted,#64748b)",     Icon: Circle,        label: "IDLE" },
  active:  { color: "var(--neural-blue,#60a5fa)",      Icon: Activity,      label: "ACTIVE" },
  done:    { color: "var(--validation-emerald,#10b981)", Icon: CheckCircle2, label: "DONE" },
  overdue: { color: "var(--critical-red,#ef4444)",     Icon: AlertTriangle, label: "OVERDUE" },
}

function formatDuration(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return "—"
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  if (sec < 86_400) return `${(sec / 3600).toFixed(1)}h`
  return `${(sec / 86_400).toFixed(1)}d`
}

function formatEta(iso: string | null): string {
  if (!iso) return "—"
  try {
    const t = new Date(iso)
    const delta = (t.getTime() - Date.now()) / 1000
    if (delta < 0) return "now"
    return `in ${formatDuration(delta)}`
  } catch {
    return "—"
  }
}

export function PipelineTimeline() {
  const [data, setData] = useState<PipelineTimeline | null>(null)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    try {
      const info = await getPipelineTimeline()
      if (!mountedRef.current) return
      setData(info)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh()
    const interval = setInterval(() => void refresh(), POLL_MS)
    const sub = subscribeEvents((ev: SSEEvent) => {
      // Any pipeline lifecycle change may shift the timeline — rather
      // than narrow-match, opportunistically refresh on "pipeline" +
      // "invoke" events. Cheap endpoint, small payload.
      if (ev.event === "pipeline" || ev.event === "invoke") {
        void refresh()
      }
    })
    return () => {
      mountedRef.current = false
      clearInterval(interval)
      sub.close()
    }
  }, [refresh])

  return (
    <section
      className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]"
      aria-label="Pipeline Timeline"
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <div className="flex items-center gap-2">
          <Clock3 className="w-4 h-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
          <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
            PIPELINE TIMELINE
          </h2>
          <PanelHelp doc="panels-overview" />
        </div>
        {data && (
          <span className="font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)] flex items-center gap-3 tabular-nums shrink-0">
            <span className="flex items-center gap-1" style={{ minWidth: 56 }}>
              <Zap className="w-3 h-3 shrink-0" aria-hidden />
              <span aria-label="tasks completed in the last 7 days" className="text-right inline-block" style={{ minWidth: 36 }}>
                {data.velocity.tasks_completed_7d}/7d
              </span>
            </span>
            <span
              aria-label="average step duration"
              className="inline-block text-right truncate"
              style={{ minWidth: 70, maxWidth: 90 }}
              title={`avg step duration ${formatDuration(data.velocity.avg_step_seconds)}`}
            >
              AVG {formatDuration(data.velocity.avg_step_seconds)}
            </span>
            <span
              aria-label="pipeline completion estimate"
              className="inline-block text-right truncate"
              style={{ minWidth: 70, maxWidth: 110 }}
              title={`ETA ${formatEta(data.velocity.eta_completion)}`}
            >
              ETA {formatEta(data.velocity.eta_completion)}
            </span>
          </span>
        )}
      </header>

      {error && (
        <div
          className="px-3 py-1.5 flex items-center justify-between gap-2 font-mono text-[10px] text-[var(--critical-red,#ef4444)]"
          role="alert"
        >
          <span className="truncate">{error}</span>
          <button
            onClick={() => void refresh()}
            className="px-1.5 py-0.5 rounded-sm border border-current hover:bg-current/10"
          >
            RETRY
          </button>
        </div>
      )}

      {data && (
        <ol
          className="relative flex flex-col md:flex-row md:items-stretch gap-2 p-3 overflow-x-auto"
          aria-label="pipeline phases"
        >
          {data.steps.map((step, idx) => {
            const style = STATUS_STYLE[step.status]
            const { Icon } = style
            const elapsed =
              step.started_at && !step.completed_at
                ? (Date.now() - new Date(step.started_at).getTime()) / 1000
                : null
            const totalSec =
              step.started_at && step.completed_at
                ? (new Date(step.completed_at).getTime() - new Date(step.started_at).getTime()) / 1000
                : null
            return (
              <li
                key={step.id}
                data-testid={`timeline-step-${step.id}`}
                data-status={step.status}
                className="relative flex-1 min-w-[140px] rounded-sm border p-2 flex flex-col gap-1.5 overflow-hidden"
                style={{
                  borderColor: style.color,
                  boxShadow:
                    step.status === "active"
                      ? `0 0 18px -6px ${style.color}, inset 0 0 24px -14px ${style.color}`
                      : undefined,
                }}
              >
                {/* top-left step index + bottom-right status pill */}
                <div className="flex items-center justify-between font-mono text-[8px] tracking-[0.25em] opacity-70">
                  <span>STEP_{String(idx + 1).padStart(2, "0")}</span>
                  <span className="flex items-center gap-1" style={{ color: style.color }}>
                    <Icon className="w-3 h-3" aria-hidden />
                    {style.label}
                  </span>
                </div>

                <div className="flex items-center gap-1.5">
                  <span
                    className="font-mono font-bold text-[12px] tracking-[0.08em] leading-tight min-w-0 flex-1 whitespace-normal [word-break:keep-all]"
                    style={{ color: style.color }}
                  >
                    {step.name}
                  </span>
                </div>

                <span className="font-mono text-[9px] text-[var(--muted-foreground,#94a3b8)] truncate">
                  {step.npi_phase}
                  {!step.auto_advance && (
                    <span className="ml-1 text-[var(--fui-orange,#f59e0b)]">· manual</span>
                  )}
                </span>

                <div className="font-mono text-[9.5px] text-[var(--muted-foreground,#94a3b8)] leading-tight">
                  {step.status === "done" && totalSec !== null && (
                    <span>took {formatDuration(totalSec)}</span>
                  )}
                  {step.status === "active" && elapsed !== null && (
                    <span>{formatDuration(elapsed)} elapsed</span>
                  )}
                  {step.status === "overdue" && step.deadline_at && (
                    <span className="text-[var(--critical-red,#ef4444)]">past deadline</span>
                  )}
                  {step.status === "idle" && <span>queued</span>}
                </div>
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}

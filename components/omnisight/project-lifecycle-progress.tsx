"use client"

/**
 * OP-1785 - project-scoped lifecycle progress.
 *
 * Joins the existing frontend data surfaces for one project:
 * project_runs aggregation, pipeline timeline, and artifact listing.
 * The caller must pass the path pid; this component aligns the active
 * project context before issuing requests so existing endpoints receive
 * the X-Project-Id header without any backend changes.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type { LucideIcon } from "lucide-react"
import {
  Activity,
  AlertTriangle,
  Archive,
  Box,
  CheckCircle2,
  Clock3,
  FileText,
  Loader2,
  PackageCheck,
  RefreshCw,
  Rocket,
  ScrollText,
} from "lucide-react"
import {
  getArtifactDownloadUrl,
  getPipelineTimeline,
  listArtifacts,
  listProjectRuns,
  subscribeEvents,
  type ArtifactItem,
  type PipelineTimeline,
  type PipelineTimelineStep,
  type ProjectRun,
  type WorkflowRunSummary,
} from "@/lib/api"
import { useProject } from "@/lib/project-context"

const POLL_MS = 15_000

type LifecycleStageId = "spec" | "build" | "test" | "deliver"
type LifecycleStatus = "done" | "active" | "waiting" | "blocked"

interface LifecycleStage {
  id: LifecycleStageId
  label: string
  description: string
  Icon: LucideIcon
  status: LifecycleStatus
  detail: string
  evidence: string
}

interface ProgressState {
  runs: ProjectRun[]
  pipeline: PipelineTimeline | null
  artifacts: ArtifactItem[]
}

const STAGE_META: Record<
  LifecycleStageId,
  { label: string; description: string; Icon: LucideIcon; tokens: string[] }
> = {
  spec: {
    label: "Spec",
    description: "Project record and intake context are available.",
    Icon: ScrollText,
    tokens: ["spec", "intent", "intake", "requirements"],
  },
  build: {
    label: "Build",
    description: "Implementation and packaging workflow activity.",
    Icon: Box,
    tokens: ["build", "compile", "package", "implementation"],
  },
  test: {
    label: "Test",
    description: "Validation, simulation, and verification workflow activity.",
    Icon: CheckCircle2,
    tokens: ["test", "validation", "verify", "simulation", "qa"],
  },
  deliver: {
    label: "Deliver",
    description: "Release, report, store, or artifact handoff.",
    Icon: PackageCheck,
    tokens: ["deliver", "release", "store", "report", "artifact", "publish"],
  },
}

const STATUS_STYLE: Record<
  LifecycleStatus,
  { label: string; color: string; bg: string; Icon: LucideIcon }
> = {
  done: {
    label: "DONE",
    color: "var(--validation-emerald,#10b981)",
    bg: "rgba(16,185,129,0.12)",
    Icon: CheckCircle2,
  },
  active: {
    label: "ACTIVE",
    color: "var(--neural-cyan,#67e8f9)",
    bg: "rgba(103,232,249,0.12)",
    Icon: Activity,
  },
  waiting: {
    label: "WAITING",
    color: "var(--fui-orange,#f59e0b)",
    bg: "rgba(245,158,11,0.12)",
    Icon: Clock3,
  },
  blocked: {
    label: "BLOCKED",
    color: "var(--destructive,#ef4444)",
    bg: "rgba(239,68,68,0.12)",
    Icon: AlertTriangle,
  },
}

function formatCount(n: number): string {
  return n.toLocaleString("en-US")
}

function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

function formatAge(ts: number | string | null | undefined): string {
  if (ts == null) return "-"
  const ms = typeof ts === "number" ? ts * 1000 : new Date(ts).getTime()
  if (!Number.isFinite(ms)) return "-"
  const sec = Math.max(0, Math.floor((Date.now() - ms) / 1000))
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86_400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86_400)}d ago`
}

function latestRun(runs: ProjectRun[]): ProjectRun | null {
  return runs.slice().sort((a, b) => b.created_at - a.created_at)[0] ?? null
}

function allChildren(runs: ProjectRun[]): WorkflowRunSummary[] {
  return runs.flatMap((run) => run.children)
}

function textIncludesAny(value: string, tokens: string[]): boolean {
  const lower = value.toLowerCase()
  return tokens.some((token) => lower.includes(token))
}

function stepMatchesStage(step: PipelineTimelineStep, stage: LifecycleStageId): boolean {
  const haystack = `${step.id} ${step.name} ${step.npi_phase}`
  return textIncludesAny(haystack, STAGE_META[stage].tokens)
}

function runMatchesStage(run: WorkflowRunSummary, stage: LifecycleStageId): boolean {
  const metadataText = Object.values(run.metadata ?? {})
    .map((value) => String(value))
    .join(" ")
  const haystack = `${run.id} ${run.kind} ${run.last_step_id ?? ""} ${metadataText}`
  return textIncludesAny(haystack, STAGE_META[stage].tokens)
}

function stageStatusFromData(
  stage: LifecycleStageId,
  runs: WorkflowRunSummary[],
  steps: PipelineTimelineStep[],
  artifacts: ArtifactItem[],
): { status: LifecycleStatus; detail: string; evidence: string } {
  if (stage === "spec") {
    const specStep = steps.find((step) => stepMatchesStage(step, stage))
    if (specStep?.status === "overdue") {
      return { status: "blocked", detail: specStep.name, evidence: "pipeline overdue" }
    }
    if (specStep?.status === "active") {
      return { status: "active", detail: specStep.name, evidence: "pipeline active" }
    }
    return { status: "done", detail: "Project scope resolved", evidence: "tenant project context" }
  }

  const matchingSteps = steps.filter((step) => stepMatchesStage(step, stage))
  const matchingRuns = runs.filter((run) => runMatchesStage(run, stage))
  const hasArtifacts = stage === "deliver" && artifacts.length > 0

  if (matchingSteps.some((step) => step.status === "overdue") || matchingRuns.some((run) => run.status === "failed")) {
    return {
      status: "blocked",
      detail: "Needs operator attention",
      evidence: matchingRuns.length ? `${matchingRuns.length} matching run(s)` : "pipeline overdue",
    }
  }
  if (
    matchingSteps.some((step) => step.status === "active")
    || matchingRuns.some((run) => run.status === "running" || run.status === "executing")
  ) {
    return {
      status: "active",
      detail: matchingRuns[0]?.kind || matchingSteps.find((step) => step.status === "active")?.name || "In progress",
      evidence: matchingRuns.length ? `${matchingRuns.length} matching run(s)` : "pipeline active",
    }
  }
  if (
    hasArtifacts
    || matchingSteps.some((step) => step.status === "done")
    || matchingRuns.some((run) => run.status === "completed")
  ) {
    return {
      status: "done",
      detail: hasArtifacts ? `${artifacts.length} artifact(s) published` : "Completed",
      evidence: hasArtifacts ? "artifact endpoint" : `${matchingRuns.length || matchingSteps.length} matching item(s)`,
    }
  }
  if (matchingRuns.length > 0 || matchingSteps.length > 0) {
    return {
      status: "waiting",
      detail: "Queued",
      evidence: `${matchingRuns.length + matchingSteps.length} planned item(s)`,
    }
  }
  return { status: "waiting", detail: "No scoped signal yet", evidence: "no matching run/pipeline data" }
}

function buildStages(data: ProgressState): LifecycleStage[] {
  const runs = allChildren(data.runs)
  const steps = data.pipeline?.steps ?? []
  return (["spec", "build", "test", "deliver"] as LifecycleStageId[]).map((id) => {
    const meta = STAGE_META[id]
    const derived = stageStatusFromData(id, runs, steps, data.artifacts)
    return {
      id,
      label: meta.label,
      description: meta.description,
      Icon: meta.Icon,
      ...derived,
    }
  })
}

function describeError(exc: unknown): string {
  if (exc instanceof Error) return exc.message
  return String(exc)
}

export function ProjectLifecycleProgress({ projectId }: { projectId: string }) {
  const { currentProjectId, projectChangeEpoch, switchProject } = useProject()
  const [data, setData] = useState<ProgressState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)
  const scoped = currentProjectId === projectId

  useEffect(() => {
    if (currentProjectId !== projectId) {
      switchProject(projectId)
    }
  }, [currentProjectId, projectId, switchProject])

  const refresh = useCallback(async () => {
    if (!scoped) return
    setLoading(true)
    setError(null)
    try {
      const [runs, pipeline, artifacts] = await Promise.all([
        listProjectRuns(projectId, { limit: 10 }),
        getPipelineTimeline().catch(() => null),
        listArtifacts().catch(() => []),
      ])
      if (!mountedRef.current) return
      setData({ runs, pipeline, artifacts })
    } catch (exc) {
      if (!mountedRef.current) return
      setError(describeError(exc))
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [projectId, scoped])

  useEffect(() => {
    mountedRef.current = true
    if (!scoped) return () => { mountedRef.current = false }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount populates state from existing endpoints
    void refresh()
    const interval = setInterval(() => void refresh(), POLL_MS)
    const sub = subscribeEvents((ev) => {
      if (
        ev.event === "workflow_updated"
        || ev.event === "pipeline"
        || ev.event === "artifact_created"
        || ev.event === "invoke"
      ) {
        void refresh()
      }
    })
    return () => {
      mountedRef.current = false
      clearInterval(interval)
      sub.close()
    }
  }, [refresh, scoped, projectChangeEpoch])

  const stages = useMemo(() => data ? buildStages(data) : [], [data])
  const latest = useMemo(() => data ? latestRun(data.runs) : null, [data])
  const childRuns = useMemo(() => data ? allChildren(data.runs) : [], [data])
  const running = childRuns.filter((run) => run.status === "running" || run.status === "executing").length
  const completed = childRuns.filter((run) => run.status === "completed").length
  const failed = childRuns.filter((run) => run.status === "failed").length

  if (!scoped) {
    return (
      <section
        className="rounded border border-[var(--border)] bg-[var(--card)] p-5 font-mono text-xs text-[var(--muted-foreground)]"
        data-testid="project-progress-scoping"
      >
        <Loader2 size={14} className="mr-2 inline animate-spin" />
        Scoping project data...
      </section>
    )
  }

  return (
    <section className="flex flex-col gap-4" data-testid="project-lifecycle-progress">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <StatTile label="project runs" value={formatCount(data?.runs.length ?? 0)} />
        <StatTile label="workflow runs" value={formatCount(childRuns.length)} />
        <StatTile label="active" value={formatCount(running)} />
        <StatTile label="artifacts" value={formatCount(data?.artifacts.length ?? 0)} />
      </div>

      <section className="holo-glass-simple corner-brackets-full rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] px-3 py-2">
          <div className="flex items-center gap-2">
            <Rocket className="h-4 w-4 text-[var(--neural-cyan,#67e8f9)]" aria-hidden />
            <h2 className="font-mono text-sm tracking-wider text-[var(--neural-cyan,#67e8f9)]">
              PROJECT LIFECYCLE
            </h2>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-sm border border-[var(--border)] px-2 py-1 font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] disabled:opacity-50"
            data-testid="project-progress-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        {error && (
          <div
            className="border-b border-[var(--destructive)]/30 px-3 py-2 font-mono text-[10px] text-[var(--destructive)]"
            role="alert"
            data-testid="project-progress-error"
          >
            {error}
          </div>
        )}

        {loading && !data ? (
          <div className="flex items-center justify-center p-10 font-mono text-xs text-[var(--muted-foreground)]" data-testid="project-progress-loading">
            <Loader2 size={14} className="mr-2 animate-spin" />
            Loading lifecycle...
          </div>
        ) : (
          <ol className="grid grid-cols-1 gap-2 p-3 md:grid-cols-4" aria-label="project lifecycle stages">
            {stages.map((stage, idx) => {
              const status = STATUS_STYLE[stage.status]
              const StageIcon = stage.Icon
              const StatusIcon = status.Icon
              return (
                <li
                  key={stage.id}
                  className="relative min-h-[176px] rounded-sm border p-3"
                  style={{
                    borderColor: status.color,
                    background: status.bg,
                    boxShadow: stage.status === "active"
                      ? `0 0 18px -8px ${status.color}, inset 0 0 24px -18px ${status.color}`
                      : undefined,
                  }}
                  data-testid={`project-progress-stage-${stage.id}`}
                  data-status={stage.status}
                >
                  <div className="mb-3 flex items-center justify-between font-mono text-[8px] tracking-[0.25em] text-[var(--muted-foreground)]">
                    <span>PHASE_{String(idx + 1).padStart(2, "0")}</span>
                    <span className="inline-flex items-center gap-1" style={{ color: status.color }}>
                      <StatusIcon size={12} />
                      {status.label}
                    </span>
                  </div>
                  <div className="mb-3 flex items-center gap-2">
                    <StageIcon size={18} style={{ color: status.color }} />
                    <div className="min-w-0">
                      <h3 className="font-mono text-sm font-semibold uppercase tracking-wider text-[var(--foreground)]">
                        {stage.label}
                      </h3>
                      <p className="font-mono text-[10px] leading-snug text-[var(--muted-foreground)]">
                        {stage.description}
                      </p>
                    </div>
                  </div>
                  <div className="mt-auto rounded-sm border border-[var(--border)]/70 bg-[var(--background)]/40 p-2 font-mono text-[10px]">
                    <div className="truncate text-[var(--foreground)]" title={stage.detail}>
                      {stage.detail}
                    </div>
                    <div className="mt-1 truncate text-[var(--muted-foreground)]" title={stage.evidence}>
                      {stage.evidence}
                    </div>
                  </div>
                </li>
              )
            })}
          </ol>
        )}
      </section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(320px,0.9fr)]">
        <section className="rounded border border-[var(--border)] bg-[var(--card)]">
          <header className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2">
            <h2 className="inline-flex items-center gap-2 font-mono text-sm">
              <Clock3 size={14} />
              Latest project run
            </h2>
            {latest && (
              <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
                {formatAge(latest.created_at)}
              </span>
            )}
          </header>
          {!latest ? (
            <div className="p-6 font-mono text-xs text-[var(--muted-foreground)]" data-testid="project-progress-runs-empty">
              No project runs are available for this scope yet.
            </div>
          ) : (
            <div className="p-3" data-testid="project-progress-latest-run">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm font-semibold">{latest.label}</span>
                <span className="rounded border border-[var(--border)] px-2 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)]">
                  {latest.id}
                </span>
              </div>
              <div className="mb-3 grid grid-cols-2 gap-2 md:grid-cols-4">
                <StatTile compact label="total" value={formatCount(latest.summary.total)} />
                <StatTile compact label="running" value={formatCount(latest.summary.running)} />
                <StatTile compact label="completed" value={formatCount(latest.summary.completed)} />
                <StatTile compact label="failed" value={formatCount(latest.summary.failed)} />
              </div>
              <ul className="flex flex-col gap-1">
                {latest.children.slice(0, 6).map((run) => (
                  <li
                    key={run.id}
                    className="grid grid-cols-[minmax(0,1fr)_auto] gap-2 rounded-sm border border-[var(--border)] px-2 py-1.5 font-mono text-[10px]"
                    data-testid={`project-progress-run-${run.id}`}
                  >
                    <span className="truncate" title={`${run.kind} ${run.id}`}>
                      {run.kind || run.id}
                    </span>
                    <span className={run.status === "failed" ? "text-[var(--destructive)]" : "text-[var(--muted-foreground)]"}>
                      {run.status}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        <section className="rounded border border-[var(--border)] bg-[var(--card)]">
          <header className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2">
            <h2 className="inline-flex items-center gap-2 font-mono text-sm">
              <Archive size={14} />
              Delivery artifacts
            </h2>
            <span className="font-mono text-[10px] text-[var(--muted-foreground)]">
              {completed} complete / {failed} failed
            </span>
          </header>
          {!data || data.artifacts.length === 0 ? (
            <div className="p-6 font-mono text-xs text-[var(--muted-foreground)]" data-testid="project-progress-artifacts-empty">
              No artifacts have been published for this project scope.
            </div>
          ) : (
            <ul className="flex max-h-[320px] flex-col overflow-y-auto p-2" data-testid="project-progress-artifacts">
              {data.artifacts.slice(0, 12).map((artifact) => (
                <li
                  key={artifact.id}
                  className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-sm px-2 py-1.5 font-mono text-[10px] hover:bg-[var(--secondary)]/30"
                  data-testid={`project-progress-artifact-${artifact.id}`}
                >
                  <FileText size={12} className="text-[var(--neural-cyan,#67e8f9)]" />
                  <div className="min-w-0">
                    <a
                      href={getArtifactDownloadUrl(artifact.id)}
                      className="block truncate text-[var(--foreground)] hover:underline"
                      title={artifact.name}
                    >
                      {artifact.name}
                    </a>
                    <span className="text-[var(--muted-foreground)]">
                      {artifact.type} / {formatBytes(artifact.size)} / {formatAge(artifact.created_at)}
                    </span>
                  </div>
                  <span className="text-[var(--muted-foreground)]">{artifact.version ?? ""}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </section>
  )
}

function StatTile({
  label,
  value,
  compact = false,
}: {
  label: string
  value: string
  compact?: boolean
}) {
  return (
    <div className={`rounded border border-[var(--border)] bg-[var(--card)] font-mono ${compact ? "p-2" : "p-3"}`}>
      <div className="text-[9px] uppercase tracking-[0.2em] text-[var(--muted-foreground)]">
        {label}
      </div>
      <div className={`${compact ? "text-sm" : "text-xl"} tabular-nums text-[var(--foreground)]`}>
        {value}
      </div>
    </div>
  )
}

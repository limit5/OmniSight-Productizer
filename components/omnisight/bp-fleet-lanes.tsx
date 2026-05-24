"use client"

/**
 * OP-1504 — WP.10 BP Fleet UI Lanes.
 *
 * 4-lane dispatch board for the Blueprint fleet:
 *   1. Active     — running / booting / materializing / awaiting confirmation
 *   2. Scheduled  — queued (idle / warning) agents waiting to dispatch
 *   3. Ambient    — long-running background agents (watchdog / daemon / rtc)
 *   4. History    — completed / failed archive
 *
 * Each card opens a detail panel with the execution envelope (sub_tasks,
 * workspace, model, progress) and a Revoke action when revocable.
 *
 * Inspired by Warp's `AgentManagementView` + `AmbientAgentRTC` flag —
 * independently implemented, no upstream source consulted.
 */

import { useCallback, useState } from "react"
import { Activity, Calendar, Cpu, History as HistoryIcon, X } from "lucide-react"

import { Block } from "./block"

export const LANE_KEYS = ["active", "scheduled", "ambient", "history"] as const
export type FleetLaneKey = (typeof LANE_KEYS)[number]

export interface FleetLaneAgent {
  id: string
  name: string
  type: string
  sub_type: string
  status: string
  ai_model: string | null
  progress: { current: number; total: number }
}

export interface FleetLaneDetail extends FleetLaneAgent {
  lane: FleetLaneKey
  revocable: boolean
  sub_tasks: { id: string; label: string; status: string }[]
  workspace: {
    branch: string | null
    status: string
    commit_count: number
    task_id: string | null
  }
}

export interface FleetLanesSnapshot {
  lanes: Record<FleetLaneKey, FleetLaneAgent[]>
  counts: Record<FleetLaneKey, number>
}

interface LaneVisual {
  label: string
  Icon: typeof Activity
  toneClass: string
}

const LANE_VISUALS: Record<FleetLaneKey, LaneVisual> = {
  active: {
    label: "Active",
    Icon: Activity,
    toneClass: "text-emerald-300 border-emerald-500/40 bg-emerald-500/5",
  },
  scheduled: {
    label: "Scheduled",
    Icon: Calendar,
    toneClass: "text-sky-300 border-sky-500/40 bg-sky-500/5",
  },
  ambient: {
    label: "Ambient",
    Icon: Cpu,
    toneClass: "text-violet-300 border-violet-500/40 bg-violet-500/5",
  },
  history: {
    label: "History",
    Icon: HistoryIcon,
    toneClass: "text-slate-300 border-slate-500/40 bg-slate-500/5",
  },
}

const STATUS_TONE: Record<string, string> = {
  running: "text-emerald-300",
  booting: "text-amber-300",
  materializing: "text-cyan-300",
  awaiting_confirmation: "text-yellow-300",
  idle: "text-slate-400",
  warning: "text-amber-400",
  success: "text-emerald-400",
  error: "text-rose-400",
}

/** Build a stable display string for the per-card progress badge. */
export function formatProgress(progress: { current: number; total: number }): string {
  if (!progress.total) return "—"
  return `${progress.current}/${progress.total}`
}

/** Pure helper used by the empty-state copy + tests. */
export function laneEmptyCopy(lane: FleetLaneKey): string {
  switch (lane) {
    case "active":
      return "No running agents."
    case "scheduled":
      return "Queue is empty."
    case "ambient":
      return "No ambient agents."
    case "history":
      return "No archived runs yet."
  }
}

export interface BpFleetLanesProps {
  /** Snapshot of all four lanes — see GET /bp/fleet/lanes. */
  snapshot: FleetLanesSnapshot
  /** Resolver for the detail-panel payload (GET /bp/fleet/agents/:id). */
  onLoadDetail?: (agentId: string) => Promise<FleetLaneDetail>
  /** Revoke action (POST /bp/fleet/agents/:id/revoke). */
  onRevoke?: (agentId: string) => Promise<void>
}

export function BpFleetLanes({ snapshot, onLoadDetail, onRevoke }: BpFleetLanesProps) {
  const [selected, setSelected] = useState<FleetLaneDetail | null>(null)
  const [loadingId, setLoadingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleSelect = useCallback(
    async (agentId: string) => {
      setError(null)
      if (!onLoadDetail) return
      setLoadingId(agentId)
      try {
        const detail = await onLoadDetail(agentId)
        setSelected(detail)
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc))
      } finally {
        setLoadingId(null)
      }
    },
    [onLoadDetail],
  )

  const handleRevoke = useCallback(async () => {
    if (!selected || !onRevoke) return
    setError(null)
    try {
      await onRevoke(selected.id)
      // Optimistic: reflect the revoke in the detail panel without re-fetching.
      setSelected({ ...selected, status: "error", lane: "history", revocable: false })
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [onRevoke, selected])

  return (
    <section
      data-testid="bp-fleet-lanes"
      aria-label="Blueprint fleet dispatch board"
      className="flex w-full flex-col gap-3"
    >
      <header className="flex items-center justify-between gap-2">
        <h2 className="font-mono text-sm font-semibold uppercase tracking-wider text-[var(--neural-cyan,#67e8f9)]">
          Blueprint Fleet
        </h2>
        <span className="text-[11px] text-[var(--muted-foreground,#94a3b8)]">
          WP.10 dispatch board
        </span>
      </header>

      <div
        role="list"
        aria-label="Fleet lanes"
        className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4"
      >
        {LANE_KEYS.map((key) => (
          <Lane
            key={key}
            laneKey={key}
            agents={snapshot.lanes[key] ?? []}
            count={snapshot.counts?.[key] ?? snapshot.lanes[key]?.length ?? 0}
            loadingId={loadingId}
            onSelect={handleSelect}
          />
        ))}
      </div>

      {error ? (
        <p role="alert" className="text-xs text-rose-400">
          {error}
        </p>
      ) : null}

      {selected ? (
        <DetailPanel
          detail={selected}
          onClose={() => setSelected(null)}
          onRevoke={onRevoke ? handleRevoke : undefined}
        />
      ) : null}
    </section>
  )
}

function Lane({
  laneKey,
  agents,
  count,
  loadingId,
  onSelect,
}: {
  laneKey: FleetLaneKey
  agents: FleetLaneAgent[]
  count: number
  loadingId: string | null
  onSelect: (id: string) => void
}) {
  const visual = LANE_VISUALS[laneKey]
  const Icon = visual.Icon
  return (
    <Block
      as="article"
      kind="bp.lane"
      role="listitem"
      data-testid={`fleet-lane-${laneKey}`}
      aria-label={`${visual.label} lane`}
      className={`flex min-h-[200px] flex-col gap-2 rounded-md border p-3 ${visual.toneClass}`}
    >
      <header className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-wider">
          <Icon size={14} aria-hidden="true" />
          {visual.label}
        </span>
        <span
          data-testid={`fleet-lane-${laneKey}-count`}
          className="rounded bg-black/30 px-1.5 py-0.5 font-mono text-[10px]"
        >
          {count}
        </span>
      </header>

      <div className="flex flex-col gap-1.5">
        {agents.length === 0 ? (
          <p className="text-[11px] italic text-[var(--muted-foreground,#94a3b8)]">
            {laneEmptyCopy(laneKey)}
          </p>
        ) : (
          agents.map((agent) => (
            <Block
              as="button"
              key={agent.id}
              type="button"
              blockId={agent.id}
              kind="bp.card"
              status={agent.status}
              data-testid={`fleet-card-${agent.id}`}
              data-lane={laneKey}
              onClick={() => onSelect(agent.id)}
              disabled={loadingId === agent.id}
              className="group flex flex-col gap-1 rounded border border-white/10 bg-black/30 p-2 text-left text-[11px] outline-none transition-colors hover:border-white/30 focus-visible:ring-2 focus-visible:ring-white/50 disabled:opacity-60"
            >
              <span className="flex items-center justify-between gap-2">
                <span className="truncate font-semibold text-white/90">{agent.name}</span>
                <span
                  className={`font-mono text-[10px] uppercase ${STATUS_TONE[agent.status] ?? "text-slate-300"}`}
                >
                  {agent.status}
                </span>
              </span>
              <span className="flex items-center justify-between gap-2 text-[10px] text-[var(--muted-foreground,#94a3b8)]">
                <span className="truncate">
                  {agent.sub_type ? `${agent.type} / ${agent.sub_type}` : agent.type}
                </span>
                <span className="font-mono">{formatProgress(agent.progress)}</span>
              </span>
            </Block>
          ))
        )}
      </div>
    </Block>
  )
}

function DetailPanel({
  detail,
  onClose,
  onRevoke,
}: {
  detail: FleetLaneDetail
  onClose: () => void
  onRevoke?: () => void | Promise<void>
}) {
  return (
    <Block
      as="aside"
      blockId={detail.id}
      kind="bp.detail"
      status={detail.status}
      role="dialog"
      aria-label={`Agent ${detail.name} detail`}
      data-testid="fleet-detail-panel"
      className="flex flex-col gap-3 rounded-md border border-white/15 bg-black/60 p-4 text-sm"
    >
      <header className="flex items-start justify-between gap-3">
        <div className="flex flex-col">
          <h3 className="font-mono text-sm font-semibold uppercase tracking-wider text-white">
            {detail.name}
          </h3>
          <p className="text-[11px] text-[var(--muted-foreground,#94a3b8)]">
            {detail.type}
            {detail.sub_type ? ` / ${detail.sub_type}` : ""}
            {" · "}
            <span data-testid="fleet-detail-lane">{detail.lane}</span>
          </p>
        </div>
        <button
          type="button"
          aria-label="Close detail panel"
          onClick={onClose}
          className="rounded p-1 text-white/70 outline-none hover:bg-white/10 focus-visible:ring-2 focus-visible:ring-white/50"
        >
          <X size={14} aria-hidden="true" />
        </button>
      </header>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-[12px]">
        <dt className="text-[var(--muted-foreground,#94a3b8)]">Status</dt>
        <dd className={STATUS_TONE[detail.status] ?? "text-slate-300"}>{detail.status}</dd>
        <dt className="text-[var(--muted-foreground,#94a3b8)]">Model</dt>
        <dd className="text-white/90">{detail.ai_model ?? "—"}</dd>
        <dt className="text-[var(--muted-foreground,#94a3b8)]">Progress</dt>
        <dd className="font-mono text-white/90">{formatProgress(detail.progress)}</dd>
        <dt className="text-[var(--muted-foreground,#94a3b8)]">Workspace</dt>
        <dd className="font-mono text-white/90">
          {detail.workspace.branch ?? "—"} ({detail.workspace.status})
        </dd>
      </dl>

      {detail.sub_tasks.length > 0 ? (
        <section
          data-testid="fleet-detail-subtasks"
          aria-label="Sub-tasks"
          className="flex flex-col gap-1"
        >
          <h4 className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground,#94a3b8)]">
            Timeline
          </h4>
          <ul className="flex flex-col gap-1">
            {detail.sub_tasks.map((st) => (
              <Block
                as="li"
                key={st.id}
                kind="bp.subtask"
                status={st.status}
                className="flex items-center justify-between gap-2 rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
              >
                <span className="truncate text-white/90">{st.label}</span>
                <span
                  className={`font-mono text-[10px] uppercase ${STATUS_TONE[st.status] ?? "text-slate-300"}`}
                >
                  {st.status}
                </span>
              </Block>
            ))}
          </ul>
        </section>
      ) : null}

      <footer className="flex items-center justify-end gap-2">
        {detail.revocable && onRevoke ? (
          <button
            type="button"
            data-testid="fleet-revoke-button"
            onClick={() => void onRevoke()}
            className="rounded border border-rose-500/40 bg-rose-500/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-wider text-rose-300 outline-none transition-colors hover:bg-rose-500/20 focus-visible:ring-2 focus-visible:ring-rose-400"
          >
            Revoke
          </button>
        ) : null}
      </footer>
    </Block>
  )
}

export default BpFleetLanes

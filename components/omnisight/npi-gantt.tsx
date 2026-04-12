"use client"

import { Zap, Target, Rocket, CheckCircle2, Clock, AlertTriangle } from "lucide-react"

interface NPIMilestone {
  id: string
  title: string
  track: "engineering" | "design" | "market"
  status: "pending" | "in_progress" | "completed" | "blocked"
  due_date?: string
  jira_tag?: string
}

interface NPIPhase {
  id: string
  name: string
  short_name: string
  order: number
  status: "pending" | "active" | "completed" | "blocked"
  milestones: NPIMilestone[]
}

interface NPIGanttProps {
  phases: NPIPhase[]
  isOBM: boolean
}

const TRACK_COLORS = {
  engineering: "var(--hardware-orange)",
  design: "var(--artifact-purple)",
  market: "var(--neural-blue)",
}

const STATUS_COLORS = {
  pending: "var(--muted-foreground)",
  in_progress: "var(--hardware-orange)",
  completed: "var(--validation-emerald)",
  blocked: "var(--critical-red)",
  active: "var(--hardware-orange)",
}

export function NPIGantt({ phases, isOBM }: NPIGanttProps) {
  const totalPhases = phases.length
  if (totalPhases === 0) return null

  // Calculate progress percentage per phase
  const phaseData = phases.map(phase => {
    const total = phase.milestones.length
    const completed = phase.milestones.filter(m => m.status === "completed").length
    const inProgress = phase.milestones.filter(m => m.status === "in_progress").length
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0
    return { ...phase, total, completed, inProgress, pct }
  })

  return (
    <div className="space-y-1">
      {phaseData.map((phase, idx) => {
        const statusColor = STATUS_COLORS[phase.status] || STATUS_COLORS.pending
        const barWidth = phase.pct
        const inProgressWidth = phase.total > 0 ? Math.round((phase.inProgress / phase.total) * 100) : 0

        return (
          <div key={phase.id} className="flex items-center gap-1.5">
            {/* Phase label */}
            <span className="font-mono text-[8px] w-7 text-right shrink-0" style={{ color: statusColor }}>
              {phase.short_name}
            </span>

            {/* Gantt bar */}
            <div className="flex-1 h-3 rounded-sm bg-[var(--secondary)] overflow-hidden relative">
              {/* Completed portion */}
              <div
                className="absolute inset-y-0 left-0 rounded-sm transition-all duration-500"
                style={{
                  width: `${barWidth}%`,
                  backgroundColor: STATUS_COLORS.completed,
                  opacity: 0.8,
                }}
              />
              {/* In-progress portion (stacked after completed) */}
              {inProgressWidth > 0 && (
                <div
                  className="absolute inset-y-0 rounded-sm transition-all duration-500 animate-pulse"
                  style={{
                    left: `${barWidth}%`,
                    width: `${inProgressWidth}%`,
                    backgroundColor: STATUS_COLORS.in_progress,
                    opacity: 0.6,
                  }}
                />
              )}
              {/* Blocked indicator */}
              {phase.status === "blocked" && (
                <div className="absolute inset-0 flex items-center justify-center">
                  <AlertTriangle size={8} className="text-[var(--critical-red)]" />
                </div>
              )}
            </div>

            {/* Percentage */}
            <span className="font-mono text-[8px] w-7 text-right shrink-0" style={{ color: statusColor }}>
              {barWidth}%
            </span>
          </div>
        )
      })}

      {/* Track legend (only for OBM) */}
      {isOBM && (
        <div className="flex gap-2 mt-1 pt-1 border-t border-[var(--border)]">
          {(["engineering", "design", "market"] as const).map(track => (
            <span key={track} className="flex items-center gap-0.5 font-mono text-[7px]" style={{ color: TRACK_COLORS[track] }}>
              <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: TRACK_COLORS[track] }} />
              {track.slice(0, 3).toUpperCase()}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

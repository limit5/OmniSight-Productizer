"use client"

import { AlertTriangle, CheckCircle2, Clock, Zap, Shield, Package, Rocket, Target } from "lucide-react"

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

const TRACK_CONFIG = {
  engineering: { label: "ENG", color: "var(--hardware-orange)" },
  design: { label: "DSN", color: "var(--artifact-purple)" },
  market: { label: "MKT", color: "var(--neural-blue)" },
}

const STATUS_CONFIG: Record<string, { color: string; glow: boolean; pulse: boolean }> = {
  pending: { color: "var(--muted-foreground)", glow: false, pulse: false },
  active: { color: "var(--hardware-orange)", glow: true, pulse: true },
  in_progress: { color: "var(--hardware-orange)", glow: true, pulse: true },
  completed: { color: "var(--validation-emerald)", glow: true, pulse: false },
  blocked: { color: "var(--critical-red)", glow: true, pulse: true },
}

function getPhaseIcon(shortName: string) {
  const icons: Record<string, typeof Zap> = {
    PRD: Target, EIV: Shield, POC: Zap, HVT: Package,
    EVT: Zap, DVT: Shield, PVT: Package, MP: Rocket,
  }
  return icons[shortName] || Zap
}

export function NPIGantt({ phases, isOBM }: NPIGanttProps) {
  if (phases.length === 0) return null

  const phaseData = phases.map(phase => {
    const total = phase.milestones.length
    const completed = phase.milestones.filter(m => m.status === "completed").length
    const inProgress = phase.milestones.filter(m => m.status === "in_progress").length
    const blocked = phase.milestones.filter(m => m.status === "blocked").length
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0
    const inPct = total > 0 ? Math.round((inProgress / total) * 100) : 0
    return { ...phase, total, completed, inProgress, blocked, pct, inPct }
  })

  return (
    <div className="space-y-0.5">
      {/* Column headers */}
      <div className="flex items-center gap-1.5 mb-1">
        <span className="font-mono text-[7px] text-[var(--muted-foreground)] w-8 text-right opacity-50">PHASE</span>
        <div className="flex-1 flex items-center justify-between">
          <span className="font-mono text-[7px] text-[var(--muted-foreground)] opacity-50">0%</span>
          <span className="font-mono text-[7px] text-[var(--muted-foreground)] opacity-50">50%</span>
          <span className="font-mono text-[7px] text-[var(--muted-foreground)] opacity-50">100%</span>
        </div>
        <span className="w-6" />
      </div>

      {phaseData.map((phase) => {
        const cfg = STATUS_CONFIG[phase.status] || STATUS_CONFIG.pending
        const PhaseIcon = getPhaseIcon(phase.short_name)

        return (
          <div key={phase.id} className="group">
            <div className="flex items-center gap-1.5">
              {/* Phase icon + label */}
              <div className="flex items-center gap-1 w-8 shrink-0 justify-end">
                <PhaseIcon
                  size={8}
                  style={{ color: cfg.color }}
                  className={cfg.pulse ? "animate-pulse" : ""}
                />
                <span
                  className="font-mono text-[8px] font-bold"
                  style={{ color: cfg.color }}
                >
                  {phase.short_name}
                </span>
              </div>

              {/* Gantt bar container */}
              <div className="flex-1 relative">
                {/* Background track with grid lines */}
                <div className="h-4 rounded-sm overflow-hidden relative"
                  style={{
                    backgroundColor: "color-mix(in srgb, var(--secondary) 60%, transparent)",
                    border: `1px solid color-mix(in srgb, ${cfg.color} 15%, transparent)`,
                  }}
                >
                  {/* 25% grid lines */}
                  {[25, 50, 75].map(pct => (
                    <div
                      key={pct}
                      className="absolute inset-y-0 w-px opacity-10"
                      style={{ left: `${pct}%`, backgroundColor: "var(--foreground)" }}
                    />
                  ))}

                  {/* Completed bar with glow */}
                  {phase.pct > 0 && (
                    <div
                      className="absolute inset-y-0 left-0 transition-all duration-700 ease-out"
                      style={{
                        width: `${phase.pct}%`,
                        background: `linear-gradient(90deg, color-mix(in srgb, ${STATUS_CONFIG.completed.color} 40%, transparent), color-mix(in srgb, ${STATUS_CONFIG.completed.color} 70%, transparent))`,
                        boxShadow: `inset 0 0 8px color-mix(in srgb, ${STATUS_CONFIG.completed.color} 30%, transparent)`,
                      }}
                    >
                      {/* Scan line effect */}
                      <div
                        className="absolute inset-0 opacity-30"
                        style={{
                          background: "repeating-linear-gradient(90deg, transparent, transparent 2px, rgba(255,255,255,0.05) 2px, rgba(255,255,255,0.05) 4px)",
                        }}
                      />
                    </div>
                  )}

                  {/* In-progress bar with pulse glow */}
                  {phase.inPct > 0 && (
                    <div
                      className="absolute inset-y-0 transition-all duration-700"
                      style={{
                        left: `${phase.pct}%`,
                        width: `${phase.inPct}%`,
                        background: `linear-gradient(90deg, color-mix(in srgb, ${STATUS_CONFIG.in_progress.color} 30%, transparent), color-mix(in srgb, ${STATUS_CONFIG.in_progress.color} 50%, transparent))`,
                        animation: "pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite",
                      }}
                    />
                  )}

                  {/* Leading edge glow (current progress marker) */}
                  {(phase.pct > 0 && phase.pct < 100) && (
                    <div
                      className="absolute inset-y-0 w-0.5"
                      style={{
                        left: `${phase.pct + phase.inPct}%`,
                        backgroundColor: cfg.color,
                        boxShadow: `0 0 6px ${cfg.color}, 0 0 12px ${cfg.color}40`,
                      }}
                    />
                  )}

                  {/* Blocked overlay */}
                  {phase.status === "blocked" && (
                    <div className="absolute inset-0 flex items-center justify-center"
                      style={{ background: "repeating-linear-gradient(45deg, transparent, transparent 3px, rgba(239,68,68,0.08) 3px, rgba(239,68,68,0.08) 6px)" }}
                    >
                      <AlertTriangle size={10} className="text-[var(--critical-red)] drop-shadow-[0_0_4px_rgba(239,68,68,0.6)]" />
                    </div>
                  )}

                  {/* Completed checkmark overlay */}
                  {phase.pct === 100 && (
                    <div className="absolute inset-0 flex items-center justify-center">
                      <CheckCircle2 size={10} className="text-[var(--validation-emerald)] drop-shadow-[0_0_4px_rgba(16,185,129,0.6)]" />
                    </div>
                  )}
                </div>
              </div>

              {/* Stats */}
              <div className="w-6 shrink-0 text-right">
                <span
                  className="font-mono text-[8px] font-bold tabular-nums"
                  style={{
                    color: cfg.color,
                    textShadow: cfg.glow ? `0 0 6px ${cfg.color}60` : "none",
                  }}
                >
                  {phase.pct}%
                </span>
              </div>
            </div>

            {/* Milestone dots row (on hover) */}
            <div className="h-0 group-hover:h-3 overflow-hidden transition-all duration-200 ml-[calc(2rem+6px)]">
              <div className="flex gap-0.5 items-center h-3">
                {phase.milestones.map(ms => {
                  const msCfg = STATUS_CONFIG[ms.status] || STATUS_CONFIG.pending
                  const trackColor = isOBM ? TRACK_CONFIG[ms.track]?.color : msCfg.color
                  return (
                    <div
                      key={ms.id}
                      className="w-1.5 h-1.5 rounded-full transition-all"
                      style={{
                        backgroundColor: trackColor || msCfg.color,
                        opacity: ms.status === "pending" ? 0.3 : 0.9,
                        boxShadow: ms.status === "completed" ? `0 0 4px ${msCfg.color}` : "none",
                      }}
                      title={`${ms.title} (${ms.status})`}
                    />
                  )
                })}
              </div>
            </div>
          </div>
        )
      })}

      {/* Track legend */}
      {isOBM && (
        <div className="flex gap-3 mt-2 pt-1.5 border-t border-[var(--border)]">
          {(["engineering", "design", "market"] as const).map(track => {
            const cfg = TRACK_CONFIG[track]
            return (
              <span key={track} className="flex items-center gap-1 font-mono text-[7px]" style={{ color: cfg.color }}>
                <span
                  className="w-2 h-1 rounded-sm"
                  style={{
                    backgroundColor: cfg.color,
                    boxShadow: `0 0 4px ${cfg.color}40`,
                  }}
                />
                {cfg.label}
              </span>
            )
          })}
        </div>
      )}
    </div>
  )
}

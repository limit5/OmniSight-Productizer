"use client"

import { useState, useEffect } from "react"
import { ChevronDown, ChevronUp, Target, Zap, Shield, Package, Rocket, CheckCircle2, Clock, AlertTriangle, BarChart3, List } from "lucide-react"
import { NPIGantt } from "./npi-gantt"
import { PanelHelp } from "@/components/omnisight/panel-help"

// Types matching backend
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

interface NPIData {
  business_model: "odm" | "oem" | "jdm" | "obm"
  current_phase_id?: string
  phases: NPIPhase[]
}

interface NPITimelineProps {
  data?: NPIData | null
  onBusinessModelChange?: (model: string) => void
  onMilestoneStatusChange?: (milestoneId: string, status: string) => void
  onPhaseStatusChange?: (phaseId: string, status: string) => void
}

const TRACK_CONFIG = {
  engineering: { label: "ENG", color: "var(--hardware-orange)", icon: Zap },
  design: { label: "DSN", color: "var(--artifact-purple)", icon: Target },
  market: { label: "MKT", color: "var(--neural-blue)", icon: Rocket },
}

const PHASE_STATUS_CONFIG = {
  pending: { color: "var(--muted-foreground)", glow: false, pulse: false },
  active: { color: "var(--hardware-orange)", glow: true, pulse: true },
  completed: { color: "var(--validation-emerald)", glow: true, pulse: false },
  blocked: { color: "var(--critical-red)", glow: true, pulse: true },
}

const BIZ_MODELS = [
  { id: "odm", label: "ODM", desc: "委託設計", tracks: 1, color: "var(--hardware-orange)" },
  { id: "oem", label: "OEM", desc: "委託製造", tracks: 1, color: "var(--validation-emerald)" },
  { id: "jdm", label: "JDM", desc: "聯合設計", tracks: 1, color: "var(--artifact-purple)" },
  { id: "obm", label: "OBM", desc: "自有品牌", tracks: 3, color: "var(--neural-blue)" },
]

function getPhaseIcon(shortName: string) {
  const icons: Record<string, typeof Zap> = {
    PRD: Target, EIV: Shield, POC: Zap, HVT: Package,
    EVT: Zap, DVT: Shield, PVT: Package, MP: Rocket,
  }
  return icons[shortName] || Zap
}

export function NPITimeline({ data, onBusinessModelChange, onMilestoneStatusChange, onPhaseStatusChange }: NPITimelineProps) {
  const [expandedPhase, setExpandedPhase] = useState<string | null>(null)
  const [viewMode, setViewMode] = useState<"timeline" | "gantt">("timeline")

  if (!data || !data.phases.length) {
    return (
      <div className="h-full flex flex-col">
        <div className="px-3 py-3 holo-glass-simple mb-3 corner-brackets">
          <div className="flex items-center gap-2 relative z-10">
            <div className="w-2 h-2 rounded-full bg-[var(--neural-blue)] pulse-blue pulse-ring" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--neural-blue)]">NPI LIFECYCLE</h2>
            <PanelHelp doc="panels-overview" />
          </div>
        </div>
        <div className="flex-1 flex items-center justify-center">
          <p className="font-mono text-xs text-[var(--muted-foreground)] opacity-50">Awaiting NPI data...</p>
        </div>
      </div>
    )
  }

  const isOBM = data.business_model === "obm"
  const completedPhases = data.phases.filter(p => p.status === "completed").length
  const totalPhases = data.phases.length
  const progressPercent = totalPhases > 0 ? Math.round((completedPhases / totalPhases) * 100) : 0

  const visibleTracks = isOBM ? ["engineering", "design", "market"] : ["engineering"]

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-3 py-2 holo-glass-simple mb-2 corner-brackets data-stream">
        {/* Row 1: Title + percentage */}
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-1.5 min-w-0">
            <div className="w-2 h-2 rounded-full bg-[var(--neural-blue)] pulse-blue pulse-ring shrink-0" />
            <h2 className="font-sans text-xs font-semibold tracking-fui text-[var(--neural-blue)] truncate">NPI LIFECYCLE</h2>
            <PanelHelp doc="panels-overview" />
          </div>
          <span className="font-mono text-[10px] text-[var(--validation-emerald)] tabular-nums shrink-0">{progressPercent}%</span>
        </div>
        {/* Row 2: Progress bar + view toggle */}
        <div className="flex items-center gap-2 mt-1.5 relative z-20">
          <div className="h-1 flex-1 rounded-full bg-[var(--border)] overflow-hidden">
            <div
              className="h-full rounded-full bg-[var(--validation-emerald)] transition-all duration-500"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
          {/* View mode toggle */}
          <div className="flex rounded-sm overflow-hidden border border-[var(--border)] shrink-0">
            <button
              onClick={(e) => { e.stopPropagation(); setViewMode("timeline") }}
              className={`px-1 py-0.5 cursor-pointer transition-all duration-200 ${
                viewMode === "timeline"
                  ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                  : "text-[var(--muted-foreground)] hover:text-[var(--neural-blue)]"
              }`}
              title="Timeline View"
            >
              <List size={9} />
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); setViewMode("gantt") }}
              className={`px-1 py-0.5 cursor-pointer transition-all duration-200 border-l border-[var(--border)] ${
                viewMode === "gantt"
                  ? "bg-[var(--neural-blue)]/20 text-[var(--neural-blue)]"
                  : "text-[var(--muted-foreground)] hover:text-[var(--neural-blue)]"
              }`}
              title="Gantt View"
            >
              <BarChart3 size={9} />
            </button>
          </div>
        </div>
      </div>

      {/* Business Model Selector */}
      <div className="px-3 mb-2">
        <div className="flex gap-1">
          {BIZ_MODELS.map(bm => (
            <button
              key={bm.id}
              onClick={() => onBusinessModelChange?.(bm.id)}
              className={`relative z-20 flex-1 py-1 rounded font-mono text-[9px] transition-all cursor-pointer ${
                data.business_model === bm.id
                  ? "ring-1"
                  : "text-[var(--muted-foreground)] hover:text-[var(--foreground)] bg-[var(--secondary)]"
              }`}
              style={data.business_model === bm.id ? {
                backgroundColor: `color-mix(in srgb, ${bm.color} 20%, transparent)`,
                color: bm.color,
                ringColor: bm.color,
                boxShadow: `0 0 6px color-mix(in srgb, ${bm.color} 30%, transparent)`,
              } : undefined}
              title={bm.desc}
            >
              {bm.label}
            </button>
          ))}
        </div>
        {isOBM && (
          <div className="flex gap-1 mt-1.5">
            {(["engineering", "design", "market"] as const).map(t => {
              const cfg = TRACK_CONFIG[t]
              return (
                <span key={t} className="flex items-center gap-1 font-mono text-[8px]" style={{ color: cfg.color }}>
                  <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: cfg.color }} />
                  {cfg.label}
                </span>
              )
            })}
          </div>
        )}
      </div>

      {/* Gantt View */}
      {viewMode === "gantt" && (
        <div className="flex-1 overflow-y-auto px-3 pb-2">
          <NPIGantt phases={data.phases} isOBM={isOBM} />
        </div>
      )}

      {/* Timeline View */}
      {viewMode === "timeline" && (
      <div className="flex-1 overflow-y-auto px-3 pb-2">
        <div className="relative">
          {/* Vertical line */}
          <div className="absolute left-[11px] top-3 bottom-3 w-px bg-[var(--border)]" />

          {data.phases.map((phase, idx) => {
            const cfg = PHASE_STATUS_CONFIG[phase.status] || PHASE_STATUS_CONFIG.pending
            const PhaseIcon = getPhaseIcon(phase.short_name)
            const isExpanded = expandedPhase === phase.id
            const isCurrent = data.current_phase_id === phase.id
            const phaseMs = phase.milestones.filter(m => visibleTracks.includes(m.track))
            const completedMs = phaseMs.filter(m => m.status === "completed").length

            return (
              <div key={phase.id} className="relative mb-1">
                {/* Phase node */}
                <button
                  onClick={() => setExpandedPhase(isExpanded ? null : phase.id)}
                  className="relative z-20 w-full flex items-center gap-2 py-1.5 rounded hover:bg-[var(--secondary)]/50 transition-all cursor-pointer group"
                >
                  {/* Circle node */}
                  <div
                    className={`relative w-[22px] h-[22px] rounded-full flex items-center justify-center shrink-0 border-2 transition-all ${
                      cfg.pulse ? "animate-pulse" : ""
                    }`}
                    style={{
                      borderColor: cfg.color,
                      backgroundColor: phase.status === "completed"
                        ? `color-mix(in srgb, ${cfg.color} 30%, transparent)`
                        : "var(--background)",
                      boxShadow: cfg.glow ? `0 0 8px ${cfg.color}40` : "none",
                    }}
                  >
                    {phase.status === "completed" ? (
                      <CheckCircle2 size={10} style={{ color: cfg.color }} />
                    ) : (
                      <PhaseIcon size={9} style={{ color: cfg.color }} />
                    )}
                  </div>

                  {/* Phase info */}
                  <div className="flex-1 min-w-0 text-left">
                    <div className="flex items-center gap-1.5">
                      <span className="font-mono text-[10px] font-bold" style={{ color: cfg.color }}>
                        {phase.short_name}
                      </span>
                      {isCurrent && (
                        <span className="px-1 py-0.5 rounded text-[7px] font-mono bg-[var(--hardware-orange)]/20 text-[var(--hardware-orange)]">
                          CURRENT
                        </span>
                      )}
                      <span className="font-mono text-[9px] text-[var(--muted-foreground)] truncate">{phase.name}</span>
                    </div>
                    {/* Mini progress */}
                    <div className="flex items-center gap-1 mt-0.5">
                      <div className="h-1 flex-1 rounded-full bg-[var(--border)] overflow-hidden max-w-[60px]">
                        <div
                          className="h-full rounded-full transition-all"
                          style={{
                            width: phaseMs.length ? `${(completedMs / phaseMs.length) * 100}%` : "0%",
                            backgroundColor: cfg.color,
                          }}
                        />
                      </div>
                      <span className="font-mono text-[8px] text-[var(--muted-foreground)]">{completedMs}/{phaseMs.length}</span>
                    </div>
                  </div>

                  <div className="shrink-0">
                    {isExpanded ? <ChevronUp size={10} className="text-[var(--muted-foreground)]" /> : <ChevronDown size={10} className="text-[var(--muted-foreground)]" />}
                  </div>
                </button>

                {/* Expanded milestones */}
                {isExpanded && (
                  <div className="ml-7 mt-1 mb-2 space-y-0.5">
                    {visibleTracks.map(track => {
                      const trackMs = phaseMs.filter(m => m.track === track)
                      if (!trackMs.length) return null
                      const tCfg = TRACK_CONFIG[track as keyof typeof TRACK_CONFIG]
                      return (
                        <div key={track}>
                          {isOBM && (
                            <div className="flex items-center gap-1 mb-0.5 mt-1">
                              <span className="w-1 h-1 rounded-full" style={{ backgroundColor: tCfg.color }} />
                              <span className="font-mono text-[7px] uppercase" style={{ color: tCfg.color }}>{tCfg.label}</span>
                            </div>
                          )}
                          {trackMs.map(ms => {
                            const msColor = ms.status === "completed" ? "var(--validation-emerald)"
                              : ms.status === "in_progress" ? "var(--hardware-orange)"
                              : ms.status === "blocked" ? "var(--critical-red)"
                              : "var(--muted-foreground)"
                            return (
                              <button
                                key={ms.id}
                                onClick={(e) => {
                                  e.stopPropagation()
                                  const next = ms.status === "pending" ? "in_progress" : ms.status === "in_progress" ? "completed" : ms.status === "completed" ? "pending" : "pending"
                                  onMilestoneStatusChange?.(ms.id, next)
                                }}
                                className="relative z-20 w-full flex items-center gap-1.5 px-2 py-1 rounded text-left hover:bg-[var(--secondary)]/50 transition-all cursor-pointer group"
                              >
                                <div
                                  className={`w-3 h-3 rounded-sm border shrink-0 flex items-center justify-center ${
                                    ms.status === "completed" ? "border-[var(--validation-emerald)] bg-[var(--validation-emerald)]/20" : "border-[var(--border)]"
                                  }`}
                                >
                                  {ms.status === "completed" && <CheckCircle2 size={8} className="text-[var(--validation-emerald)]" />}
                                  {ms.status === "in_progress" && <Clock size={7} className="text-[var(--hardware-orange)]" />}
                                  {ms.status === "blocked" && <AlertTriangle size={7} className="text-[var(--critical-red)]" />}
                                </div>
                                <span className="font-mono text-[9px] flex-1 min-w-0 truncate" style={{ color: msColor }}>
                                  {ms.jira_tag && <span className="opacity-60">{ms.jira_tag} </span>}
                                  {ms.title}
                                </span>
                              </button>
                            )
                          })}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
      )}
    </div>
  )
}

"use client"

import { useState, useEffect } from "react"
import { ArrowRight, FileText, ChevronDown, ChevronUp } from "lucide-react"

interface HandoffItem {
  task_id: string
  agent_id: string
  created_at: string
}

interface HandoffTimelineProps {
  handoffs?: HandoffItem[]
  onLoadHandoffs?: () => void
}

const AGENT_COLORS: Record<string, string> = {
  firmware: "var(--hardware-orange)",
  software: "var(--neural-blue)",
  validator: "var(--validation-emerald)",
  reporter: "var(--artifact-purple)",
  reviewer: "var(--critical-red)",
  general: "var(--muted-foreground)",
}

function getAgentColor(agentId: string): string {
  for (const [type, color] of Object.entries(AGENT_COLORS)) {
    if (agentId.toLowerCase().includes(type)) return color
  }
  return "var(--muted-foreground)"
}

export function HandoffTimeline({ handoffs = [], onLoadHandoffs }: HandoffTimelineProps) {
  const [collapsed, setCollapsed] = useState(true)

  useEffect(() => {
    if (!collapsed && handoffs.length === 0 && onLoadHandoffs) {
      onLoadHandoffs()
    }
  }, [collapsed, handoffs.length, onLoadHandoffs])

  return (
    <div className="border-b border-[var(--border)]">
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-[var(--secondary)] transition-colors"
      >
        <FileText size={10} className="text-[var(--artifact-purple)]" />
        <span className="font-mono text-[9px] font-semibold tracking-fui text-[var(--artifact-purple)] flex-1 text-left">
          HANDOFF CHAIN
        </span>
        <span className="font-mono text-[9px] text-[var(--muted-foreground)]">{handoffs.length}</span>
        {collapsed ? <ChevronDown size={10} className="text-[var(--muted-foreground)]" /> : <ChevronUp size={10} className="text-[var(--muted-foreground)]" />}
      </button>

      {!collapsed && (
        <div className="px-3 pb-2">
          {handoffs.length === 0 ? (
            <p className="font-mono text-[9px] text-[var(--muted-foreground)] text-center py-2 opacity-60">
              No handoffs recorded yet
            </p>
          ) : (
            <div className="space-y-1">
              {handoffs.map((h, idx) => {
                const color = getAgentColor(h.agent_id)
                const next = handoffs[idx + 1]
                return (
                  <div key={`${h.task_id}-${h.agent_id}-${idx}`}>
                    <div className="flex items-center gap-1.5 py-1 rounded px-1.5 bg-[var(--secondary)]">
                      {/* Agent dot */}
                      <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: color }} />
                      {/* Agent ID */}
                      <span className="font-mono text-[9px] flex-1 min-w-0 truncate" style={{ color }}>
                        {h.agent_id}
                      </span>
                      {/* Timestamp */}
                      <span className="font-mono text-[8px] text-[var(--muted-foreground)] shrink-0">
                        {h.created_at.includes("T") ? h.created_at.split("T")[1]?.slice(0, 8) : h.created_at}
                      </span>
                    </div>
                    {/* Arrow to next agent */}
                    {next && (
                      <div className="flex items-center justify-center py-0.5">
                        <ArrowRight size={10} className="text-[var(--muted-foreground)] opacity-40" />
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

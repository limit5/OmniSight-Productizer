"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Network, ShieldCheck, Users } from "lucide-react"

import type { Agent } from "@/components/omnisight/agent-matrix-wall"
import { listAgentCards, type AgentCardSummary } from "@/lib/api"

const GUILD_LABELS: Record<string, string> = {
  architect: "Architect",
  sa_sd: "SA-SD",
  ux: "UX",
  pm: "PM",
  gateway: "Gateway",
  bsp: "BSP",
  hal: "HAL",
  algo_cv: "Algo-CV",
  optical: "Optical",
  isp: "ISP",
  audio: "Audio",
  frontend: "Frontend",
  backend: "Backend",
  sre: "SRE",
  qa: "QA",
  auditor: "Auditor",
  red_team: "RedTeam",
  forensics: "Forensics",
  intel: "Intel",
  reporter: "Reporter",
  custom: "Custom",
  generalist: "Generalist",
}

const TYPE_GUILD_FALLBACK: Record<Agent["type"], string> = {
  firmware: "bsp",
  software: "backend",
  reporter: "reporter",
  validator: "qa",
  reviewer: "auditor",
  custom: "custom",
}

const ACTIVE_STATUSES = new Set(["running", "booting", "awaiting_confirmation", "materializing"])

export interface OperationsConsoleProps {
  agents: readonly Agent[]
  activePanel: string
}

interface GuildRow {
  guild: string
  label: string
  total: number
  active: number
}

export function OperationsConsole({ agents, activePanel }: OperationsConsoleProps) {
  const [cards, setCards] = useState<AgentCardSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const refreshCards = useCallback(async () => {
    try {
      const next = await listAgentCards({ sort_by: "activity" })
      if (!mountedRef.current) return
      setCards(next)
      setError(null)
    } catch (exc) {
      if (!mountedRef.current) return
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount populates Guild card metadata from the API
    void refreshCards()
    return () => {
      mountedRef.current = false
    }
  }, [refreshCards])

  const rows = useMemo(() => buildGuildRows(agents, cards), [agents, cards])
  const activeGuilds = rows.filter((row) => row.active > 0).length
  const coverage = agents.length === 0
    ? 100
    : Math.round((agents.filter((agent) => resolveGuild(agent, cards)).length / agents.length) * 100)
  const coverageTone = coverage >= 100
    ? "text-[var(--validation-emerald,#10b981)]"
    : coverage >= 75
      ? "text-[var(--fui-orange,#f59e0b)]"
      : "text-[var(--critical-red,#ef4444)]"

  return (
    <section
      aria-label="Operations Console"
      className="border-b border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background)]/70 px-3 py-2 backdrop-blur"
      data-testid="operations-console"
    >
      <div className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--neural-cyan,#67e8f9)]">
            <Network className="h-3.5 w-3.5" aria-hidden />
            Guild Topology
          </span>
          <span className="rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] px-1.5 py-0.5 font-mono text-[10px] uppercase text-[var(--muted-foreground,#94a3b8)]">
            panel {activePanel}
          </span>
          <span
            className={`inline-flex items-center gap-1 rounded-sm border border-current/40 px-1.5 py-0.5 font-mono text-[10px] uppercase ${coverageTone}`}
            data-testid="operations-console-compliance-badge"
            title="Percent of loaded agents with an explicit or derived Guild"
          >
            <ShieldCheck className="h-3 w-3" aria-hidden />
            Guild coverage {coverage}%
          </span>
          {error && (
            <span
              className="max-w-[280px] truncate font-mono text-[10px] text-[var(--fui-orange,#f59e0b)]"
              title={error}
            >
              cards unavailable; using agent type fallback
            </span>
          )}
        </div>

        <div className="flex min-w-0 items-center gap-2 overflow-x-auto pb-1 xl:pb-0" data-testid="operations-console-guilds">
          <span className="inline-flex shrink-0 items-center gap-1 font-mono text-[10px] text-[var(--muted-foreground,#94a3b8)]">
            <Users className="h-3 w-3" aria-hidden />
            {activeGuilds}/{rows.length} active
          </span>
          {rows.map((row) => (
            <span
              key={row.guild}
              className="inline-flex shrink-0 items-center gap-1 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--secondary)]/35 px-1.5 py-0.5 font-mono text-[10px] text-[var(--foreground)]"
              data-guild={row.guild}
              data-testid="operations-console-guild"
              title={`${row.label}: ${row.active} active / ${row.total} total`}
            >
              <span className={row.active > 0 ? "text-[var(--validation-emerald,#10b981)]" : "text-[var(--muted-foreground,#94a3b8)]"}>
                {row.label}
              </span>
              <span className="tabular-nums text-[var(--muted-foreground,#94a3b8)]">
                {row.active}/{row.total}
              </span>
            </span>
          ))}
        </div>
      </div>
    </section>
  )
}

function buildGuildRows(agents: readonly Agent[], cards: readonly AgentCardSummary[]): GuildRow[] {
  const counts = new Map<string, { total: number; active: number }>()
  for (const agent of agents) {
    const guild = resolveGuild(agent, cards) || TYPE_GUILD_FALLBACK[agent.type] || "custom"
    const current = counts.get(guild) ?? { total: 0, active: 0 }
    current.total += 1
    if (ACTIVE_STATUSES.has(agent.status)) current.active += 1
    counts.set(guild, current)
  }
  for (const card of cards) {
    if (agents.some((agent) => agent.id === card.agent_id)) continue
    const current = counts.get(card.guild) ?? { total: 0, active: 0 }
    current.total += 1
    counts.set(card.guild, current)
  }
  return Array.from(counts.entries())
    .map(([guild, count]) => ({
      guild,
      label: GUILD_LABELS[guild] ?? guild.replace(/_/g, " "),
      total: count.total,
      active: count.active,
    }))
    .sort((a, b) => b.active - a.active || b.total - a.total || a.label.localeCompare(b.label))
}

function resolveGuild(agent: Agent, cards: readonly AgentCardSummary[]): string | null {
  if (agent.guild?.trim()) return agent.guild.trim()
  const card = cards.find((entry) => entry.agent_id === agent.id)
  if (card?.guild?.trim()) return card.guild.trim()
  return null
}

export default OperationsConsole

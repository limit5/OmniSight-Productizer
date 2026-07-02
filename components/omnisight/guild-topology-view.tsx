"use client"

/**
 * BP.K.7 - Guild topology view.
 *
 * Pure presentation layer for the Guild Hall / Party Hall data that already
 * exists under `components/omnisight/agents`. The caller owns fetching and
 * mutation; this component lays out Guild nodes, cross-Guild synergy edges,
 * and active party summaries in one operator-facing topology.
 */

import {
  BrainCircuit,
  Code2,
  Database,
  GitBranch,
  Laptop,
  ServerCog,
  Shield,
  Smartphone,
  Sparkles,
  Users,
  Wrench,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import type { ReactElement } from "react"

import type { AgentGuild } from "@/components/omnisight/agents/CharacterCard"
import type { PartySynergy } from "@/components/omnisight/agents/PartyBadge"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export interface GuildTopologyMember {
  agentId?: string
  agent_id?: string
  agentClass?: string
  agent_class?: string
  instanceSuffix?: string
  instance_suffix?: string
  level?: number
  xp?: number
  specializationLabel?: string
  specialization_label?: string
  spec?: string
}

export interface GuildTopologyGuild {
  guild: AgentGuild
  displayName?: string | null
  display_name?: string | null
  summary?: string | null
  memberCount?: number
  member_count?: number
  members?: readonly GuildTopologyMember[]
}

export interface GuildTopologyParty {
  partyId?: string
  party_id?: string
  name: string
  members: readonly { memberAgentId?: string; member_agent_id?: string }[]
  activeTaskId?: string | null
  active_task_id?: string | null
  synergy?: PartySynergy | null
}

export interface GuildTopologyViewProps {
  guilds: readonly GuildTopologyGuild[]
  synergies?: readonly PartySynergy[]
  parties?: readonly GuildTopologyParty[]
  className?: string
}

interface GuildVisual {
  label: string
  description: string
  Icon: LucideIcon
  nodeClass: string
  iconClass: string
}

export interface TopologyGuildNode {
  guild: AgentGuild
  label: string
  summary: string
  memberCount: number
  members: readonly GuildTopologyMember[]
  x: number
  y: number
  visual: GuildVisual
}

export interface TopologyEdge {
  id: string
  from: TopologyGuildNode
  to: TopologyGuildNode
  label: string
  bonus: string
}

const GUILD_ORDER: AgentGuild[] = [
  "backend",
  "frontend",
  "security",
  "devops",
  "data",
  "mobile",
  "embedded",
  "isp",
  "generalist",
]

const GUILD_VISUALS: Record<AgentGuild, GuildVisual> = {
  backend: {
    label: "Backend",
    description: "Services, APIs, and persistence",
    Icon: ServerCog,
    nodeClass: "border-sky-500/40 bg-sky-500/10",
    iconClass: "text-sky-700 dark:text-sky-300",
  },
  frontend: {
    label: "Frontend",
    description: "Interface and client experience",
    Icon: Code2,
    nodeClass: "border-fuchsia-500/40 bg-fuchsia-500/10",
    iconClass: "text-fuchsia-700 dark:text-fuchsia-300",
  },
  security: {
    label: "Security",
    description: "Auth, audit, and boundary protection",
    Icon: Shield,
    nodeClass: "border-rose-500/40 bg-rose-500/10",
    iconClass: "text-rose-700 dark:text-rose-300",
  },
  devops: {
    label: "DevOps",
    description: "CI, release, and infrastructure",
    Icon: Wrench,
    nodeClass: "border-orange-500/40 bg-orange-500/10",
    iconClass: "text-orange-700 dark:text-orange-300",
  },
  data: {
    label: "Data",
    description: "Models, analytics, and quality",
    Icon: Database,
    nodeClass: "border-emerald-500/40 bg-emerald-500/10",
    iconClass: "text-emerald-700 dark:text-emerald-300",
  },
  mobile: {
    label: "Mobile",
    description: "Device, store, and mobile UX",
    Icon: Smartphone,
    nodeClass: "border-cyan-500/40 bg-cyan-500/10",
    iconClass: "text-cyan-700 dark:text-cyan-300",
  },
  embedded: {
    label: "Embedded",
    description: "Firmware, SoC, and hardware edges",
    Icon: Laptop,
    nodeClass: "border-lime-500/40 bg-lime-500/10",
    iconClass: "text-lime-700 dark:text-lime-300",
  },
  isp: {
    label: "ISP",
    description: "Camera and image signal pipelines",
    Icon: Laptop,
    nodeClass: "border-teal-500/40 bg-teal-500/10",
    iconClass: "text-teal-700 dark:text-teal-300",
  },
  generalist: {
    label: "Generalist",
    description: "Cross-domain product work",
    Icon: BrainCircuit,
    nodeClass: "border-violet-500/40 bg-violet-500/10",
    iconClass: "text-violet-700 dark:text-violet-300",
  },
}

const VIEWBOX_WIDTH = 720
const VIEWBOX_HEIGHT = 420
const CENTER_X = VIEWBOX_WIDTH / 2
const CENTER_Y = 184
const NODE_W = 132
const NODE_H = 78
const NODE_RADIUS_X = 270
const NODE_RADIUS_Y = 128

function normalizeCount(value: number | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 0
  return Math.max(0, Math.trunc(value))
}

function displayNameFor(guild: GuildTopologyGuild | undefined, fallback: string): string {
  return guild?.displayName?.trim() || guild?.display_name?.trim() || fallback
}

function summaryFor(guild: GuildTopologyGuild | undefined, fallback: string): string {
  return guild?.summary?.trim() || fallback
}

function memberCountFor(guild: GuildTopologyGuild | undefined): number {
  if (!guild) return 0
  return normalizeCount(guild.memberCount ?? guild.member_count ?? guild.members?.length)
}

function memberId(member: GuildTopologyMember): string {
  return member.agentId?.trim() || member.agent_id?.trim() || "agent"
}

function partyId(party: GuildTopologyParty): string {
  return party.partyId?.trim() || party.party_id?.trim() || party.name
}

function activeTaskId(party: GuildTopologyParty): string | null {
  return party.activeTaskId?.trim() || party.active_task_id?.trim() || null
}

function formatCount(count: number): string {
  return count.toLocaleString()
}

function formatPct(value: number | null): string {
  if (value === null) return ""
  const pct = Math.round(value * 100)
  if (!Number.isFinite(pct) || pct <= 0) return ""
  return `+${pct}%`
}

function synergyBonus(synergy: PartySynergy): string {
  const parts: string[] = []
  const xp = formatPct(synergy.xpBonus)
  if (xp) parts.push(`${xp} XP`)
  if (synergy.skillBonusTarget && synergy.skillBonus !== null) {
    const skill = formatPct(synergy.skillBonus)
    if (skill) parts.push(`${skill} ${synergy.skillBonusTarget}`)
  }
  return parts.length > 0 ? parts.join(" / ") : "No XP bonus"
}

export function buildGuildTopologyNodes(
  guilds: readonly GuildTopologyGuild[],
): readonly TopologyGuildNode[] {
  const guildsByKey = new Map(guilds.map((item) => [item.guild, item]))

  return GUILD_ORDER.map((guild, index) => {
    const visual = GUILD_VISUALS[guild]
    const payload = guildsByKey.get(guild)
    const angle = (index / GUILD_ORDER.length) * Math.PI * 2 - Math.PI / 2

    return {
      guild,
      label: displayNameFor(payload, visual.label),
      summary: summaryFor(payload, visual.description),
      memberCount: memberCountFor(payload),
      members: payload?.members ?? [],
      x: CENTER_X + Math.cos(angle) * NODE_RADIUS_X,
      y: CENTER_Y + Math.sin(angle) * NODE_RADIUS_Y,
      visual,
    }
  })
}

export function buildGuildTopologyEdges(
  nodes: readonly TopologyGuildNode[],
  synergies: readonly PartySynergy[],
): readonly TopologyEdge[] {
  const nodeByGuild = new Map(nodes.map((node) => [node.guild, node]))

  return synergies.flatMap((synergy) => {
    const [firstGuild, secondGuild] = synergy.guilds
    const from = firstGuild ? nodeByGuild.get(firstGuild) : undefined
    const to = secondGuild ? nodeByGuild.get(secondGuild) : undefined
    if (!from || !to) return []
    return [{
      id: synergy.label,
      from,
      to,
      label: synergy.displayName,
      bonus: synergyBonus(synergy),
    }]
  })
}

export function GuildTopologyView({
  guilds,
  synergies = [],
  parties = [],
  className,
}: GuildTopologyViewProps): ReactElement {
  const nodes = buildGuildTopologyNodes(guilds)
  const edges = buildGuildTopologyEdges(nodes, synergies)
  const totalMembers = nodes.reduce((total, node) => total + node.memberCount, 0)
  const activeParties = parties.filter((party) => activeTaskId(party) !== null)

  return (
    <section
      aria-label="Guild topology"
      className={cn("space-y-4", className)}
      data-testid="guild-topology-view"
    >
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold leading-tight">Guild topology</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Cross-Guild membership, synergy paths, and active party load
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge variant="outline" className="h-7 gap-1.5 px-2 text-xs">
            <Users className="size-3.5" aria-hidden="true" />
            {formatCount(totalMembers)} members
          </Badge>
          <Badge variant="outline" className="h-7 gap-1.5 px-2 text-xs">
            <Sparkles className="size-3.5" aria-hidden="true" />
            {formatCount(edges.length)} synergies
          </Badge>
          <Badge variant="outline" className="h-7 gap-1.5 px-2 text-xs">
            <GitBranch className="size-3.5" aria-hidden="true" />
            {formatCount(parties.length)} parties
          </Badge>
        </div>
      </header>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_280px]">
        <div className="min-w-0 overflow-hidden rounded-lg border bg-background">
          <svg
            viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
            role="img"
            aria-label={`Guild topology with ${nodes.length} Guilds and ${edges.length} synergies`}
            className="block aspect-[12/7] h-auto w-full"
            data-testid="guild-topology-canvas"
          >
            <defs>
              <marker
                id="guild-topology-arrow"
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="6"
                markerHeight="6"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
              </marker>
            </defs>

            <circle
              cx={CENTER_X}
              cy={CENTER_Y}
              r="66"
              className="fill-muted/30 stroke-border"
              strokeDasharray="6 6"
            />
            <text
              x={CENTER_X}
              y={CENTER_Y - 8}
              textAnchor="middle"
              className="fill-foreground text-[13px] font-semibold"
            >
              Guild mesh
            </text>
            <text
              x={CENTER_X}
              y={CENTER_Y + 13}
              textAnchor="middle"
              className="fill-muted-foreground text-[11px]"
            >
              {formatCount(totalMembers)} members
            </text>

            {edges.map((edge) => (
              <g key={edge.id} data-testid="guild-topology-edge" data-synergy-label={edge.id}>
                <path
                  d={`M ${edge.from.x} ${edge.from.y} Q ${CENTER_X} ${CENTER_Y} ${edge.to.x} ${edge.to.y}`}
                  className="fill-none stroke-violet-500/50"
                  strokeWidth="2"
                  markerEnd="url(#guild-topology-arrow)"
                />
                <title>{`${edge.label}: ${edge.bonus}`}</title>
              </g>
            ))}

            {nodes.map((node) => (
              <foreignObject
                key={node.guild}
                x={node.x - NODE_W / 2}
                y={node.y - NODE_H / 2}
                width={NODE_W}
                height={NODE_H}
                data-testid="guild-topology-node"
                data-guild={node.guild}
                data-member-count={node.memberCount}
              >
                <div
                  className={cn(
                    "flex h-full min-w-0 flex-col rounded-md border p-2 shadow-sm backdrop-blur",
                    node.visual.nodeClass,
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <node.visual.Icon
                        className={cn("size-4 shrink-0", node.visual.iconClass)}
                        aria-hidden="true"
                      />
                      <p className="truncate text-xs font-semibold leading-none">
                        {node.label}
                      </p>
                    </div>
                    <span className="font-mono text-xs tabular-nums">
                      {formatCount(node.memberCount)}
                    </span>
                  </div>
                  <p className="mt-2 line-clamp-2 text-[10px] leading-snug text-muted-foreground">
                    {node.summary}
                  </p>
                  <div className="mt-auto flex gap-1 overflow-hidden pt-2">
                    {node.members.slice(0, 3).map((member) => (
                      <span
                        key={memberId(member)}
                        className="max-w-[34px] truncate rounded-sm border bg-background/70 px-1 font-mono text-[9px]"
                        title={memberId(member)}
                      >
                        {memberId(member).slice(0, 4)}
                      </span>
                    ))}
                    {node.members.length > 3 ? (
                      <span className="rounded-sm border bg-background/70 px-1 font-mono text-[9px]">
                        +{node.members.length - 3}
                      </span>
                    ) : null}
                  </div>
                </div>
              </foreignObject>
            ))}
          </svg>
        </div>

        <aside className="space-y-3" aria-label="Guild topology summary">
          <section className="rounded-lg border bg-muted/20 p-3" data-testid="guild-topology-synergies">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-sm font-semibold leading-tight">Synergy paths</h3>
              <Badge variant="secondary" className="text-[10px]">
                {edges.length}
              </Badge>
            </div>
            <div className="mt-3 space-y-2">
              {edges.length === 0 ? (
                <p className="text-xs text-muted-foreground">No cross-Guild synergies loaded.</p>
              ) : (
                edges.map((edge) => (
                  <div key={edge.id} className="rounded-md border bg-background/70 p-2">
                    <div className="flex items-center justify-between gap-2">
                      <p className="truncate text-xs font-medium">{edge.label}</p>
                      <span className="font-mono text-[10px] text-muted-foreground">
                        {edge.from.guild}/{edge.to.guild}
                      </span>
                    </div>
                    <p className="mt-1 font-mono text-[11px] text-violet-700 dark:text-violet-300">
                      {edge.bonus}
                    </p>
                  </div>
                ))
              )}
            </div>
          </section>

          <section className="rounded-lg border bg-muted/20 p-3" data-testid="guild-topology-parties">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-sm font-semibold leading-tight">Active parties</h3>
              <Badge variant="secondary" className="text-[10px]">
                {activeParties.length}/{parties.length}
              </Badge>
            </div>
            <div className="mt-3 space-y-2">
              {parties.length === 0 ? (
                <p className="text-xs text-muted-foreground">No active parties formed.</p>
              ) : (
                parties.slice(0, 4).map((party) => {
                  const taskId = activeTaskId(party)
                  return (
                    <div
                      key={partyId(party)}
                      className="rounded-md border bg-background/70 p-2"
                      data-testid="guild-topology-party"
                      data-party-id={partyId(party)}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <p className="truncate text-xs font-medium">{party.name}</p>
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {party.members.length} members
                        </span>
                      </div>
                      <p className="mt-1 truncate text-[11px] text-muted-foreground">
                        {taskId ? `Task ${taskId}` : "Idle"}
                        {party.synergy ? ` / ${party.synergy.displayName}` : ""}
                      </p>
                    </div>
                  )
                })
              )}
              {parties.length > 4 ? (
                <p className="text-[11px] text-muted-foreground">
                  +{parties.length - 4} more parties
                </p>
              ) : null}
            </div>
          </section>
        </aside>
      </div>
    </section>
  )
}

export default GuildTopologyView

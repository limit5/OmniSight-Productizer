"use client"

/**
 * OP-1460 (Sprint G.C) — Agent roster route.
 *
 * Mounts the RPG-style operator surface that previously had no Next.js
 * route: GuildHall as the landing view (membership across the canonical
 * Guild roster), a drill-down grid of every agent linking to
 * `/agents/[agent_id]` for the Character Card detail, and the PartyHall
 * for the active multi-agent party feed.
 *
 * Data sources:
 *   - GET /api/v1/agents/cards    → `listAgentCards`
 *   - GET /api/v1/agents/parties  → `listAgentParties`
 *
 * Both endpoints are owned by `backend/routers/agents.py`. The page
 * stays presentational on top of the existing components in
 * `components/omnisight/agents/`.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { ArrowLeft, ChevronRight, Loader2, RefreshCw, Users } from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import {
  listAgentCards,
  listAgentParties,
  type AgentCardSummary,
  type AgentPartyDto,
} from "@/lib/api"
import type { AgentGuild } from "@/components/omnisight/agents/CharacterCard"
import {
  GuildHall,
  type GuildHallGuild,
} from "@/components/omnisight/agents/GuildHall"
import {
  PartyHall,
  type Party,
} from "@/components/omnisight/agents/PartyHall"
import type { PartySynergy } from "@/components/omnisight/agents/PartyBadge"

const KNOWN_GUILDS: readonly AgentGuild[] = [
  "backend",
  "frontend",
  "security",
  "devops",
  "data",
  "mobile",
  "embedded",
  "generalist",
]

const GUILD_LABEL: Record<AgentGuild, string> = {
  backend: "Backend",
  frontend: "Frontend",
  security: "Security",
  devops: "DevOps",
  data: "Data",
  mobile: "Mobile",
  embedded: "Embedded",
  generalist: "Generalist",
}

function isKnownGuild(value: string): value is AgentGuild {
  return (KNOWN_GUILDS as readonly string[]).includes(value)
}

function normaliseGuild(value: string | null | undefined): AgentGuild {
  if (value && isKnownGuild(value)) return value
  return "generalist"
}

function aggregateGuilds(cards: AgentCardSummary[]): GuildHallGuild[] {
  const counts = new Map<AgentGuild, number>()
  for (const card of cards) {
    const guild = normaliseGuild(card.guild)
    counts.set(guild, (counts.get(guild) ?? 0) + 1)
  }
  return KNOWN_GUILDS.map((guild) => ({
    guild,
    memberCount: counts.get(guild) ?? 0,
  }))
}

function groupCardsByGuild(
  cards: AgentCardSummary[],
): Array<{ guild: AgentGuild; cards: AgentCardSummary[] }> {
  const buckets = new Map<AgentGuild, AgentCardSummary[]>()
  for (const card of cards) {
    const guild = normaliseGuild(card.guild)
    const bucket = buckets.get(guild) ?? []
    bucket.push(card)
    buckets.set(guild, bucket)
  }
  return KNOWN_GUILDS.filter((guild) => (buckets.get(guild)?.length ?? 0) > 0).map(
    (guild) => ({
      guild,
      cards: (buckets.get(guild) ?? []).slice().sort(
        (a, b) => b.level - a.level || a.agent_id.localeCompare(b.agent_id),
      ),
    }),
  )
}

function cardDisplayName(card: AgentCardSummary): string {
  const base = card.agent_class || card.agent_id
  return card.instance_suffix ? `${base} ${card.instance_suffix}` : base
}

function mapPartySynergy(
  party: AgentPartyDto,
): PartySynergy | null {
  if (!party.synergy) return null
  const guilds = party.synergy.guilds.map((g) => normaliseGuild(g))
  return {
    label: party.synergy.label,
    displayName: party.synergy.display_name,
    guilds,
    xpBonus: party.synergy.xp_bonus,
    skillBonusTarget: party.synergy.skill_bonus_target,
    skillBonus: party.synergy.skill_bonus,
    summary: party.synergy.summary,
  }
}

function mapParty(party: AgentPartyDto, lookup: Map<string, AgentCardSummary>): Party {
  return {
    partyId: party.party_id,
    name: party.name,
    activeTaskId: party.active_task_id,
    activeTaskTitle: null,
    synergy: mapPartySynergy(party),
    members: party.members
      .filter((m) => m.released_at === null)
      .map((member) => {
        const card = lookup.get(member.member_agent_id)
        return {
          agentId: member.member_agent_id,
          displayName: card ? cardDisplayName(card) : null,
          guild: card ? normaliseGuild(card.guild) : null,
          portraitUrl: null,
        }
      }),
  }
}

export default function AgentsRosterPage() {
  const auth = useAuth()
  const router = useRouter()
  const [cards, setCards] = useState<AgentCardSummary[]>([])
  const [parties, setParties] = useState<AgentPartyDto[]>([])
  const [loading, setLoading] = useState(true)
  const [cardsError, setCardsError] = useState<string | null>(null)
  const [partiesError, setPartiesError] = useState<string | null>(null)

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") {
      const next =
        typeof window !== "undefined"
          ? window.location.pathname + window.location.search
          : "/agents"
      router.replace(`/login?next=${encodeURIComponent(next)}`)
    }
  }, [auth.loading, auth.user, auth.authMode, router])

  const refresh = useCallback(async () => {
    setLoading(true)
    setCardsError(null)
    setPartiesError(null)
    const [cardsRes, partiesRes] = await Promise.allSettled([
      listAgentCards(),
      listAgentParties(),
    ])
    if (cardsRes.status === "fulfilled") {
      setCards(cardsRes.value)
    } else {
      const msg =
        cardsRes.reason instanceof Error
          ? cardsRes.reason.message
          : String(cardsRes.reason)
      setCardsError(msg)
      setCards([])
    }
    if (partiesRes.status === "fulfilled") {
      setParties(partiesRes.value)
    } else {
      const msg =
        partiesRes.reason instanceof Error
          ? partiesRes.reason.message
          : String(partiesRes.reason)
      setPartiesError(msg)
      setParties([])
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") return
    const timer = window.setTimeout(() => {
      void refresh()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [auth.loading, auth.user, auth.authMode, refresh])

  const guildSummaries = useMemo(() => aggregateGuilds(cards), [cards])
  const grouped = useMemo(() => groupCardsByGuild(cards), [cards])
  const cardLookup = useMemo(() => {
    const map = new Map<string, AgentCardSummary>()
    for (const card of cards) map.set(card.agent_id, card)
    return map
  }, [cards])
  const partyViewModels = useMemo(
    () => parties.map((p) => mapParty(p, cardLookup)),
    [parties, cardLookup],
  )

  if (auth.loading || (!auth.user && auth.authMode !== "open")) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)] flex items-center gap-2">
          <Loader2 size={14} className="animate-spin" />
          Verifying operator session...
        </div>
      </main>
    )
  }

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="agents-roster-page"
    >
      <div className="max-w-6xl mx-auto">
        <header className="flex items-start justify-between gap-4 mb-6 flex-wrap">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link
                href="/"
                className="hover:text-[var(--foreground)] inline-flex items-center gap-1"
              >
                <ArrowLeft size={10} /> dashboard
              </Link>
              <ChevronRight size={10} />
              <span className="text-[var(--foreground)]">agents</span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <Users size={20} />
              Agent Roster
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Operator-facing RPG roster: Guild membership, agent
              Character Cards, and active multi-agent parties.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="agents-roster-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        {cardsError && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
            data-testid="agents-roster-cards-error"
          >
            Failed to load agent cards: {cardsError}
          </div>
        )}

        <section className="mb-8" data-testid="agents-roster-guild-hall">
          <GuildHall guilds={guildSummaries} />
        </section>

        <section className="mb-8" data-testid="agents-roster-grid">
          <div className="mb-3">
            <h2 className="text-lg font-semibold leading-tight">Agent roster</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Click an agent to open the Character Card detail view.
            </p>
          </div>

          {loading && cards.length === 0 ? (
            <div className="rounded-md border border-dashed bg-muted/20 p-6 text-center text-xs font-mono text-muted-foreground">
              <Loader2 size={14} className="animate-spin inline-block mr-2" />
              Loading agent cards...
            </div>
          ) : grouped.length === 0 ? (
            <div className="rounded-md border border-dashed bg-muted/20 p-6 text-center text-xs font-mono text-muted-foreground">
              No agents registered yet.
            </div>
          ) : (
            <div className="space-y-5">
              {grouped.map(({ guild, cards: bucket }) => (
                <div
                  key={guild}
                  data-testid={`agents-roster-guild-${guild}`}
                  data-agent-guild={guild}
                >
                  <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {GUILD_LABEL[guild]} · {bucket.length}
                  </h3>
                  <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                    {bucket.map((card) => (
                      <li key={card.agent_id}>
                        <Link
                          href={`/agents/${encodeURIComponent(card.agent_id)}`}
                          data-testid="agents-roster-card-link"
                          data-agent-id={card.agent_id}
                          className="flex items-center justify-between gap-3 rounded-md border bg-card px-3 py-2 hover:border-primary/60 hover:bg-accent transition-colors"
                        >
                          <span className="min-w-0">
                            <span className="block truncate text-sm font-medium">
                              {cardDisplayName(card)}
                            </span>
                            <span className="block truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                              {card.specialization_label || card.agent_id}
                            </span>
                          </span>
                          <span className="shrink-0 rounded-sm bg-muted px-2 py-0.5 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                            Lv {card.level}
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </section>

        <section data-testid="agents-roster-party-hall">
          {partiesError ? (
            <div
              className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
              data-testid="agents-roster-parties-error"
            >
              Failed to load parties: {partiesError}
            </div>
          ) : (
            <PartyHall parties={partyViewModels} />
          )}
        </section>
      </div>
    </main>
  )
}

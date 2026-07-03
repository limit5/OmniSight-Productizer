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
import {
  ArrowLeft,
  ChevronRight,
  Loader2,
  RefreshCw,
  RotateCcw,
  UserMinus,
  UserSquare2,
  Users,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useUiMode } from "@/hooks/use-ui-mode"
import {
  listAgentCards,
  listAgentParties,
  listCharacters,
  patchCharacter,
  recruitCharacter,
  ApiError,
  type AgentCardSummary,
  type AgentCharacterDef,
  type AgentPartyDto,
  type RecruitCharacterRequest,
} from "@/lib/api"
import type { AgentGuild } from "@/components/omnisight/agents/CharacterCard"
import { AgentRosterTour } from "@/components/omnisight/agents/AgentRosterTour"
import {
  GuildHall,
  type GuildHallGuild,
} from "@/components/omnisight/agents/GuildHall"
import { RecruitModal } from "@/components/omnisight/agents/RecruitModal"
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
  "isp",
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
  isp: "ISP",
  generalist: "Generalist",
}

function isKnownGuild(value: string): value is AgentGuild {
  return (KNOWN_GUILDS as readonly string[]).includes(value)
}

function normaliseGuild(value: string | null | undefined): AgentGuild {
  if (value && isKnownGuild(value)) return value
  return "generalist"
}

function aggregateGuilds(
  cards: AgentCardSummary[],
  characters: Map<string, AgentCharacterDef>,
): GuildHallGuild[] {
  const counts = new Map<AgentGuild, number>()
  for (const card of cards) {
    const character = characters.get(card.agent_id)
    if (character && !character.active) continue
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
  characters: Map<string, AgentCharacterDef>,
  showRetired: boolean,
): Array<{ guild: AgentGuild; cards: AgentCardSummary[] }> {
  const buckets = new Map<AgentGuild, AgentCardSummary[]>()
  for (const card of cards) {
    const character = characters.get(card.agent_id)
    if (!showRetired && character && !character.active) continue
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

function cardDisplayName(
  card: AgentCardSummary,
  character?: AgentCharacterDef,
): string {
  if (character?.display_name?.trim()) return character.display_name.trim()
  const base = card.agent_class || card.agent_id
  return card.instance_suffix ? `${base} ${card.instance_suffix}` : base
}

function initialsFor(value: string): string {
  const parts = value.trim().split(/\s+/).filter(Boolean)
  if (parts.length >= 2) {
    return `${parts[0]?.[0] ?? ""}${parts[1]?.[0] ?? ""}`.toUpperCase()
  }
  return (parts[0]?.slice(0, 2) || "AG").toUpperCase()
}

function apiErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = err.parsed?.detail
    if (typeof detail === "string" && detail.trim()) return detail
    return err.message
  }
  if (err instanceof Error) return err.message
  return String(err)
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
          portraitUrl: card?.portrait_url ?? null,
        }
      }),
  }
}

export default function AgentsRosterPage() {
  const auth = useAuth()
  const router = useRouter()
  // RPG-UI dial: Immersive turns the guild roster into a full-body 立繪 gallery;
  // Focus keeps the dense list. The Guild Hall is the roster's immersive home.
  const { immersive } = useUiMode()
  const [cards, setCards] = useState<AgentCardSummary[]>([])
  const [characters, setCharacters] = useState<AgentCharacterDef[]>([])
  const [parties, setParties] = useState<AgentPartyDto[]>([])
  const [loading, setLoading] = useState(true)
  const [cardsError, setCardsError] = useState<string | null>(null)
  const [charactersError, setCharactersError] = useState<string | null>(null)
  const [partiesError, setPartiesError] = useState<string | null>(null)
  const [recruitOpen, setRecruitOpen] = useState(false)
  const [showRetired, setShowRetired] = useState(false)
  const [characterActionError, setCharacterActionError] = useState<string | null>(null)
  const [patchingSlug, setPatchingSlug] = useState<string | null>(null)

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
    setCharactersError(null)
    setPartiesError(null)
    const [cardsRes, charactersRes, partiesRes] = await Promise.allSettled([
      listAgentCards(),
      listCharacters({ include_retired: true }),
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
    if (charactersRes.status === "fulfilled") {
      setCharacters(charactersRes.value)
    } else {
      const msg =
        charactersRes.reason instanceof Error
          ? charactersRes.reason.message
          : String(charactersRes.reason)
      setCharactersError(msg)
      setCharacters([])
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

  const charactersBySlug = useMemo(() => {
    const map = new Map<string, AgentCharacterDef>()
    for (const character of characters) map.set(character.slug, character)
    return map
  }, [characters])
  const guildSummaries = useMemo(
    () => aggregateGuilds(cards, charactersBySlug),
    [cards, charactersBySlug],
  )
  const grouped = useMemo(
    () => groupCardsByGuild(cards, charactersBySlug, showRetired),
    [cards, charactersBySlug, showRetired],
  )
  const cardLookup = useMemo(() => {
    const map = new Map<string, AgentCardSummary>()
    for (const card of cards) map.set(card.agent_id, card)
    return map
  }, [cards])
  const partyViewModels = useMemo(
    () => parties.map((p) => mapParty(p, cardLookup)),
    [parties, cardLookup],
  )
  const retiredCount = useMemo(
    () => characters.filter((character) => !character.active).length,
    [characters],
  )

  const handleRecruit = useCallback(
    async (payload: RecruitCharacterRequest) => {
      await recruitCharacter(payload)
      setRecruitOpen(false)
      await refresh()
    },
    [refresh],
  )

  const handlePatchActive = useCallback(
    async (character: AgentCharacterDef, active: boolean) => {
      setPatchingSlug(character.slug)
      setCharacterActionError(null)
      try {
        await patchCharacter(character.slug, { active })
        await refresh()
      } catch (err) {
        setCharacterActionError(apiErrorMessage(err))
      } finally {
        setPatchingSlug(null)
      }
    },
    [refresh],
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
      <AgentRosterTour />
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

        {charactersError && (
          <div
            className="mb-4 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
            data-testid="agents-roster-characters-error"
          >
            Failed to load character definitions: {charactersError}
          </div>
        )}

        <section className="mb-8" data-testid="agents-roster-guild-hall">
          <GuildHall guilds={guildSummaries} onRecruit={() => setRecruitOpen(true)} />
        </section>

        <section className="mb-8" data-testid="agents-roster-grid">
          <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold leading-tight">Agent roster</h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Click an agent to open the Character Card detail view.
              </p>
            </div>
            <label className="inline-flex items-center gap-2 rounded border bg-card px-2.5 py-1.5 font-mono text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={showRetired}
                onChange={(event) => setShowRetired(event.target.checked)}
                className="size-3.5"
                data-testid="agents-roster-show-retired"
              />
              Show retired
              <span className="rounded-sm bg-muted px-1.5 py-0.5 text-[10px]">
                {retiredCount}
              </span>
            </label>
          </div>

          {characterActionError ? (
            <div
              className="mb-3 rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
              data-testid="agents-roster-character-action-error"
            >
              {characterActionError}
            </div>
          ) : null}

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
                  <ul className={immersive ? "grid gap-3 grid-cols-2 sm:grid-cols-3 xl:grid-cols-4" : "grid gap-2 sm:grid-cols-2 xl:grid-cols-3"}>
                    {bucket.map((card) => (
                      <li key={card.agent_id}>
                        {(() => {
                          const character = charactersBySlug.get(card.agent_id)
                          const isRetired = Boolean(character && !character.active)
                          const displayName = cardDisplayName(card, character)
                          return (
                            immersive ? (
                              // Immersive Guild Hall: a full-body 立繪 gallery card.
                              <Link
                                href={`/agents/${encodeURIComponent(card.agent_id)}`}
                                data-testid="agents-roster-card-link"
                                data-agent-id={card.agent_id}
                                data-character-active={isRetired ? "false" : "true"}
                                className={[
                                  "group flex flex-col overflow-hidden rounded-lg border bg-card transition hover:border-primary/60 hover:shadow-lg",
                                  isRetired ? "opacity-50 grayscale" : "",
                                ].join(" ")}
                              >
                                <div className="relative flex aspect-[3/4] items-end justify-center overflow-hidden bg-gradient-to-b from-primary/10 via-background to-background">
                                  {card.fullbody_url ? (
                                    // eslint-disable-next-line @next/next/no-img-element
                                    <img
                                      src={card.fullbody_url}
                                      alt=""
                                      className="h-full w-full object-contain drop-shadow-lg transition-transform duration-300 group-hover:scale-105"
                                    />
                                  ) : card.portrait_url ? (
                                    // eslint-disable-next-line @next/next/no-img-element
                                    <img src={card.portrait_url} alt="" className="size-20 rounded-md object-cover" />
                                  ) : (
                                    <span className="flex size-full items-center justify-center text-3xl font-bold text-muted-foreground">
                                      {displayName ? initialsFor(displayName) : "?"}
                                    </span>
                                  )}
                                  {isRetired ? (
                                    <span className="absolute left-1.5 top-1.5 rounded-sm border border-muted-foreground/30 bg-background/70 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-muted-foreground">
                                      Retired
                                    </span>
                                  ) : null}
                                </div>
                                <div className="flex items-center justify-between gap-2 px-2 py-1.5">
                                  <span className="min-w-0 truncate text-sm font-medium">{displayName}</span>
                                  <span className="shrink-0 rounded-sm bg-muted px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                                    Lv{card.level}
                                  </span>
                                </div>
                              </Link>
                            ) : (
                            <Link
                              href={`/agents/${encodeURIComponent(card.agent_id)}`}
                              data-testid="agents-roster-card-link"
                              data-agent-id={card.agent_id}
                              data-character-active={isRetired ? "false" : "true"}
                              className={[
                                "flex items-center justify-between gap-3 rounded-md border bg-card px-3 py-2 hover:border-primary/60 hover:bg-accent transition-colors",
                                isRetired ? "opacity-50 grayscale" : "",
                              ].join(" ")}
                            >
                              <span
                                className="flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-md border bg-muted text-xs font-semibold uppercase text-muted-foreground"
                                aria-hidden="true"
                              >
                                {card.portrait_url ? (
                                  // eslint-disable-next-line @next/next/no-img-element
                                  <img
                                    src={card.portrait_url}
                                    alt=""
                                    className="size-full object-cover"
                                  />
                                ) : displayName ? (
                                  <span>{initialsFor(displayName)}</span>
                                ) : (
                                  <UserSquare2 className="size-4" aria-hidden="true" />
                                )}
                              </span>
                              <span className="min-w-0">
                                <span className="block truncate text-sm font-medium">
                                  {displayName}
                                </span>
                                <span className="block truncate text-[10px] uppercase tracking-wider text-muted-foreground">
                                  {card.specialization_label || card.agent_id}
                                </span>
                              </span>
                              <span className="flex shrink-0 items-center gap-2">
                                {isRetired ? (
                                  <span className="rounded-sm border border-muted-foreground/30 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                                    Retired
                                  </span>
                                ) : null}
                                <span className="rounded-sm bg-muted px-2 py-0.5 font-mono text-[11px] uppercase tracking-wider text-muted-foreground">
                                  Lv {card.level}
                                </span>
                              </span>
                            </Link>
                            )
                          )
                        })()}
                        {(() => {
                          const character = charactersBySlug.get(card.agent_id)
                          if (!character || character.built_in) return null
                          const isRetired = !character.active
                          const busy = patchingSlug === character.slug
                          return (
                            <div className="mt-1 flex justify-end">
                              <button
                                type="button"
                                onClick={() => void handlePatchActive(character, isRetired)}
                                disabled={busy}
                                className="inline-flex items-center gap-1 rounded border border-border bg-background px-2 py-1 font-mono text-[10px] text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                                data-testid="agents-roster-character-active-toggle"
                                data-character-slug={character.slug}
                              >
                                {busy ? (
                                  <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                                ) : isRetired ? (
                                  <RotateCcw className="size-3" aria-hidden="true" />
                                ) : (
                                  <UserMinus className="size-3" aria-hidden="true" />
                                )}
                                {isRetired ? "Reactivate" : "Retire"}
                              </button>
                            </div>
                          )
                        })()}
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
      <RecruitModal
        open={recruitOpen}
        guilds={KNOWN_GUILDS}
        onClose={() => setRecruitOpen(false)}
        onRecruit={handleRecruit}
      />
    </main>
  )
}

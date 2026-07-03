"use client"

/**
 * OP-1460 (Sprint G.C) — Character Card detail route.
 *
 * Drill-down target from the `/agents` GuildHall grid. Pulls the
 * stat-sheet JSON from `GET /api/v1/agents/{agent_id}/card` (RPG.W1.3,
 * `backend/routers/agents.py::get_agent_card`) and renders the
 * presentational `<CharacterCard>` shell from
 * `components/omnisight/agents/CharacterCard.tsx`.
 *
 * Skill / talent / tool tabs are owned by sibling endpoints and are
 * deliberately left out of this slim shell — the ticket scope is
 * landing-on-route + Character Card surface.
 */

import { useCallback, useEffect, useState } from "react"
import Link from "next/link"
import { useParams, useRouter } from "next/navigation"
import { ArrowLeft, ChevronRight, Loader2, RefreshCw, UserSquare2 } from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import {
  ApiError,
  getAgentAchievements,
  getAgentCard,
  getAgentTalents,
  type AgentAchievementBadge,
  type AgentCardDetail,
  type AgentTalentsResponse,
} from "@/lib/api"
import {
  CharacterCard,
  type AgentGuild,
  type CharacterBadge,
  type CharacterBadgeKind,
  type CharacterBadgeRarity,
  type CharacterTalentMilestone,
} from "@/components/omnisight/agents/CharacterCard"

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

const KNOWN_BADGE_KINDS: readonly CharacterBadgeKind[] = [
  "pr_merged_100",
  "regression_streak_30",
  "taught_agents_5",
  "campaign",
  "custom",
]

const KNOWN_BADGE_RARITIES: readonly CharacterBadgeRarity[] = [
  "bronze",
  "silver",
  "gold",
  "legendary",
]

function isKnownGuild(value: string): value is AgentGuild {
  return (KNOWN_GUILDS as readonly string[]).includes(value)
}

function isKnownBadgeKind(value: string): value is CharacterBadgeKind {
  return (KNOWN_BADGE_KINDS as readonly string[]).includes(value)
}

function normaliseBadgeKind(value: string | null | undefined): CharacterBadgeKind {
  if (value && isKnownBadgeKind(value)) return value
  return "custom"
}

function isKnownBadgeRarity(value: string): value is CharacterBadgeRarity {
  return (KNOWN_BADGE_RARITIES as readonly string[]).includes(value)
}

function normaliseBadgeRarity(
  value: string | null | undefined,
): CharacterBadgeRarity | null {
  if (value && isKnownBadgeRarity(value)) return value
  return null
}

function normaliseGuild(value: string | null | undefined): AgentGuild {
  if (value && isKnownGuild(value)) return value
  return "generalist"
}

// Mirrors the W1.3 leveling table heuristic: each level needs ~1000 XP
// to advance. The backend doesn't yet surface `next_level_xp` on the
// stat-sheet endpoint, so we derive a stable display value here. When
// the W12-style `next_level_xp` ships on the card endpoint, this stub
// drops in favour of the real number.
function deriveNextLevelXp(level: number): number {
  const safeLevel = Number.isFinite(level) && level > 0 ? Math.trunc(level) : 1
  return safeLevel * 1000
}

function cardDisplayName(card: AgentCardDetail): string {
  const base = card.agent_class || card.agent_id
  return card.instance_suffix ? `${base} ${card.instance_suffix}` : base
}

function achievementBadgeToCharacterBadge(badge: AgentAchievementBadge): CharacterBadge {
  return {
    id: badge.id,
    kind: normaliseBadgeKind(badge.kind),
    label: badge.label ?? null,
    description: badge.description ?? null,
    earnedAt: badge.earnedAt ?? null,
    progressLabel: badge.progressLabel ?? null,
    rarity: normaliseBadgeRarity(badge.rarity),
    locked: badge.locked ?? null,
  }
}

function talentsResponseToMilestones(
  talents: AgentTalentsResponse,
): CharacterTalentMilestone[] {
  const choices = new Map(
    talents.choices.map((choice) => [choice.milestone_level, choice.talent_id]),
  )
  const pending = new Set(talents.pending_milestone_forks)
  return talents.milestones.map((milestone) => ({
    milestoneLevel: milestone,
    options: [],
    chosenTalentId: choices.get(milestone) ?? null,
    choiceRequired: pending.has(milestone),
  }))
}

export default function AgentCharacterCardPage() {
  const auth = useAuth()
  const router = useRouter()
  const params = useParams<{ agent_id: string }>()
  const rawAgentId = Array.isArray(params?.agent_id)
    ? params.agent_id[0]
    : params?.agent_id
  const agentId = typeof rawAgentId === "string" ? rawAgentId : ""

  const [card, setCard] = useState<AgentCardDetail | null>(null)
  const [badges, setBadges] = useState<CharacterBadge[]>([])
  const [talents, setTalents] = useState<CharacterTalentMilestone[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notFound, setNotFound] = useState(false)

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") {
      const next =
        typeof window !== "undefined"
          ? window.location.pathname + window.location.search
          : `/agents/${agentId}`
      router.replace(`/login?next=${encodeURIComponent(next)}`)
    }
  }, [auth.loading, auth.user, auth.authMode, router, agentId])

  const refresh = useCallback(async () => {
    if (!agentId) return
    setLoading(true)
    setError(null)
    setNotFound(false)
    try {
      const [result, achievements, talentSummary] = await Promise.all([
        getAgentCard(agentId),
        getAgentAchievements(agentId),
        getAgentTalents(agentId),
      ])
      setCard(result)
      setBadges(achievements.unlocked.map(achievementBadgeToCharacterBadge))
      setTalents(talentsResponseToMilestones(talentSummary))
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 404) {
        setNotFound(true)
        setCard(null)
        setBadges([])
        setTalents([])
      } else {
        const msg = exc instanceof Error ? exc.message : String(exc)
        setError(msg)
        setCard(null)
        setBadges([])
        setTalents([])
      }
    } finally {
      setLoading(false)
    }
  }, [agentId])

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") return
    const timer = window.setTimeout(() => {
      void refresh()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [auth.loading, auth.user, auth.authMode, refresh])

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
      data-testid="agent-character-card-page"
      data-agent-id={agentId}
    >
      <div className="max-w-4xl mx-auto">
        <header className="flex items-start justify-between gap-4 mb-6 flex-wrap">
          <div>
            <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-1">
              <Link
                href="/agents"
                className="hover:text-[var(--foreground)] inline-flex items-center gap-1"
              >
                <ArrowLeft size={10} /> roster
              </Link>
              <ChevronRight size={10} />
              <span className="text-[var(--foreground)] truncate max-w-[24ch]">
                {agentId || "unknown"}
              </span>
            </div>
            <h1 className="text-xl font-semibold flex items-center gap-2">
              <UserSquare2 size={20} />
              Character Card
            </h1>
            <p className="text-xs text-[var(--muted-foreground)] mt-1">
              Layer-1 stat sheet for the selected agent.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading || !agentId}
            className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--secondary)]/40 text-xs font-mono disabled:opacity-50"
            data-testid="agent-character-card-refresh"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
        </header>

        {!agentId && (
          <div
            className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
            data-testid="agent-character-card-missing-id"
          >
            Missing agent id in URL.
          </div>
        )}

        {agentId && loading && !card && (
          <div
            className="rounded-md border border-dashed bg-muted/20 p-6 text-center text-xs font-mono text-muted-foreground"
            data-testid="agent-character-card-loading"
          >
            <Loader2 size={14} className="animate-spin inline-block mr-2" />
            Loading character card...
          </div>
        )}

        {error && (
          <div
            className="rounded border border-[var(--destructive)]/40 bg-[var(--destructive)]/10 p-3 text-xs font-mono text-[var(--destructive)]"
            data-testid="agent-character-card-error"
          >
            Failed to load character card: {error}
          </div>
        )}

        {notFound && (
          <div
            className="rounded border border-[var(--border)] bg-[var(--card)] p-4 text-sm font-mono text-[var(--muted-foreground)]"
            data-testid="agent-character-card-not-found"
          >
            No Character Card found for agent <span className="text-[var(--foreground)]">{agentId}</span>.{" "}
            <Link href="/agents" className="underline hover:text-[var(--foreground)]">
              Back to roster
            </Link>
            .
          </div>
        )}

        {card && (
          <CharacterCard
            agentId={card.agent_id}
            displayName={cardDisplayName(card)}
            guild={normaliseGuild(card.guild)}
            level={card.level}
            xp={card.xp}
            nextLevelXp={deriveNextLevelXp(card.level)}
            specialization={card.specialization_label}
            instanceSuffix={card.instance_suffix}
            portraitUrl={card.portrait_url ?? null}
            voice={card.voice ?? null}
            styleFingerprint={card.style_fingerprint}
            badges={badges}
            talents={talents}
            onTalentLocked={refresh}
          />
        )}
      </div>
    </main>
  )
}

"use client"

/**
 * RPG.W8.1 — operator-facing agent Character Card shell.
 *
 * Scope: the compact RPG-style identity card only. Data loading,
 * instance switching, skill radar, talent tree, and tool proficiency
 * tabs are separate W8 follow-ups per ADR-0008.
 *
 * Sub-wave coverage (newest first — these blocks ship as inline
 * sections on the W8.1 shell. ADR-0008 §"Implementation split" line
 * 191 calls these the "Stats / Skills / Tools" three-tab layout; the
 * `data-testid` strings on each section name the conceptual tab so a
 * future tabbed refactor only re-parents the existing JSX):
 *
 * - W15.3 (OP-1384): active buff / debuff icons render in the card
 *   header from `CharacterBuff`, with kind / polarity / label metadata
 *   kept on each icon for operator tooling and UI tests.
 *
 * - W13.6 (OP-183): the `data-testid="character-card-tools"` section
 *   below — per-tool row rendered from `CharacterTool` with the Lv
 *   header, invocation count, success-rate bar, and the
 *   gate-blocked banner that surfaces when `level < requiredLevel`.
 *   The required-level number on each row mirrors the YAML row in
 *   `config/tool_proficiency_gates.yaml` (W13.3 / OP-180 ship); the
 *   bar's `aria-valuenow` is the success-rate percent so screen
 *   readers hear the same number an operator sees. The
 *   `getLevelProgressPercent` clamp is reused for the row bar so the
 *   tools section honours the same NaN / overflow behaviour as the
 *   header level bar — see `character-card-tools.test.tsx` for the
 *   contract.
 *
 * - Future W8.x tabs: when the three-tab layout lands, each section
 *   below moves into a tab panel keyed by `character-card-{stats,
 *   skills,tools}`. The `data-testid` strings on each section already
 *   match the conceptual tab id, so the refactor is a re-parent, not
 *   a rewrite.
 */

import {
  AlertTriangle,
  Award,
  BrainCircuit,
  Code2,
  Crown,
  Database,
  Gem,
  GraduationCap,
  Laptop,
  Medal,
  ServerCog,
  Shield,
  Smartphone,
  Sparkles,
  Trophy,
  Wrench,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import type { ReactElement } from "react"
import { useMemo, useState } from "react"

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

import { SkillBranchTree } from "./SkillBranchTree"
import { TalentForkModal } from "./TalentForkModal"

export type AgentGuild =
  | "backend"
  | "frontend"
  | "security"
  | "devops"
  | "data"
  | "mobile"
  | "embedded"
  | "isp"
  | "generalist"

export interface CharacterCardProps {
  agentId: string
  displayName: string
  guild: AgentGuild
  level: number
  xp: number
  nextLevelXp: number
  specialization: string
  className?: string
  portraitUrl?: string | null
  instanceSuffix?: string | null
  styleFingerprint?: string | null
  buffs?: readonly CharacterBuff[]
  badges?: readonly CharacterBadge[]
  skills?: readonly CharacterSkill[]
  tools?: readonly CharacterTool[]
  talents?: readonly CharacterTalentMilestone[]
  capstone?: CharacterCapstone | null
  onLockBranch?: (skillId: string, branchId: string) => void
  onLockTalent?: (milestoneLevel: number, talentId: string) => void
  onTalentLocked?: () => void | Promise<void>
  onLockCapstone?: () => void
}

export interface CharacterTalentOption {
  talentId: string
  displayName: string
  summary?: string | null
  routingLabel?: string | null
}

export interface CharacterTalentMilestone {
  milestoneLevel: number
  options: readonly CharacterTalentOption[]
  /** The locked talent_id for this milestone, or null if the operator hasn't chosen yet. */
  chosenTalentId?: string | null
  /** True when ``agent.level >= milestoneLevel`` AND ``chosenTalentId`` is null. */
  choiceRequired?: boolean | null
}

export interface CharacterCapstone {
  abilityId: string
  displayName: string
  summary?: string | null
  /** True when ``agent.level >= 80`` and the Lv-80 milestone talent is locked. */
  pickable?: boolean | null
  /** True when the capstone ability has been locked. */
  locked?: boolean | null
}

/**
 * One row in the Character Card "Tools" tab (W13.6 / OP-183).
 *
 * Mirrors `agent_tool_proficiency` (alembic 0227): `level` is the Lv
 * 1-5 derived per ADR-0008 thresholds, `invocationCount` /
 * `successCount` are the raw counters that feed the success-rate bar,
 * and `requiredLevel` is the per-tool YAML gate from
 * `config/tool_proficiency_gates.yaml` (W13.3 / OP-180). When the
 * agent's `level` on the row falls below `requiredLevel`, the row
 * renders the gate-blocked banner instead of suppressing the row, so
 * operators can see *why* the dispatcher refused without diffing the
 * tool_proficiency_insufficient SSE event against the YAML.
 */
export interface CharacterTool {
  toolId: string
  displayName?: string | null
  level: number
  invocationCount: number
  successCount: number
  requiredLevel?: number | null
  lastUsedAt?: string | null
}

export interface CharacterSkillBranchOption {
  branchId: string
  displayName: string
  summary?: string | null
}

export interface CharacterSkill {
  skillId: string
  displayName?: string | null
  level: number
  xp: number
  nextLevelXp: number
  branchChoice?: string | null
  branchChoiceRequired?: boolean | null
  branchOptions?: readonly CharacterSkillBranchOption[]
  lastActiveAt?: string | null
}

export type CharacterBuffKind =
  | "fresh_tokens"
  | "well_rested"
  | "streak"
  | "cap_warning"
  | "burnout"
  | "stale_memory"
  | "custom"

export type CharacterBuffPolarity = "buff" | "debuff" | "warning"

export interface CharacterBuff {
  id?: string
  kind: CharacterBuffKind
  label?: string | null
  description?: string | null
  expiresIn?: string | null
  stacks?: number | null
  polarity?: CharacterBuffPolarity | null
}

export type CharacterBadgeKind =
  | "pr_merged_100"
  | "regression_streak_30"
  | "taught_agents_5"
  | "campaign"
  | "custom"

export type CharacterBadgeRarity = "bronze" | "silver" | "gold" | "legendary"

export interface CharacterBadge {
  id?: string
  kind: CharacterBadgeKind
  label?: string | null
  description?: string | null
  earnedAt?: string | null
  progressLabel?: string | null
  rarity?: CharacterBadgeRarity | null
  locked?: boolean | null
}

interface GuildVisual {
  label: string
  crestLabel: string
  Icon: LucideIcon
  toneClass: string
  barClass: string
  portraitClass: string
}

interface BuffVisual {
  label: string
  Icon: LucideIcon
  toneClass: string
}

interface BadgeVisual {
  label: string
  Icon: LucideIcon
  toneClass: string
}

const GUILD_VISUALS: Record<AgentGuild, GuildVisual> = {
  backend: {
    label: "Backend Guild",
    crestLabel: "BE",
    Icon: ServerCog,
    toneClass: "border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-300",
    barClass: "bg-sky-500",
    portraitClass: "from-sky-500/20 via-emerald-500/10 to-background",
  },
  frontend: {
    label: "Frontend Guild",
    crestLabel: "FE",
    Icon: Code2,
    toneClass:
      "border-fuchsia-500/30 bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300",
    barClass: "bg-fuchsia-500",
    portraitClass: "from-fuchsia-500/20 via-amber-500/10 to-background",
  },
  security: {
    label: "Security Guild",
    crestLabel: "SE",
    Icon: Shield,
    toneClass:
      "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300",
    barClass: "bg-rose-500",
    portraitClass: "from-rose-500/20 via-slate-500/10 to-background",
  },
  devops: {
    label: "DevOps Guild",
    crestLabel: "DO",
    Icon: Wrench,
    toneClass:
      "border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-300",
    barClass: "bg-orange-500",
    portraitClass: "from-orange-500/20 via-cyan-500/10 to-background",
  },
  data: {
    label: "Data Guild",
    crestLabel: "DA",
    Icon: Database,
    toneClass:
      "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    barClass: "bg-emerald-500",
    portraitClass: "from-emerald-500/20 via-violet-500/10 to-background",
  },
  mobile: {
    label: "Mobile Guild",
    crestLabel: "MO",
    Icon: Smartphone,
    toneClass:
      "border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
    barClass: "bg-cyan-500",
    portraitClass: "from-cyan-500/20 via-lime-500/10 to-background",
  },
  embedded: {
    label: "Embedded Guild",
    crestLabel: "EM",
    Icon: Laptop,
    toneClass:
      "border-lime-500/30 bg-lime-500/10 text-lime-700 dark:text-lime-300",
    barClass: "bg-lime-500",
    portraitClass: "from-lime-500/20 via-zinc-500/10 to-background",
  },
  isp: {
    label: "ISP Guild",
    crestLabel: "ISP",
    Icon: Laptop,
    toneClass:
      "border-teal-500/30 bg-teal-500/10 text-teal-700 dark:text-teal-300",
    barClass: "bg-teal-500",
    portraitClass: "from-teal-500/20 via-fuchsia-500/10 to-background",
  },
  generalist: {
    label: "Generalist Guild",
    crestLabel: "GN",
    Icon: BrainCircuit,
    toneClass:
      "border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300",
    barClass: "bg-violet-500",
    portraitClass: "from-violet-500/20 via-sky-500/10 to-background",
  },
}

const BUFF_VISUALS: Record<CharacterBuffKind, BuffVisual> = {
  fresh_tokens: {
    label: "Fresh Tokens",
    Icon: Sparkles,
    toneClass: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  },
  well_rested: {
    label: "Well-Rested",
    Icon: Gem,
    toneClass: "border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
  },
  streak: {
    label: "Streak",
    Icon: Medal,
    toneClass: "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  },
  cap_warning: {
    label: "Cap Warning",
    Icon: Shield,
    toneClass: "border-orange-500/30 bg-orange-500/10 text-orange-700 dark:text-orange-300",
  },
  burnout: {
    label: "Burnout",
    Icon: Wrench,
    toneClass: "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300",
  },
  stale_memory: {
    label: "Stale Memory",
    Icon: BrainCircuit,
    toneClass: "border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-300",
  },
  custom: {
    label: "Custom Effect",
    Icon: Sparkles,
    toneClass: "border-muted-foreground/30 bg-muted/40 text-muted-foreground",
  },
}

const BADGE_VISUALS: Record<CharacterBadgeKind, BadgeVisual> = {
  pr_merged_100: {
    label: "100 PRs Merged",
    Icon: Trophy,
    toneClass:
      "border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  },
  regression_streak_30: {
    label: "30 Regression-Free",
    Icon: Shield,
    toneClass:
      "border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  },
  taught_agents_5: {
    label: "Taught 5 Agents",
    Icon: GraduationCap,
    toneClass:
      "border-cyan-500/35 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300",
  },
  campaign: {
    label: "Campaign Badge",
    Icon: Crown,
    toneClass:
      "border-fuchsia-500/35 bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300",
  },
  custom: {
    label: "Achievement",
    Icon: Award,
    toneClass: "border-muted-foreground/30 bg-muted/40 text-muted-foreground",
  },
}

const BADGE_RARITY_CLASS: Record<CharacterBadgeRarity, string> = {
  bronze: "after:bg-orange-500",
  silver: "after:bg-slate-400",
  gold: "after:bg-amber-500",
  legendary: "after:bg-fuchsia-500",
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(100, value))
}

export function getLevelProgressPercent(xp: number, nextLevelXp: number): number {
  if (!Number.isFinite(nextLevelXp) || nextLevelXp <= 0) return 0
  return clampPercent((xp / nextLevelXp) * 100)
}

function initialsFor(name: string): string {
  const parts = name
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
  if (parts.length === 0) return "AI"
  return parts.map((part) => part[0]?.toUpperCase()).join("")
}

function buffTitle(buff: CharacterBuff, fallbackLabel: string): string {
  const parts = [buff.label?.trim() || fallbackLabel]
  if (buff.description?.trim()) parts.push(buff.description.trim())
  if (buff.expiresIn?.trim()) parts.push(`Expires ${buff.expiresIn.trim()}`)
  if (Number.isFinite(buff.stacks) && Number(buff.stacks) > 1) {
    parts.push(`${Math.trunc(Number(buff.stacks))} stacks`)
  }
  return parts.join(" · ")
}

function badgeTitle(badge: CharacterBadge, fallbackLabel: string): string {
  const parts = [badge.label?.trim() || fallbackLabel]
  if (badge.description?.trim()) parts.push(badge.description.trim())
  if (badge.earnedAt?.trim()) parts.push(`Earned ${badge.earnedAt.trim()}`)
  if (badge.progressLabel?.trim()) parts.push(badge.progressLabel.trim())
  return parts.join(" · ")
}

export function CharacterCard({
  agentId,
  displayName,
  guild,
  level,
  xp,
  nextLevelXp,
  specialization,
  className,
  portraitUrl,
  instanceSuffix,
  styleFingerprint,
  buffs = [],
  badges = [],
  skills = [],
  tools = [],
  talents = [],
  capstone = null,
  onLockBranch,
  onLockTalent,
  onTalentLocked,
  onLockCapstone,
}: CharacterCardProps): ReactElement {
  const [talentModalMilestone, setTalentModalMilestone] = useState<number | null>(null)
  const visual = GUILD_VISUALS[guild] ?? GUILD_VISUALS.generalist
  const progress = getLevelProgressPercent(xp, nextLevelXp)
  const GuildIcon = visual.Icon
  const activeBuffs = buffs.filter((buff) => buff && BUFF_VISUALS[buff.kind])
  const visibleBadges = badges.filter((badge) => badge && BADGE_VISUALS[badge.kind])
  const visibleSkills = skills.filter((skill) => skill && skill.skillId)
  const visibleTools = tools.filter((tool) => tool && tool.toolId)
  const visibleTalents = talents
    .filter((talent) => talent && Number.isFinite(talent.milestoneLevel))
    .slice()
    .sort((a, b) => a.milestoneLevel - b.milestoneLevel)
  const pendingTalentMilestones = useMemo(
    () =>
      visibleTalents
        .filter((talent) => Boolean(talent.choiceRequired) && !talent.chosenTalentId)
        .map((talent) => talent.milestoneLevel),
    [visibleTalents],
  )
  const activeTalentModalMilestone = talentModalMilestone !== null
    && pendingTalentMilestones.includes(talentModalMilestone)
      ? talentModalMilestone
      : null

  return (
    <article
      className={cn(
        "relative overflow-hidden rounded-lg border bg-card text-card-foreground shadow-sm",
        "before:absolute before:inset-x-0 before:top-0 before:h-1 before:bg-gradient-to-r before:from-transparent before:via-primary/60 before:to-transparent",
        className,
      )}
      data-agent-id={agentId}
      data-agent-guild={guild}
      data-testid="character-card"
    >
      <div className="grid gap-4 p-4 sm:grid-cols-[8rem_1fr]">
        <div
          className={cn(
            "relative flex min-h-32 items-center justify-center rounded-md border bg-gradient-to-br p-3",
            visual.portraitClass,
          )}
          data-testid="character-card-portrait"
        >
          <Avatar className="size-24 rounded-md border bg-background shadow-sm">
            {portraitUrl ? (
              <AvatarImage src={portraitUrl} alt={`${displayName} portrait`} />
            ) : null}
            <AvatarFallback className="rounded-md text-xl font-semibold">
              {initialsFor(displayName)}
            </AvatarFallback>
          </Avatar>

          <div
            aria-label={visual.label}
            className={cn(
              "absolute -right-2 -top-2 flex size-11 items-center justify-center rounded-md border shadow-sm",
              visual.toneClass,
            )}
            title={visual.label}
            data-testid="character-card-guild-crest"
          >
            <GuildIcon className="size-5" aria-hidden="true" />
            <span className="sr-only">{visual.crestLabel}</span>
          </div>
        </div>

        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="truncate text-lg font-semibold leading-tight">
                  {displayName}
                </h3>
                {instanceSuffix ? (
                  <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">
                    {instanceSuffix}
                  </Badge>
                ) : null}
              </div>
              <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
                {agentId}
              </p>
            </div>

            <Badge
              variant="outline"
              className={cn("h-7 gap-1.5 px-2 text-xs", visual.toneClass)}
            >
              <GuildIcon className="size-3.5" aria-hidden="true" />
              {visual.label}
            </Badge>
          </div>

          {activeBuffs.length > 0 ? (
            <div
              aria-label="Active character buffs"
              className="flex flex-wrap items-center gap-1.5"
              data-testid="character-card-buffs"
            >
              {activeBuffs.map((buff, index) => {
                const buffVisual = BUFF_VISUALS[buff.kind]
                const BuffIcon = buffVisual.Icon
                const label = buff.label?.trim() || buffVisual.label
                const stacks =
                  Number.isFinite(buff.stacks) && Number(buff.stacks) > 1
                    ? Math.trunc(Number(buff.stacks))
                    : null

                return (
                  <div
                    key={buff.id ?? `${buff.kind}-${index}`}
                    aria-label={label}
                    className={cn(
                      "relative flex size-8 items-center justify-center rounded-md border",
                      buffVisual.toneClass,
                    )}
                    data-buff-kind={buff.kind}
                    data-buff-label={label}
                    data-buff-polarity={buff.polarity ?? ""}
                    data-testid="character-card-buff"
                    title={buffTitle(buff, buffVisual.label)}
                  >
                    <BuffIcon className="size-4" aria-hidden="true" />
                    <span className="sr-only">{label}</span>
                    {stacks ? (
                      <span className="absolute -right-1 -top-1 flex min-w-4 items-center justify-center rounded-full border bg-background px-1 font-mono text-[9px] leading-4 text-foreground shadow-sm">
                        {stacks}
                      </span>
                    ) : null}
                  </div>
                )
              })}
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
            <div className="min-w-0" data-testid="character-card-level">
              <div className="mb-1.5 flex items-center justify-between gap-3 text-xs">
                <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
                  <Medal className="size-3.5 text-amber-500" aria-hidden="true" />
                  Level {Math.max(1, Math.trunc(level))}
                </span>
                <span className="font-mono text-muted-foreground">
                  {Math.max(0, Math.trunc(xp)).toLocaleString()} /{" "}
                  {Math.max(0, Math.trunc(nextLevelXp)).toLocaleString()} XP
                </span>
              </div>
              <div
                aria-label={`Level progress ${Math.round(progress)} percent`}
                className="h-2 overflow-hidden rounded-full bg-muted"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(progress)}
              >
                <div
                  className={cn("h-full rounded-full transition-all", visual.barClass)}
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>

            <div
              className="rounded-md border bg-muted/30 px-3 py-2 sm:min-w-40"
              data-testid="character-card-specialization"
            >
              <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase text-muted-foreground">
                <Sparkles className="size-3 text-amber-500" aria-hidden="true" />
                Specialization
              </div>
              <div className="mt-1 text-sm font-semibold leading-tight">
                {specialization}
              </div>
            </div>
          </div>

          {styleFingerprint ? (
            <div className="flex items-center gap-2 rounded-md border border-dashed bg-background/60 px-3 py-2 text-xs text-muted-foreground">
              <Gem className="size-3.5 text-emerald-500" aria-hidden="true" />
              <span className="min-w-0 truncate">
                Style fingerprint:{" "}
                <span className="font-mono text-foreground">{styleFingerprint}</span>
              </span>
            </div>
          ) : null}

          {visibleSkills.length > 0 ? (
            <section
              aria-label="Agent skill tree"
              className="rounded-md border bg-background/50 p-3"
              data-testid="character-card-skills"
            >
              <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium uppercase text-muted-foreground">
                <GraduationCap className="size-3.5 text-emerald-500" aria-hidden="true" />
                Skills
              </div>
              <ul className="flex flex-col gap-2">
                {visibleSkills.map((skill) => {
                  const skillProgress = getLevelProgressPercent(skill.xp, skill.nextLevelXp)
                  const branchLocked = Boolean(skill.branchChoice)
                  return (
                    <li
                      key={skill.skillId}
                      className="rounded-md border bg-background/40 p-2"
                      data-skill-id={skill.skillId}
                      data-skill-level={skill.level}
                      data-skill-branch={skill.branchChoice ?? ""}
                      data-testid="character-card-skill"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-mono text-xs font-semibold">
                          {skill.displayName?.trim() || skill.skillId}
                        </span>
                        <span className="font-mono text-[10px] text-muted-foreground">
                          Lv {Math.max(1, Math.trunc(skill.level))} · {Math.max(0, Math.trunc(skill.xp)).toLocaleString()} /{" "}
                          {Math.max(0, Math.trunc(skill.nextLevelXp)).toLocaleString()} XP
                        </span>
                      </div>
                      <div
                        aria-label={`Skill ${skill.skillId} progress ${Math.round(skillProgress)} percent`}
                        className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted"
                        role="progressbar"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={Math.round(skillProgress)}
                      >
                        <div
                          className={cn("h-full rounded-full transition-all", visual.barClass)}
                          style={{ width: `${skillProgress}%` }}
                        />
                      </div>
                      {branchLocked ? (
                        <p
                          className="mt-1 text-[10px] text-muted-foreground"
                          data-testid="character-card-skill-branch-locked"
                        >
                          Branch: <span className="font-mono text-foreground">{skill.branchChoice}</span> (locked)
                        </p>
                      ) : null}
                      <SkillBranchTree skill={skill} onLockBranch={onLockBranch} />
                    </li>
                  )
                })}
              </ul>
            </section>
          ) : null}

          {/* Tools tab (W13.6 / OP-183) — see module docstring. */}
          {visibleTools.length > 0 ? (
            <section
              aria-label="Agent tool proficiency"
              className="rounded-md border bg-background/50 p-3"
              data-testid="character-card-tools"
            >
              <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium uppercase text-muted-foreground">
                <Wrench className="size-3.5 text-sky-500" aria-hidden="true" />
                Tools
              </div>
              <ul className="flex flex-col gap-2">
                {visibleTools.map((tool) => {
                  const invocations = Math.max(0, Math.trunc(tool.invocationCount))
                  const successes = Math.max(0, Math.min(invocations, Math.trunc(tool.successCount)))
                  const ratio = invocations > 0 ? successes / invocations : 0
                  const ratioPercent = clampPercent(ratio * 100)
                  const requiredLevel = Number.isFinite(tool.requiredLevel)
                    ? Math.max(1, Math.trunc(Number(tool.requiredLevel)))
                    : null
                  const blocked = requiredLevel !== null && tool.level < requiredLevel
                  return (
                    <li
                      key={tool.toolId}
                      className="rounded-md border bg-background/40 p-2"
                      data-tool-id={tool.toolId}
                      data-tool-level={tool.level}
                      data-tool-blocked={blocked ? "true" : "false"}
                      data-testid="character-card-tool"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-mono text-xs font-semibold">
                          {tool.displayName?.trim() || tool.toolId}
                        </span>
                        <span className="font-mono text-[10px] text-muted-foreground">
                          Lv {Math.max(1, Math.trunc(tool.level))} · {invocations.toLocaleString()} invocations ·{" "}
                          {Math.round(ratioPercent)}% success
                        </span>
                      </div>
                      <div
                        aria-label={`Tool ${tool.toolId} success ${Math.round(ratioPercent)} percent`}
                        className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted"
                        role="progressbar"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={Math.round(ratioPercent)}
                      >
                        <div
                          className={cn("h-full rounded-full transition-all", visual.barClass)}
                          style={{ width: `${ratioPercent}%` }}
                        />
                      </div>
                      {blocked ? (
                        <p
                          className="mt-1 text-[10px] text-rose-700 dark:text-rose-300"
                          data-testid="character-card-tool-gate-blocked"
                        >
                          Gate: requires Lv {requiredLevel} (refused)
                        </p>
                      ) : null}
                    </li>
                  )
                })}
              </ul>
            </section>
          ) : null}

          {visibleTalents.length > 0 || capstone ? (
            <section
              aria-label="Agent talent tree"
              className="rounded-md border bg-background/50 p-3"
              data-testid="character-card-talents"
            >
              <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium uppercase text-muted-foreground">
                <Crown className="size-3.5 text-fuchsia-500" aria-hidden="true" />
                Talent tree
              </div>
              <ul className="flex flex-col gap-2">
                {visibleTalents.map((milestone) => {
                  const chosen = milestone.chosenTalentId ?? null
                  const locked = Boolean(chosen)
                  const needsPick = Boolean(milestone.choiceRequired) && !locked
                  const chosenOption = chosen
                    ? milestone.options.find((option) => option.talentId === chosen) ?? null
                    : null
                  return (
                    <li
                      key={milestone.milestoneLevel}
                      className="rounded-md border bg-background/40 p-2"
                      data-milestone-level={milestone.milestoneLevel}
                      data-talent-locked={locked ? "true" : "false"}
                      data-talent-id={chosen ?? ""}
                      data-testid="character-card-talent-milestone"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-mono text-xs font-semibold">
                          Lv {milestone.milestoneLevel} Talent
                        </span>
                        {locked ? (
                          <span
                            className="font-mono text-[10px] text-emerald-700 dark:text-emerald-300"
                            data-testid="character-card-talent-locked"
                          >
                            Choice locked
                          </span>
                        ) : needsPick ? (
                          <button
                            type="button"
                            onClick={() => setTalentModalMilestone(milestone.milestoneLevel)}
                            className="inline-flex items-center gap-1 rounded border border-amber-500/45 bg-amber-500/10 px-2 py-0.5 font-mono text-[10px] text-amber-700 hover:bg-amber-500/20 dark:text-amber-300"
                            data-testid="character-card-talent-pending"
                          >
                            <AlertTriangle className="size-3" aria-hidden="true" />
                            Pick talent
                          </button>
                        ) : (
                          <span className="font-mono text-[10px] text-muted-foreground">
                            Lv {milestone.milestoneLevel} milestone
                          </span>
                        )}
                      </div>
                      {locked && chosenOption ? (
                        <p
                          className="mt-1 text-[10px] text-muted-foreground"
                          data-testid="character-card-talent-chosen"
                        >
                          Locked:{" "}
                          <span className="font-mono text-foreground">
                            {chosenOption.displayName}
                          </span>
                        </p>
                      ) : null}
                      <div
                        className="mt-1 flex flex-wrap items-center gap-1.5"
                        data-testid="character-card-talent-options"
                      >
                        {milestone.options.map((option) => {
                          const isChosen = option.talentId === chosen
                          const isAlternate = locked && !isChosen
                          return (
                            <button
                              key={option.talentId}
                              type="button"
                              disabled={locked || !onLockTalent}
                              onClick={() =>
                                onLockTalent?.(milestone.milestoneLevel, option.talentId)
                              }
                              className={cn(
                                "rounded border px-2 py-0.5 font-mono text-[10px]",
                                isChosen
                                  ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                                  : "bg-background hover:bg-muted",
                                isAlternate && "opacity-45",
                                locked && "cursor-not-allowed",
                              )}
                              data-testid="character-card-talent-option"
                              data-talent-option-id={option.talentId}
                              data-talent-option-chosen={isChosen ? "true" : "false"}
                              data-talent-option-alternate={isAlternate ? "true" : "false"}
                              title={option.summary ?? option.displayName}
                            >
                              {option.displayName}
                            </button>
                          )
                        })}
                      </div>
                    </li>
                  )
                })}
              </ul>
              {capstone ? (
                <div
                  className="mt-2 rounded-md border bg-background/40 p-2"
                  data-testid="character-card-capstone"
                  data-capstone-locked={capstone.locked ? "true" : "false"}
                  data-capstone-pickable={capstone.pickable ? "true" : "false"}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="inline-flex items-center gap-1.5 font-mono text-xs font-semibold">
                      <Crown className="size-3 text-fuchsia-500" aria-hidden="true" />
                      Lv 80 Capstone
                    </span>
                    {capstone.locked ? (
                      <span className="font-mono text-[10px] text-emerald-700 dark:text-emerald-300">
                        Locked
                      </span>
                    ) : capstone.pickable ? (
                      <button
                        type="button"
                        disabled={!onLockCapstone}
                        onClick={() => onLockCapstone?.()}
                        className="rounded border bg-background px-2 py-0.5 font-mono text-[10px] hover:bg-muted"
                        data-testid="character-card-capstone-lock"
                      >
                        Lock {capstone.displayName}
                      </button>
                    ) : (
                      <span className="font-mono text-[10px] text-muted-foreground">
                        Requires Lv 80 + final pick
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    <span className="font-mono text-foreground">
                      {capstone.displayName}
                    </span>
                    {capstone.summary ? ` — ${capstone.summary}` : null}
                  </p>
                </div>
              ) : null}
            </section>
          ) : null}

          {visibleBadges.length > 0 ? (
            <section
              aria-label="Achievement badge wall"
              className="rounded-md border bg-background/50 p-3"
              data-testid="character-card-badge-wall"
            >
              <div className="mb-2 flex items-center gap-1.5 text-[10px] font-medium uppercase text-muted-foreground">
                <Trophy className="size-3.5 text-amber-500" aria-hidden="true" />
                Badge wall
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                {visibleBadges.map((badge, index) => {
                  const badgeVisual = BADGE_VISUALS[badge.kind]
                  const BadgeIcon = badgeVisual.Icon
                  const label = badge.label?.trim() || badgeVisual.label
                  const rarity = badge.rarity ?? "bronze"
                  const rarityClass = BADGE_RARITY_CLASS[rarity] ?? BADGE_RARITY_CLASS.bronze
                  const locked = badge.locked === true

                  return (
                    <div
                      key={badge.id ?? `${badge.kind}-${index}`}
                      aria-label={label}
                      className={cn(
                        "relative min-w-0 rounded-md border p-2 shadow-sm",
                        "after:absolute after:right-2 after:top-2 after:size-1.5 after:rounded-full",
                        badgeVisual.toneClass,
                        rarityClass,
                        locked && "opacity-45 grayscale",
                      )}
                      data-badge-kind={badge.kind}
                      data-badge-rarity={rarity}
                      data-badge-locked={locked ? "true" : "false"}
                      data-testid="character-card-badge"
                      title={badgeTitle(badge, badgeVisual.label)}
                    >
                      <div className="flex items-center gap-2">
                        <BadgeIcon className="size-4 shrink-0" aria-hidden="true" />
                        <span className="min-w-0 truncate text-xs font-semibold leading-tight">
                          {label}
                        </span>
                      </div>
                      {badge.progressLabel?.trim() ? (
                        <div className="mt-1 truncate font-mono text-[10px] opacity-75">
                          {badge.progressLabel.trim()}
                        </div>
                      ) : null}
                    </div>
                  )
                })}
              </div>
            </section>
          ) : null}
        </div>
      </div>
      {activeTalentModalMilestone !== null ? (
        <TalentForkModal
          open
          agentId={agentId}
          agentDisplayName={displayName}
          guild={guild}
          agentLevel={level}
          milestoneLevel={activeTalentModalMilestone}
          onLocked={async () => {
            await onTalentLocked?.()
            setTalentModalMilestone(null)
          }}
          onCancel={() => setTalentModalMilestone(null)}
        />
      ) : null}
    </article>
  )
}

export default CharacterCard

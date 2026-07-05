"use client"

/**
 * Sora（そら）— dedicated Guild Leader character sheet.
 *
 * Sora is the Orchestrator: the party leader / 公會會長 who sits ABOVE the
 * worker Guilds and is NOT a DB-backed Character card. This static route takes
 * precedence over the dynamic `[agent_id]` sheet (which would 404 on her), so
 * she gets a bespoke, richer sheet:
 *   - full-body 立繪 + her signature command-panel aura (#1 leader aura)
 *   - a 6-way expression gallery cropped from her design sheet (#2 多表情)
 * Palette is blue-white — deliberately off the worker brain-colour system.
 */

import { useEffect, useMemo, useState } from "react"
import Link from "next/link"
import {
  ArrowLeft,
  ChevronRight,
  Crown,
  Headphones,
  Sparkles,
  BrainCircuit,
  Users,
  Boxes,
  TrendingUp,
  ShieldAlert,
  Radio,
  Route,
  MessageSquarePlus,
  UsersRound,
  Brain,
  Send,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useUiMode } from "@/hooks/use-ui-mode"
import { cn } from "@/lib/utils"
import {
  listAgentCards,
  getOrchestratorCommandStats,
  type AgentCardSummary,
  type OrchestratorCommandStats,
} from "@/lib/api"

// Mirror backend/agents/xp_engine.py: level_threshold(L)=ceil(100·L^1.4), MAX_LEVEL=80.
// Sora's 統帥 Lv = the level the whole org's cumulative XP maps to (always ≥ her top member).
const LEVEL_BASE_XP = 100
const LEVEL_EXP = 1.4
const MAX_LEVEL = 80
function levelThreshold(level: number): number {
  return Math.ceil(LEVEL_BASE_XP * Math.pow(level, LEVEL_EXP))
}
function levelForXp(totalXp: number): number {
  let level = 1
  while (level < MAX_LEVEL && totalXp >= levelThreshold(level + 1)) level += 1
  return level
}

const BRAIN_LABEL: Record<string, string> = {
  claude: "諾亞 / Claude",
  codex: "Codex",
  gemini: "Gemini",
  grok: "Grok",
}

function StatTile({
  icon: Icon,
  label,
  value,
  sub,
}: {
  icon: typeof Users
  label: string
  value: string
  sub?: string
}) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--background)]/40 p-2.5">
      <p className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
        <Icon size={11} /> {label}
      </p>
      <p className="mt-0.5 text-lg font-semibold leading-tight">{value}</p>
      {sub ? <p className="truncate font-mono text-[10px] text-[var(--muted-foreground)]">{sub}</p> : null}
    </div>
  )
}

const EXPRESSIONS = [
  { id: "gentle", label: "溫和微笑", en: "Gentle Smile", note: "平時待人——溫和有耐心。" },
  { id: "focused", label: "專注指揮", en: "Focused Commander", note: "面對專業時——冷靜、清晰、不高冷。" },
  { id: "confident", label: "自信鬼點子", en: "Happy Confident", note: "靈光一閃——鬼才鬼點子，但守規則。" },
  { id: "thinking", label: "沉思", en: "Thinking", note: "拆解問題、盤算路由的當下。" },
  { id: "surprised", label: "驚訝", en: "Surprised", note: "意料之外的輸入。" },
  { id: "worried", label: "關切", en: "Worried", note: "偵測到風險或阻塞時。" },
] as const

type ExpressionId = (typeof EXPRESSIONS)[number]["id"]

const PERSONA_TRAITS = [
  "溫和有耐心",
  "冷靜不高冷",
  "樂於溝通 · 擅長協調",
  "鬼才鬼點子 · 守規則",
  "熱愛工作 · 不輕易放棄",
]

const STATS: Array<{ k: string; v: string }> = [
  { k: "定位", v: "主控 Orchestrator / 隊長" },
  { k: "公會", v: "所有公會之上（協調者）" },
  { k: "大腦", v: "Claude（prod anthropic）" },
  { k: "識別色", v: "天藍 · 純白" },
  { k: "年齡 / 身高", v: "14 · 150cm" },
  { k: "獸耳", v: "白貓耳 + 白蓬鬆貓尾" },
]

export default function SoraCharacterSheetPage() {
  const auth = useAuth()
  const { immersive } = useUiMode()
  const [mood, setMood] = useState<ExpressionId>("gentle")

  // 統帥 data: the roster she commands (Phase 1, derived) + real delivery
  // track-record (Phase 2, backend). Both fail-open — the sheet never breaks
  // if the API 401s / errors, it just hides the numbers.
  const [cards, setCards] = useState<AgentCardSummary[] | null>(null)
  const [stats, setStats] = useState<OrchestratorCommandStats | null>(null)

  useEffect(() => {
    if (auth.loading) return
    if (!auth.user && auth.authMode !== "open") return
    let alive = true
    void listAgentCards().then((c) => alive && setCards(c)).catch(() => {})
    void getOrchestratorCommandStats().then((s) => alive && setStats(s)).catch(() => {})
    return () => {
      alive = false
    }
  }, [auth.loading, auth.user, auth.authMode])

  const command = useMemo(() => {
    if (!cards || cards.length === 0) return null
    const totalXp = cards.reduce((s, c) => s + (c.xp ?? 0), 0)
    const guilds = new Set(cards.map((c) => c.guild).filter(Boolean))
    const top = cards.reduce((a, b) => ((b.level ?? 0) > (a.level ?? 0) ? b : a))
    const active = cards.filter((c) => (c.status ?? "").toLowerCase() === "active").length
    const commanderLevel = levelForXp(totalXp)
    const cur = levelThreshold(commanderLevel)
    const next = commanderLevel >= MAX_LEVEL ? cur : levelThreshold(commanderLevel + 1)
    const pct = next > cur ? Math.min(100, Math.round(((totalXp - cur) / (next - cur)) * 100)) : 100
    return {
      totalXp,
      commanderLevel,
      guildCount: guilds.size,
      memberCount: cards.length,
      top,
      active,
      pct,
      toNext: Math.max(0, next - totalXp),
    }
  }, [cards])

  // Coordination kit — Sora's REAL orchestrator functions (not guild skills).
  // Proficiency scales with the org she coordinates where that's meaningful;
  // the conversational/memory abilities are core (mastered) capabilities.
  const kit = useMemo(() => {
    const members = command?.memberCount ?? 0
    const guilds = command?.guildCount ?? 0
    const dots = (n: number) => Math.max(1, Math.min(5, n))
    return [
      { icon: Send, label: "任務調度", en: "Dispatch", level: dots(Math.ceil(members / 2)), note: `統領 ${members} 名角色` },
      { icon: Route, label: "能力匹配路由", en: "Capability Routing", level: dots(guilds), note: `覆蓋 ${guilds} 個公會` },
      { icon: MessageSquarePlus, label: "對話理解建單", en: "Conversational Filing", level: 5, note: "create_task · 核心能力" },
      { icon: UsersRound, label: "隊伍編成", en: "Party Assembly", level: 4, note: "synergy 編隊" },
      { icon: Brain, label: "對話記憶 / 上下文", en: "Chat Memory", level: 5, note: "per-session 記憶 · 核心" },
    ]
  }, [command])

  const current = EXPRESSIONS.find((e) => e.id === mood) ?? EXPRESSIONS[0]

  // Keep behaviour consistent with the dynamic sheet: gate on session unless open mode.
  if (auth.loading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
        <div className="font-mono text-xs text-[var(--muted-foreground)]">Verifying operator session…</div>
      </main>
    )
  }

  return (
    <main
      className="min-h-screen bg-[var(--background)] text-[var(--foreground)] p-6 md:p-10"
      data-testid="sora-character-sheet"
      data-agent-id="sora"
    >
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center gap-2 text-[10px] font-mono text-[var(--muted-foreground)] mb-4">
          <Link href="/agents" className="hover:text-[var(--foreground)] inline-flex items-center gap-1">
            <ArrowLeft size={10} /> roster
          </Link>
          <ChevronRight size={10} />
          <span className="text-[var(--neural-blue)]">Sora · 會長</span>
        </div>

        {/* Hero: 立繪 + command-panel aura + identity */}
        <section
          className={cn(
            "relative overflow-hidden rounded-xl border border-[var(--neural-blue)]/40",
            "bg-gradient-to-br from-[var(--neural-blue)]/12 via-[var(--card)] to-[var(--card)] mb-6",
          )}
        >
          <div className="absolute right-0 top-0 z-10 flex items-center gap-1 rounded-bl-lg border-b border-l border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/15 px-2.5 py-1 font-mono text-[10px] uppercase tracking-widest text-[var(--neural-blue)]">
            <Crown size={11} /> Guild Leader
          </div>

          <div className="flex flex-col gap-4 p-5 md:flex-row md:gap-6 md:p-7">
            {/* 立繪 + aura */}
            <div className="relative flex shrink-0 items-end justify-center">
              {/* command-panel aura — her signature orchestration motif */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/full/sora-panel.png"
                alt=""
                aria-hidden="true"
                className="pointer-events-none absolute -left-2 top-2 w-40 rotate-[-6deg] opacity-30 blur-[0.5px] mix-blend-screen md:w-52"
              />
              <div className="pointer-events-none absolute inset-x-0 bottom-0 h-2/3 rounded-full bg-[var(--neural-blue)]/20 blur-2xl" />
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/full/sora.png"
                alt="Sora — full body"
                className={cn(
                  "relative w-auto object-contain drop-shadow-[0_10px_28px_rgba(56,189,248,0.28)]",
                  immersive ? "h-72 md:h-96" : "h-56 md:h-64",
                )}
              />
            </div>

            {/* identity */}
            <div className="flex min-w-0 flex-1 flex-col justify-center gap-3">
              <div>
                <div className="flex items-baseline gap-2">
                  <h1 className="text-3xl font-semibold tracking-tight md:text-4xl">Sora</h1>
                  <span className="font-mono text-base text-[var(--muted-foreground)]">そら</span>
                </div>
                <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-xs uppercase tracking-wider text-[var(--neural-blue)]">
                  <span className="inline-flex items-center gap-1">
                    <Headphones size={12} /> 公會會長 · Orchestrator
                  </span>
                  <span className="inline-flex items-center gap-1 text-[var(--muted-foreground)]">
                    <BrainCircuit size={12} /> brain Claude
                  </span>
                </p>
              </div>

              <p className="max-w-xl text-sm leading-relaxed text-[var(--muted-foreground)]">
                戰隊隊長／主控。站在所有公會之上，負責調度、路由與拆解問題——你在 Orchestrator
                面板對話的就是我。異於常人的頂尖能力、鬼點子多，但始終守規則。
              </p>

              <div className="flex flex-wrap gap-1.5">
                {PERSONA_TRAITS.map((t) => (
                  <span
                    key={t}
                    className="inline-flex items-center gap-1 rounded-full border border-[var(--neural-blue)]/30 bg-[var(--neural-blue)]/10 px-2 py-0.5 font-mono text-[10px] text-[var(--neural-blue)]"
                  >
                    <Sparkles size={9} /> {t}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* 統帥面板 — Sora's level/skills come from the org she commands (Phase 1,
            live-derived) + her real delivery track-record (Phase 2, backend). */}
        <section
          className="mb-6 rounded-xl border border-[var(--neural-blue)]/30 bg-[var(--card)] p-5"
          data-testid="sora-command-panel"
        >
          <h2 className="mb-4 flex items-center gap-1.5 text-sm font-semibold">
            <Crown size={14} className="text-[var(--neural-blue)]" /> 統帥面板
          </h2>

          <div className="grid gap-5 md:grid-cols-2">
            {/* Left: derived commander stats */}
            <div>
              <div className="flex items-end gap-3">
                <div className="flex size-16 shrink-0 flex-col items-center justify-center rounded-xl border border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/10">
                  <span className="font-mono text-[9px] uppercase tracking-wider text-[var(--muted-foreground)]">統帥</span>
                  <span className="text-2xl font-bold leading-none text-[var(--neural-blue)]">
                    {command ? command.commanderLevel : "—"}
                  </span>
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-xs text-[var(--muted-foreground)]">
                    等級 ＝ 整支戰隊累積交付經驗的總和換算（永遠 ≥ 最資深部屬）。
                  </p>
                  {command && (
                    <div className="mt-1.5">
                      <div className="h-1.5 overflow-hidden rounded-full bg-[var(--secondary)]">
                        <div
                          className="h-full rounded-full bg-[var(--neural-blue)]"
                          style={{ width: `${command.pct}%` }}
                        />
                      </div>
                      <p className="mt-1 font-mono text-[10px] text-[var(--muted-foreground)]">
                        全隊累計 {command.totalXp.toLocaleString()} XP
                        {command.commanderLevel < MAX_LEVEL && ` · 距下一級 ${command.toNext.toLocaleString()}`}
                      </p>
                    </div>
                  )}
                </div>
              </div>

              <dl className="mt-4 grid grid-cols-2 gap-2">
                <StatTile icon={Users} label="統領規模" value={command ? `${command.memberCount} 名` : "—"} sub={command ? `${command.guildCount} 個公會` : ""} />
                <StatTile icon={TrendingUp} label="最資深部屬" value={command ? `Lv${command.top.level}` : "—"} sub={command ? command.top.agent_id : ""} />
                <StatTile icon={Radio} label="現役 / 待命" value={command ? `${command.active} / ${command.memberCount - command.active}` : "—"} sub="協調中" />
                <StatTile icon={Boxes} label="全隊累計交付" value={command ? command.totalXp.toLocaleString() : "—"} sub="XP" />
              </dl>

              {/* coordination kit */}
              <p className="mb-2 mt-5 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                協調技能組
              </p>
              <ul className="space-y-1.5">
                {kit.map((s) => (
                  <li key={s.en} className="flex items-center gap-2">
                    <s.icon size={13} className="shrink-0 text-[var(--neural-blue)]" />
                    <span className="w-28 shrink-0 truncate text-xs">{s.label}</span>
                    <span className="flex gap-0.5">
                      {Array.from({ length: 5 }).map((_, i) => (
                        <span
                          key={i}
                          className={cn(
                            "h-1.5 w-1.5 rounded-full",
                            i < s.level ? "bg-[var(--neural-blue)]" : "bg-[var(--secondary)]",
                          )}
                        />
                      ))}
                    </span>
                    <span className="ml-auto truncate font-mono text-[10px] text-[var(--muted-foreground)]">{s.note}</span>
                  </li>
                ))}
              </ul>
            </div>

            {/* Right: real delivery track-record (Phase 2, backend) */}
            <div className="rounded-lg border border-[var(--border)] bg-[var(--background)]/40 p-4">
              <p className="mb-3 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-[var(--neural-blue)]">
                <Send size={11} /> 統帥交付戰功
                <span className="ml-auto normal-case text-[var(--muted-foreground)]">真實 runner 數據</span>
              </p>

              {stats ? (
                <>
                  <div className="flex items-baseline gap-2">
                    <span className="text-3xl font-bold text-[var(--foreground)]">{stats.delivered_total}</span>
                    <span className="text-xs text-[var(--muted-foreground)]">筆成功交付</span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {stats.by_brain.map((b) => (
                      <span
                        key={b.brain}
                        className="inline-flex items-center gap-1 rounded-full border border-[var(--border)] px-2 py-0.5 font-mono text-[10px] text-[var(--muted-foreground)]"
                      >
                        {BRAIN_LABEL[b.brain] ?? b.brain} · {b.count}
                      </span>
                    ))}
                  </div>

                  <dl className="mt-4 space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <dt className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                        <ShieldAlert size={11} /> 近 30 天事故
                      </dt>
                      <dd className="text-sm">{stats.incidents_30d.toLocaleString()}</dd>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <dt className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">平均交付耗時</dt>
                      <dd className="text-sm">
                        {stats.avg_seconds != null ? `${Math.round(stats.avg_seconds / 60)} 分` : "—"}
                      </dd>
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <dt className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">最近交付</dt>
                      <dd className="truncate text-sm">
                        {stats.latest?.ticket_key ? (
                          <span className="text-[var(--neural-blue)]">{stats.latest.ticket_key} ✓</span>
                        ) : (
                          "—"
                        )}
                      </dd>
                    </div>
                  </dl>

                  <p className="mt-3 border-t border-[var(--border)] pt-2 font-mono text-[9px] leading-relaxed text-[var(--muted-foreground)]">
                    交付＝runner 成功完成的 run；事故為獨立事件流（含基建雜訊），故不併成單一成功率——只呈現實數。
                  </p>
                </>
              ) : (
                <p className="font-mono text-[10px] text-[var(--muted-foreground)]">交付數據載入中／不可用。</p>
              )}
            </div>
          </div>
        </section>

        <div className="grid gap-6 md:grid-cols-[1.1fr_1fr]">
          {/* Expression gallery (#2 多表情) */}
          <section
            className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-5"
            data-testid="sora-expression-gallery"
          >
            <h2 className="mb-1 text-sm font-semibold">表情立繪</h2>
            <p className="mb-4 text-xs text-[var(--muted-foreground)]">點選切換 Sora 的神情。</p>

            <div className="flex flex-col items-center gap-3">
              {/* current expression, large */}
              <div className="relative">
                <div className="pointer-events-none absolute inset-0 rounded-full bg-[var(--neural-blue)]/15 blur-xl" />
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  key={current.id}
                  src={`/expressions/sora/${current.id}.png`}
                  alt={`Sora — ${current.en}`}
                  className="relative h-40 w-40 rounded-full border border-[var(--neural-blue)]/40 object-cover"
                />
              </div>
              <div className="text-center">
                <p className="font-semibold text-[var(--neural-blue)]">{current.label}</p>
                <p className="font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                  {current.en}
                </p>
                <p className="mt-1 text-xs text-[var(--muted-foreground)]">{current.note}</p>
              </div>
            </div>

            {/* thumbnails */}
            <div className="mt-4 grid grid-cols-6 gap-1.5">
              {EXPRESSIONS.map((e) => (
                <button
                  key={e.id}
                  type="button"
                  onClick={() => setMood(e.id)}
                  title={`${e.label} · ${e.en}`}
                  aria-pressed={e.id === mood}
                  className={cn(
                    "aspect-square overflow-hidden rounded-md border transition-all",
                    e.id === mood
                      ? "border-[var(--neural-blue)] ring-1 ring-[var(--neural-blue)]/40 scale-105"
                      : "border-[var(--border)] opacity-70 hover:opacity-100 hover:border-[var(--neural-blue)]/50",
                  )}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={`/expressions/sora/${e.id}.png`}
                    alt={e.en}
                    className="h-full w-full object-cover"
                  />
                </button>
              ))}
            </div>
          </section>

          {/* Stats / dossier */}
          <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] p-5">
            <h2 className="mb-3 text-sm font-semibold">角色檔案</h2>
            <dl className="divide-y divide-[var(--border)]">
              {STATS.map((row) => (
                <div key={row.k} className="flex items-baseline justify-between gap-3 py-2">
                  <dt className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
                    {row.k}
                  </dt>
                  <dd className="text-right text-sm">{row.v}</dd>
                </div>
              ))}
            </dl>
            <div className="mt-4 rounded-lg border border-[var(--neural-blue)]/25 bg-[var(--neural-blue)]/5 p-3">
              <p className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-[var(--neural-blue)]">
                <Crown size={11} /> 會長職責
              </p>
              <p className="mt-1.5 text-xs leading-relaxed text-[var(--muted-foreground)]">
                調度各公會、路由任務、拆解問題並在對話中與 operator 協作。守 L1／safety
                規範，不越權——會長的 +2 不取代人類審核。
              </p>
            </div>
          </section>
        </div>
      </div>
    </main>
  )
}

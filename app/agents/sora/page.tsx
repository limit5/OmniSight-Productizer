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

import { useState } from "react"
import Link from "next/link"
import { ArrowLeft, ChevronRight, Crown, Headphones, Sparkles, BrainCircuit } from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useUiMode } from "@/hooks/use-ui-mode"
import { cn } from "@/lib/utils"

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

"use client"

/**
 * Guild Leader hero — Sora（そら）, the Orchestrator.
 *
 * Sora is NOT a worker Character in a Guild; she is the party leader / guild
 * master who sits ABOVE the Guilds and is the face you talk to in the
 * Orchestrator panel. The Guild Hall therefore opens with her hero banner,
 * visually distinct from (and above) the guild roster.
 *
 * Dial-aware (RPG-UI): Immersive shows her full-body 立繪; Focus collapses to a
 * compact leader row. Her palette is blue-white — deliberately off the worker
 * brain-colour system (amber/green/indigo/pink) to mark her as the coordinator.
 */

import { Crown, Headphones, Sparkles } from "lucide-react"
import type { ReactElement } from "react"

import { cn } from "@/lib/utils"

// Sora's identity is fixed (the orchestrator persona, docs/design/rpg/characters/sora.md).
const SORA = {
  slug: "sora",
  displayName: "Sora",
  jpName: "そら",
  title: "公會會長 · Orchestrator",
  brain: "Claude",
  blurb:
    "戰隊隊長／主控。站在所有公會之上，負責調度、路由與拆解問題——你在 Orchestrator 面板對話的就是我。",
  persona: ["溫和有耐心", "冷靜不高冷", "鬼才鬼點子 · 守規則", "熱愛工作 · 不輕易放棄"],
  fullbody: "/full/sora.png",
  avatar: "/avatars/sora.png",
} as const

export interface GuildLeaderHeroProps {
  immersive?: boolean
  className?: string
}

export function GuildLeaderHero({ immersive = false, className }: GuildLeaderHeroProps): ReactElement {
  return (
    <section
      data-testid="guild-leader-hero"
      className={cn(
        "relative overflow-hidden rounded-xl border border-[var(--neural-blue)]/40",
        "bg-gradient-to-br from-[var(--neural-blue)]/12 via-[var(--card)] to-[var(--card)]",
        className,
      )}
    >
      {/* leader ribbon */}
      <div className="absolute right-0 top-0 z-10 flex items-center gap-1 rounded-bl-lg border-b border-l border-[var(--neural-blue)]/40 bg-[var(--neural-blue)]/15 px-2.5 py-1 font-mono text-[10px] uppercase tracking-widest text-[var(--neural-blue)]">
        <Crown size={11} />
        Guild Leader
      </div>

      {immersive ? (
        <div className="flex items-stretch gap-4 p-4 md:gap-6 md:p-6">
          {/* full-body 立繪 */}
          <div className="group relative shrink-0 self-end">
            <div className="pointer-events-none absolute inset-x-0 bottom-0 h-3/4 rounded-full bg-[var(--neural-blue)]/20 blur-2xl" />
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={SORA.fullbody}
              alt="Sora — Guild Leader"
              className="relative h-56 w-auto object-contain drop-shadow-[0_8px_24px_rgba(56,189,248,0.25)] transition-transform duration-300 group-hover:scale-[1.03] md:h-72"
            />
          </div>

          {/* identity */}
          <div className="flex min-w-0 flex-col justify-center gap-3 py-2">
            <div>
              <div className="flex items-baseline gap-2">
                <h2 className="text-2xl font-semibold tracking-tight text-[var(--foreground)] md:text-3xl">
                  {SORA.displayName}
                </h2>
                <span className="font-mono text-sm text-[var(--muted-foreground)]">{SORA.jpName}</span>
              </div>
              <p className="mt-0.5 flex items-center gap-1.5 font-mono text-xs uppercase tracking-wider text-[var(--neural-blue)]">
                <Headphones size={12} />
                {SORA.title}
                <span className="text-[var(--muted-foreground)]">· brain {SORA.brain}</span>
              </p>
            </div>

            <p className="max-w-xl text-sm leading-relaxed text-[var(--muted-foreground)]">{SORA.blurb}</p>

            <div className="flex flex-wrap gap-1.5">
              {SORA.persona.map((trait) => (
                <span
                  key={trait}
                  className="inline-flex items-center gap-1 rounded-full border border-[var(--neural-blue)]/30 bg-[var(--neural-blue)]/10 px-2 py-0.5 font-mono text-[10px] text-[var(--neural-blue)]"
                >
                  <Sparkles size={9} />
                  {trait}
                </span>
              ))}
            </div>
          </div>
        </div>
      ) : (
        // Focus: compact leader row
        <div className="flex items-center gap-3 p-3">
          <div className="shrink-0">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={SORA.avatar}
              alt="Sora"
              className="size-11 rounded-full border border-[var(--neural-blue)]/50 object-cover ring-1 ring-[var(--neural-blue)]/20"
            />
          </div>
          <div className="min-w-0">
            <div className="flex items-baseline gap-1.5">
              <span className="truncate font-semibold text-[var(--foreground)]">{SORA.displayName}</span>
              <span className="font-mono text-[10px] text-[var(--muted-foreground)]">{SORA.jpName}</span>
            </div>
            <p className="truncate font-mono text-[10px] uppercase tracking-wider text-[var(--neural-blue)]">
              {SORA.title} · brain {SORA.brain}
            </p>
          </div>
        </div>
      )}
    </section>
  )
}

export default GuildLeaderHero

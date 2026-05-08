"use client"

import { cn } from "@/lib/utils"
import type { ProviderConstellationProvider } from "./ProviderConstellation"

export type QuotaPulsePosition =
  | "top-right"
  | "bottom-right"
  | "top-left"
  | "bottom-left"

export interface QuotaPulseProps {
  provider: ProviderConstellationProvider
  position: QuotaPulsePosition
}

const POSITION_CLASS: Record<QuotaPulsePosition, string> = {
  "top-right": "-right-0.5 top-2",
  "bottom-right": "-right-0.5 bottom-2",
  "top-left": "-left-0.5 top-2",
  "bottom-left": "-left-0.5 bottom-2",
}

const QUOTA_COLOR_CLASS: Record<
  ProviderConstellationProvider["quotaState"],
  string
> = {
  healthy: "bg-emerald-300 shadow-[0_0_12px_rgba(110,231,183,0.9)]",
  watch: "bg-amber-300 shadow-[0_0_12px_rgba(252,211,77,0.82)]",
  critical: "bg-rose-300 shadow-[0_0_12px_rgba(253,164,175,0.82)]",
  unavailable: "bg-slate-400 shadow-[0_0_8px_rgba(148,163,184,0.5)]",
}

function pulseClass(activityLevel: number | undefined): string {
  const level = activityLevel ?? 0
  if (!Number.isFinite(level)) return "pulse-off"
  if (level >= 0.7) return "pulse-fast"
  if (level >= 0.3) return "pulse-slow"
  return "pulse-off"
}

export function QuotaPulse({ provider, position }: QuotaPulseProps) {
  const animationClass = pulseClass(provider.activityLevel)
  const percent = Math.round(Math.max(0, (provider.activityLevel ?? 0) * 100))

  return (
    <span
      className={cn(
        "quota-pulse pointer-events-none absolute z-30 size-3 rounded-full border border-white/70",
        POSITION_CLASS[position],
        QUOTA_COLOR_CLASS[provider.quotaState],
        animationClass,
      )}
      data-testid={`mp-quota-pulse-${provider.id}`}
      data-mp-quota-pulse={animationClass}
      aria-label={`${provider.name} live connectivity ${percent}%`}
      role="status"
    >
      <span className="quota-pulse__ring" aria-hidden="true" />
      <style jsx>{`
        .quota-pulse { --mp-quota-pulse-duration: 0s; }
        .quota-pulse__ring { position: absolute; inset: -5px; border: 1px solid currentColor; border-radius: 9999px; opacity: 0; }
        .pulse-fast { --mp-quota-pulse-duration: 900ms; }
        .pulse-slow { --mp-quota-pulse-duration: 1800ms; }
        .pulse-off { opacity: 0.58; }
        .pulse-fast .quota-pulse__ring, .pulse-slow .quota-pulse__ring { animation: mp-quota-pulse var(--mp-quota-pulse-duration) ease-out infinite; }
        .pulse-off .quota-pulse__ring { animation: none; }
        @keyframes mp-quota-pulse { from { transform: scale(0.5); opacity: 0.58; } to { transform: scale(1.5); opacity: 0; } }
        @media (prefers-reduced-motion: reduce) {
          .pulse-fast .quota-pulse__ring, .pulse-slow .quota-pulse__ring { animation: none; }
        }
      `}</style>
    </span>
  )
}

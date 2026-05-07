"use client"

/**
 * MP.W4.2 — provider energy sphere with quota visualization.
 *
 * Mirrors the AS.7.1 OAuthEnergySphere host contract (round provider
 * button, brand fill, halo, icon slot) and adds the Provider
 * Constellation quota layer:
 *   - size reflects remaining quota headroom,
 *   - ring colour reflects the provider status tier,
 *   - warning/critical tiers pulse when the auth motion budget allows.
 *
 * This component is intentionally standalone for MP.W4.2. The
 * constellation shell, beams, tradeoff slider, and drawer wiring are
 * separate MP.W4 tickets.
 */

import type { CSSProperties, ReactNode } from "react"

import { getAuthVisualBudget } from "@/lib/auth-visual/motion-policy"
import type { MotionLevel } from "@/lib/motion-preferences"
import type { OAuthProviderInfo } from "@/lib/auth/oauth-providers"
import {
  computeProviderStatus,
  describeProviderStatus,
  type ProviderStatusBadgeProps,
  type ProviderStatusResult,
  type ProviderStatusTier,
} from "@/components/omnisight/provider-status-badge"

export type ProviderEnergySphereSize = "primary" | "secondary"
export type ProviderEnergySpherePulse = "off" | "warning" | "critical"

export type ProviderEnergySphereQuota = Pick<
  ProviderStatusBadgeProps,
  | "status"
  | "reason"
  | "balanceRemaining"
  | "grantedTotal"
  | "currency"
  | "rateLimitRemainingRequests"
  | "rateLimitRemainingTokens"
  | "rateLimitLimitRequests"
  | "rateLimitLimitTokens"
  | "resetAtTs"
  | "loading"
>

export interface ProviderEnergySphereViz {
  tier: ProviderStatusTier
  diameterPx: number
  quotaRemainingPct: number | null
  ringColor: string
  pulse: ProviderEnergySpherePulse
  label: string
}

export interface ProviderEnergySphereProps {
  level: MotionLevel
  provider: OAuthProviderInfo
  icon: ReactNode
  quota?: ProviderEnergySphereQuota
  size?: ProviderEnergySphereSize
  href?: string
  onSelect?: () => void
  disabled?: boolean
  selected?: boolean
  className?: string
}

const BASE_DIAMETER_PX: Record<ProviderEnergySphereSize, number> = {
  primary: 64,
  secondary: 48,
}

const TIER_RING_COLOR: Record<ProviderStatusTier, string> = {
  green: "var(--validation-emerald)",
  yellow: "#eab308",
  red: "var(--critical-red)",
  gray: "var(--muted-foreground)",
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

function quotaRemainingPct(result: ProviderStatusResult): number | null {
  const signals = [result.balancePct, result.rateLimitRemainingPct].filter(
    (v): v is number => v !== null && Number.isFinite(v),
  )
  if (signals.length === 0) return null
  return Math.min(...signals)
}

function diameterForQuota(
  size: ProviderEnergySphereSize,
  tier: ProviderStatusTier,
  remainingPct: number | null,
): number {
  const base = BASE_DIAMETER_PX[size]
  if (tier === "red") return base - 8
  if (tier === "yellow") return base - 4
  if (tier === "gray" || remainingPct === null) return base
  return base + clamp((remainingPct - 50) / 50, 0, 1) * 8
}

function pulseForTier(tier: ProviderStatusTier): ProviderEnergySpherePulse {
  if (tier === "red") return "critical"
  if (tier === "yellow") return "warning"
  return "off"
}

export function computeProviderEnergySphereViz({
  provider,
  quota,
  size = "primary",
}: {
  provider: OAuthProviderInfo
  quota?: ProviderEnergySphereQuota
  size?: ProviderEnergySphereSize
}): ProviderEnergySphereViz {
  const statusProps: ProviderStatusBadgeProps = {
    provider: provider.displayName,
    ...quota,
  }
  const result = computeProviderStatus(statusProps)
  const remainingPct = quotaRemainingPct(result)
  return {
    tier: result.tier,
    diameterPx: diameterForQuota(size, result.tier, remainingPct),
    quotaRemainingPct: remainingPct,
    ringColor: TIER_RING_COLOR[result.tier],
    pulse: pulseForTier(result.tier),
    label: describeProviderStatus(statusProps, result),
  }
}

export function ProviderEnergySphere({
  level,
  provider,
  icon,
  quota,
  size = "primary",
  href,
  onSelect,
  disabled = false,
  selected = false,
  className = "",
}: ProviderEnergySphereProps) {
  const budget = getAuthVisualBudget(level)
  const viz = computeProviderEnergySphereViz({ provider, quota, size })
  const pulse =
    !disabled && budget.glowFlicker ? viz.pulse : ("off" as const)
  const actionable = !disabled && (Boolean(href) || Boolean(onSelect))
  const label = actionable
    ? `Select ${provider.displayName}. ${viz.label}`
    : `${provider.displayName}. ${viz.label}`
  const style = {
    "--mp-provider-brand": provider.brandColor,
    "--mp-provider-ring": viz.ringColor,
    width: `${viz.diameterPx}px`,
    height: `${viz.diameterPx}px`,
    background:
      "radial-gradient(circle at 32% 28%, rgba(255,255,255,0.36), transparent 30%), var(--mp-provider-brand)",
    boxShadow: selected
      ? `0 0 0 2px var(--background), 0 0 0 4px ${viz.ringColor}, 0 0 28px color-mix(in srgb, ${viz.ringColor} 55%, transparent)`
      : `0 0 0 1px color-mix(in srgb, ${viz.ringColor} 58%, transparent), 0 0 22px color-mix(in srgb, ${viz.ringColor} 36%, transparent)`,
  } as CSSProperties
  const rootClass =
    "relative inline-flex shrink-0 items-center justify-center overflow-visible rounded-full transition-[width,height,box-shadow,transform,opacity] duration-200 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--artifact-purple)] focus-visible:ring-offset-2 focus-visible:ring-offset-background " +
    (actionable ? "hover:scale-105 " : "") +
    (disabled ? "opacity-45 cursor-not-allowed " : "") +
    className

  const content = (
    <>
      <span
        aria-hidden="true"
        data-testid={`mp-provider-sphere-${provider.id}-quota-ring`}
        className={
          "absolute -inset-1 rounded-full border-2 border-[var(--mp-provider-ring)] " +
          (pulse !== "off" ? "animate-pulse" : "")
        }
        style={{
          opacity: viz.tier === "gray" ? 0.55 : 0.9,
          boxShadow: `0 0 18px color-mix(in srgb, ${viz.ringColor} 46%, transparent)`,
        }}
      />
      <span
        aria-hidden="true"
        className="absolute inset-[18%] rounded-full bg-black/18"
      />
      <span
        className="relative z-10 flex h-[58%] w-[58%] items-center justify-center [&_svg]:h-full [&_svg]:w-full"
        aria-hidden="true"
      >
        {icon}
      </span>
    </>
  )

  if (disabled || (!href && !onSelect)) {
    return (
      <span
        data-testid={`mp-provider-sphere-${provider.id}`}
        data-mp-provider-tier={size}
        data-mp-quota-tier={viz.tier}
        data-mp-quota-pulse={pulse}
        data-mp-selected={selected ? "true" : "false"}
        aria-disabled={disabled ? "true" : undefined}
        aria-label={label}
        title={label}
        className={rootClass}
        style={style}
      >
        {content}
      </span>
    )
  }

  if (href) {
    return (
      <a
        data-testid={`mp-provider-sphere-${provider.id}`}
        data-mp-provider-tier={size}
        data-mp-quota-tier={viz.tier}
        data-mp-quota-pulse={pulse}
        data-mp-selected={selected ? "true" : "false"}
        href={href}
        aria-label={label}
        title={label}
        className={rootClass}
        style={style}
      >
        {content}
      </a>
    )
  }

  return (
    <button
      type="button"
      data-testid={`mp-provider-sphere-${provider.id}`}
      data-mp-provider-tier={size}
      data-mp-quota-tier={viz.tier}
      data-mp-quota-pulse={pulse}
      data-mp-selected={selected ? "true" : "false"}
      onClick={onSelect}
      aria-label={label}
      title={label}
      className={rootClass}
      style={style}
    >
      {content}
    </button>
  )
}

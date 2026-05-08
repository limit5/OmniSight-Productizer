"use client"

/** OP-40 / MP.W4.4 - animated provider-to-core connection beam. */

import type { CSSProperties } from "react"

import type { ProviderConstellationProvider } from "./ProviderConstellation"

const SLOT_PATH: Record<ProviderConstellationProvider["slot"], string> = {
  "top-left": "M 4 16 C 28 18, 54 34, 96 84",
  "top-right": "M 96 16 C 72 18, 46 34, 4 84",
  "middle-right": "M 96 50 C 70 50, 34 50, 4 50",
  "bottom-left": "M 4 84 C 28 82, 54 66, 96 16",
  "bottom-right": "M 96 84 C 72 82, 46 66, 4 16",
}

function clampActivity(value: number | undefined): number {
  if (value === undefined || !Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

function beamStyle(activity: number): CSSProperties {
  const intensity = 0.28 + activity * 0.72

  return {
    opacity: intensity,
    filter: `drop-shadow(0 0 ${6 + activity * 12}px rgba(103,232,249,${0.3 + activity * 0.45}))`,
    "--mp-beam-duration": `${1.8 - activity * 0.8}s`,
  } as CSSProperties
}

export function ConnectionBeam({
  provider,
}: {
  provider: ProviderConstellationProvider
}) {
  const activity = clampActivity(provider.activityLevel)
  const strokeWidth = 1.6 + activity * 2.4

  return (
    <svg
      aria-hidden="true"
      className="pointer-events-none h-full w-full overflow-visible"
      data-testid="connection-beam"
      data-mp-provider={provider.id}
      data-mp-provider-slot={provider.slot}
      data-mp-activity-level={activity.toFixed(2)}
      style={beamStyle(activity)}
      viewBox="0 0 100 100"
    >
      <defs>
        <linearGradient
          id={`mp-connection-beam-${provider.id}`}
          x1="0%"
          x2="100%"
          y1="0%"
          y2="0%"
        >
          <stop offset="0%" stopColor="rgba(103,232,249,0.08)" />
          <stop offset="52%" stopColor="rgba(103,232,249,0.72)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0.95)" />
        </linearGradient>
      </defs>
      <path
        d={SLOT_PATH[provider.slot]}
        fill="none"
        pathLength={100}
        stroke={`url(#mp-connection-beam-${provider.id})`}
        strokeDasharray="14 16"
        strokeLinecap="round"
        strokeWidth={strokeWidth}
      >
        <animate
          attributeName="stroke-dashoffset"
          dur="var(--mp-beam-duration)"
          from="30"
          repeatCount="indefinite"
          to="0"
        />
      </path>
    </svg>
  )
}

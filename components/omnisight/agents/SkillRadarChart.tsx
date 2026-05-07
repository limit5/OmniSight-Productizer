"use client"

import {
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts"

import { cn } from "@/lib/utils"

export interface SkillRadarAxis {
  /** Canonical skill_id from the Guild skill matrix. */
  id: string
  label: string
  level: number
  maxLevel?: number
  xp?: number
}

export interface SkillRadarChartProps {
  axes: SkillRadarAxis[]
  guildName?: string
  maxLevel?: number
  className?: string
}

interface SkillRadarDatum {
  id: string
  label: string
  level: number
  maxLevel: number
  xp?: number
}

const DEFAULT_MAX_LEVEL = 5

function clampLevel(level: number, maxLevel: number): number {
  if (!Number.isFinite(level)) return 0
  return Math.min(Math.max(level, 0), maxLevel)
}

function formatLevel(level: number, maxLevel: number): string {
  return `${level.toFixed(level % 1 === 0 ? 0 : 1)} / ${maxLevel}`
}

function toRadarData(axes: SkillRadarAxis[], fallbackMaxLevel: number): SkillRadarDatum[] {
  return axes.map((axis) => {
    const maxLevel = axis.maxLevel ?? fallbackMaxLevel

    return {
      id: axis.id,
      label: axis.label,
      level: clampLevel(axis.level, maxLevel),
      maxLevel,
      xp: axis.xp,
    }
  })
}

function SkillRadarTooltip({
  active,
  payload,
}: {
  active?: boolean
  payload?: Array<{ payload?: SkillRadarDatum }>
}) {
  const datum = payload?.[0]?.payload

  if (!active || !datum) return null

  return (
    <div className="rounded-md border border-border/70 bg-background px-3 py-2 text-xs shadow-lg">
      <div className="font-medium text-foreground">{datum.label}</div>
      <div className="mt-1 text-muted-foreground">
        {formatLevel(datum.level, datum.maxLevel)}
        {typeof datum.xp === "number" ? ` / ${datum.xp.toLocaleString()} XP` : null}
      </div>
    </div>
  )
}

export function SkillRadarChart({
  axes,
  guildName,
  maxLevel = DEFAULT_MAX_LEVEL,
  className,
}: SkillRadarChartProps) {
  const data = toRadarData(axes, maxLevel)
  const domainMax = Math.max(maxLevel, ...data.map((axis) => axis.maxLevel))
  const title = guildName ? `${guildName} skill radar` : "Skill radar"

  if (data.length === 0) {
    return (
      <section
        aria-label={title}
        className={cn(
          "flex min-h-56 items-center justify-center rounded-md border border-dashed border-border/70 bg-muted/20 px-4 text-sm text-muted-foreground",
          className,
        )}
      >
        No skill axes yet
      </section>
    )
  }

  return (
    <section aria-label={title} className={cn("w-full", className)}>
      <div className="h-64 w-full sm:h-72" data-testid="skill-radar-chart">
        <ResponsiveContainer width="100%" height="100%">
          <RadarChart data={data} outerRadius="72%">
            <PolarGrid gridType="polygon" radialLines />
            <PolarAngleAxis
              dataKey="label"
              tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
              tickLine={false}
            />
            <PolarRadiusAxis angle={90} domain={[0, domainMax]} tick={false} axisLine={false} />
            <Tooltip content={<SkillRadarTooltip />} />
            <Radar
              dataKey="level"
              name="Level"
              stroke="var(--chart-2)"
              fill="var(--chart-2)"
              fillOpacity={0.24}
              strokeWidth={2}
              dot={{ r: 3, fill: "var(--chart-2)", strokeWidth: 0 }}
              isAnimationActive={false}
            />
          </RadarChart>
        </ResponsiveContainer>
      </div>
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th scope="col">Skill</th>
            <th scope="col">Level</th>
            <th scope="col">XP</th>
          </tr>
        </thead>
        <tbody>
          {data.map((axis) => (
            <tr key={axis.id}>
              <th scope="row">{axis.label}</th>
              <td>{formatLevel(axis.level, axis.maxLevel)}</td>
              <td>{typeof axis.xp === "number" ? axis.xp : "N/A"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
        {data.map((axis) => (
          <div key={axis.id} className="min-w-0 rounded-md border border-border/70 px-3 py-2">
            <div className="truncate text-xs font-medium text-foreground">{axis.label}</div>
            <div className="mt-1 font-mono text-xs text-muted-foreground">
              {formatLevel(axis.level, axis.maxLevel)}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

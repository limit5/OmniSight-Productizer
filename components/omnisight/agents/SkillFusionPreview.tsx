"use client"

import { ArrowRight, GitMerge, Sparkles } from "lucide-react"
import type { ReactElement } from "react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export interface FusionSkillRef {
  id: string
  label: string
  level: number
  maxLevel?: number
}

export interface FusionSkillOutcome {
  id: string
  label: string
  beforeLevel: number
  afterLevel: number
  maxLevel: number
}

export interface SkillFusionOutcome {
  components: [FusionSkillOutcome, FusionSkillOutcome]
  hybrid: FusionSkillOutcome
}

export interface SkillFusionPreviewProps {
  components: [FusionSkillRef, FusionSkillRef]
  hybrid: FusionSkillRef
  className?: string
}

const DEFAULT_MAX_LEVEL = 5
const COMPONENT_FUSION_LEVEL_COST = 1
const HYBRID_FUSION_START_LEVEL = 3

function normalizeLevel(level: number, maxLevel: number): number {
  if (!Number.isFinite(level)) return 0
  return Math.min(Math.max(Math.trunc(level), 0), maxLevel)
}

function toComponentOutcome(skill: FusionSkillRef): FusionSkillOutcome {
  const maxLevel = skill.maxLevel ?? DEFAULT_MAX_LEVEL
  const beforeLevel = normalizeLevel(skill.level, maxLevel)

  return {
    id: skill.id,
    label: skill.label,
    beforeLevel,
    afterLevel: Math.max(0, beforeLevel - COMPONENT_FUSION_LEVEL_COST),
    maxLevel,
  }
}

function toHybridOutcome(skill: FusionSkillRef): FusionSkillOutcome {
  const maxLevel = Math.max(skill.maxLevel ?? DEFAULT_MAX_LEVEL, HYBRID_FUSION_START_LEVEL)
  const beforeLevel = normalizeLevel(skill.level, maxLevel)

  return {
    id: skill.id,
    label: skill.label,
    beforeLevel,
    afterLevel: normalizeLevel(HYBRID_FUSION_START_LEVEL, maxLevel),
    maxLevel,
  }
}

export function previewSkillFusionOutcome(
  components: [FusionSkillRef, FusionSkillRef],
  hybrid: FusionSkillRef,
): SkillFusionOutcome {
  return {
    components: [toComponentOutcome(components[0]), toComponentOutcome(components[1])],
    hybrid: toHybridOutcome(hybrid),
  }
}

function LevelDelta({ outcome }: { outcome: FusionSkillOutcome }): ReactElement {
  return (
    <div className="flex items-center gap-1.5 font-mono text-xs text-muted-foreground">
      <span>Lv {outcome.beforeLevel}</span>
      <ArrowRight className="size-3" aria-hidden="true" />
      <span className="font-semibold text-foreground">Lv {outcome.afterLevel}</span>
      <span>/ {outcome.maxLevel}</span>
    </div>
  )
}

export function SkillFusionPreview({
  components,
  hybrid,
  className,
}: SkillFusionPreviewProps): ReactElement {
  const outcome = previewSkillFusionOutcome(components, hybrid)

  return (
    <section
      aria-label="Skill fusion outcome preview"
      className={cn("rounded-lg border bg-card p-4 text-card-foreground shadow-sm", className)}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <GitMerge className="size-4 text-primary" aria-hidden="true" />
            Skill Fusion
          </div>
        </div>
        <Badge variant="outline" className="h-7 gap-1.5 px-2 text-xs">
          <Sparkles className="size-3.5 text-amber-500" aria-hidden="true" />
          Hybrid Lv 3
        </Badge>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_auto_1fr] sm:items-stretch">
        <div className="grid gap-2">
          {outcome.components.map((skill) => (
            <div key={skill.id} className="rounded-md border border-border/70 px-3 py-2">
              <div className="truncate text-xs font-medium text-foreground">{skill.label}</div>
              <div className="mt-1">
                <LevelDelta outcome={skill} />
              </div>
            </div>
          ))}
        </div>

        <div className="hidden items-center justify-center text-muted-foreground sm:flex">
          <ArrowRight className="size-4" aria-hidden="true" />
        </div>

        <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2">
          <div className="truncate text-xs font-medium text-foreground">
            {outcome.hybrid.label}
          </div>
          <div className="mt-1">
            <LevelDelta outcome={outcome.hybrid} />
          </div>
        </div>
      </div>
    </section>
  )
}

export default SkillFusionPreview

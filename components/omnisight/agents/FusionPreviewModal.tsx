"use client"

/**
 * RPG.W19.3 — frontend fusion modal with preview.
 *
 * Pure presentation: callers resolve candidate agents, projected stats,
 * and confirmation wiring. The modal owns only the operator review
 * surface before a fusion action is submitted.
 */

import { GitCompareArrows, Sparkles, Swords } from "lucide-react"
import type { ReactElement } from "react"
import { useCallback } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

import { CharacterCard } from "./CharacterCard"
import type { CharacterCardProps } from "./CharacterCard"
import { SkillFusionPreview } from "./SkillFusionPreview"
import type { SkillFusionPreviewProps } from "./SkillFusionPreview"
import { SkillRadarChart } from "./SkillRadarChart"
import type { SkillRadarAxis } from "./SkillRadarChart"

export interface FusionSourceAgent
  extends Omit<CharacterCardProps, "className" | "styleFingerprint"> {
  roleLabel: string
}

export interface FusionPreviewAgent extends CharacterCardProps {
  skillAxes: SkillRadarAxis[]
  fusionClassName?: string
  projectedTrait?: string | null
}

export interface FusionPreviewModalProps {
  open: boolean
  sourceAgents: [FusionSourceAgent, FusionSourceAgent]
  previewAgent: FusionPreviewAgent
  skillFusion?: Pick<SkillFusionPreviewProps, "components" | "hybrid"> | null
  onClose: () => void
  onConfirm?: () => void
  confirmDisabled?: boolean
  className?: string
}

function formatLevel(level: number): string {
  if (!Number.isFinite(level)) return "Level 1"
  return `Level ${Math.max(1, Math.trunc(level))}`
}

function SourceSummaryCard({
  agent,
}: {
  agent: FusionSourceAgent
}): ReactElement {
  return (
    <article
      className="min-w-0 rounded-md border border-border/70 bg-muted/25 p-3"
      data-testid={`fusion-preview-source-${agent.agentId}`}
      data-agent-id={agent.agentId}
      data-agent-guild={agent.guild}
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">
          {agent.roleLabel}
        </Badge>
        <span className="font-mono text-[10px] text-muted-foreground">
          {formatLevel(agent.level)}
        </span>
      </div>
      <div className="truncate text-sm font-semibold text-foreground">
        {agent.displayName}
      </div>
      <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
        {agent.agentId}
      </div>
      <div className="mt-3 rounded border border-dashed border-border/70 bg-background/60 px-2 py-1.5">
        <div className="text-[10px] font-medium uppercase text-muted-foreground">
          Specialization
        </div>
        <div className="mt-0.5 truncate text-xs text-foreground">
          {agent.specialization}
        </div>
      </div>
    </article>
  )
}

export function FusionPreviewModal({
  open,
  sourceAgents,
  previewAgent,
  skillFusion,
  onClose,
  onConfirm,
  confirmDisabled,
  className,
}: FusionPreviewModalProps): ReactElement | null {
  const handleOpenChange = useCallback(
    (next: boolean) => {
      if (!next) onClose()
    },
    [onClose],
  )

  const handleConfirm = useCallback(() => {
    if (!onConfirm || confirmDisabled) return
    onConfirm()
  }, [onConfirm, confirmDisabled])

  if (!open) return null

  const [primary, catalyst] = sourceAgents
  const canConfirm = Boolean(onConfirm) && !confirmDisabled
  const projectedTrait = previewAgent.projectedTrait?.trim()

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className={cn(
          "max-h-[calc(100vh-2rem)] max-w-5xl overflow-y-auto",
          className,
        )}
        data-testid="fusion-preview-modal"
        data-preview-agent-id={previewAgent.agentId}
        data-source-agent-ids={`${primary.agentId},${catalyst.agentId}`}
      >
        <DialogHeader>
          <DialogTitle
            className="flex items-center gap-2 font-mono text-sm"
            data-testid="fusion-preview-modal-title"
          >
            <Swords className="size-4 text-amber-500" aria-hidden="true" />
            Fusion preview
          </DialogTitle>
          <DialogDescription className="font-mono text-[11px] text-muted-foreground">
            Review the projected class, level, and skill profile before submitting fusion.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
          <section
            aria-label="Fusion source agents"
            className="flex min-w-0 flex-col gap-3"
            data-testid="fusion-preview-sources"
          >
            <div className="flex items-center gap-2 font-mono text-[10px] uppercase text-muted-foreground">
              <GitCompareArrows className="size-3.5" aria-hidden="true" />
              Source pair
            </div>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
              <SourceSummaryCard agent={primary} />
              <SourceSummaryCard agent={catalyst} />
            </div>
          </section>

          <section
            aria-label="Projected fused agent"
            className="min-w-0 space-y-4"
            data-testid="fusion-preview-result"
          >
            <CharacterCard
              {...previewAgent}
              className="border-amber-500/35 shadow-amber-500/10"
              styleFingerprint={projectedTrait ?? previewAgent.styleFingerprint}
            />

            <div className="rounded-md border border-border/70 bg-card/50 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 font-mono text-[10px] uppercase text-muted-foreground">
                  <Sparkles className="size-3.5 text-amber-500" aria-hidden="true" />
                  Preview skill profile
                </div>
                {previewAgent.fusionClassName ? (
                  <Badge
                    variant="outline"
                    className="h-6 px-2 text-[10px]"
                    data-testid="fusion-preview-class"
                  >
                    {previewAgent.fusionClassName}
                  </Badge>
                ) : null}
              </div>
              <SkillRadarChart
                axes={previewAgent.skillAxes}
                guildName={previewAgent.fusionClassName ?? "Projected fusion"}
              />
            </div>

            {skillFusion ? (
              <SkillFusionPreview
                components={skillFusion.components}
                hybrid={skillFusion.hybrid}
                className="border-amber-500/25 bg-amber-500/5 shadow-none"
              />
            ) : null}
          </section>
        </div>

        <DialogFooter className="gap-2">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center justify-center rounded border border-border bg-card px-3 py-1.5 font-mono text-xs text-muted-foreground hover:text-foreground"
            data-testid="fusion-preview-modal-close"
          >
            Close
          </button>
          {onConfirm ? (
            <button
              type="button"
              onClick={handleConfirm}
              disabled={!canConfirm}
              className="inline-flex items-center justify-center gap-1 rounded border border-amber-500/55 bg-amber-500/10 px-3 py-1.5 font-mono text-xs text-amber-700 hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-50 dark:text-amber-300"
              data-testid="fusion-preview-modal-confirm"
            >
              <Sparkles className="size-3" aria-hidden="true" />
              Confirm fusion
            </button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default FusionPreviewModal

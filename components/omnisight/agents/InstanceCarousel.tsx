"use client"

/**
 * RPG.W8.3 - alpha/beta/gamma instance switching.
 *
 * Scope: presentational instance carousel only. Callers provide the
 * available agent instances and optional selection wiring.
 */

import { ChevronLeft, ChevronRight, Layers3 } from "lucide-react"
import type { KeyboardEvent, ReactElement } from "react"
import { useMemo, useRef, useState } from "react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

import { CharacterCard } from "./CharacterCard"
import type { CharacterCardProps } from "./CharacterCard"

export type AgentInstanceSuffix = "alpha" | "beta" | "gamma"

export interface AgentInstance extends CharacterCardProps {
  instanceSuffix: AgentInstanceSuffix
  statusLabel?: string | null
}

export interface InstanceCarouselProps {
  instances: readonly AgentInstance[]
  activeInstance?: AgentInstanceSuffix
  defaultInstance?: AgentInstanceSuffix
  onInstanceChange?: (instance: AgentInstance) => void
  className?: string
}

const INSTANCE_ORDER: AgentInstanceSuffix[] = ["alpha", "beta", "gamma"]

const INSTANCE_LABELS: Record<AgentInstanceSuffix, string> = {
  alpha: "Alpha",
  beta: "Beta",
  gamma: "Gamma",
}

function instanceOrderIndex(instance: AgentInstance): number {
  const index = INSTANCE_ORDER.indexOf(instance.instanceSuffix)
  return index === -1 ? INSTANCE_ORDER.length : index
}

function normalizeInstances(instances: readonly AgentInstance[]): AgentInstance[] {
  const bySuffix = new Map<AgentInstanceSuffix, AgentInstance>()

  instances.forEach((instance) => {
    if (!INSTANCE_ORDER.includes(instance.instanceSuffix)) return
    if (!bySuffix.has(instance.instanceSuffix)) {
      bySuffix.set(instance.instanceSuffix, instance)
    }
  })

  return Array.from(bySuffix.values()).sort(
    (left, right) => instanceOrderIndex(left) - instanceOrderIndex(right),
  )
}

function findInitialInstance(
  instances: readonly AgentInstance[],
  requested?: AgentInstanceSuffix,
): AgentInstance | null {
  if (instances.length === 0) return null
  if (!requested) return instances[0]
  return instances.find((instance) => instance.instanceSuffix === requested) ?? instances[0]
}

function wrapIndex(index: number, length: number): number {
  if (length <= 0) return 0
  return (index + length) % length
}

export function InstanceCarousel({
  instances,
  activeInstance,
  defaultInstance,
  onInstanceChange,
  className,
}: InstanceCarouselProps): ReactElement {
  const orderedInstances = useMemo(() => normalizeInstances(instances), [instances])
  const tabRefs = useRef<Partial<Record<AgentInstanceSuffix, HTMLButtonElement>>>({})
  const [internalInstance, setInternalInstance] = useState<AgentInstanceSuffix | null>(
    () => findInitialInstance(orderedInstances, defaultInstance)?.instanceSuffix ?? null,
  )

  const selectedInstance =
    findInitialInstance(orderedInstances, activeInstance ?? internalInstance ?? defaultInstance)
  const selectedIndex = selectedInstance
    ? orderedInstances.findIndex(
        (instance) => instance.instanceSuffix === selectedInstance.instanceSuffix,
      )
    : -1

  function selectInstance(instance: AgentInstance): void {
    setInternalInstance(instance.instanceSuffix)
    onInstanceChange?.(instance)
  }

  function selectOffset(offset: number): void {
    if (orderedInstances.length === 0 || selectedIndex === -1) return
    const nextInstance =
      orderedInstances[wrapIndex(selectedIndex + offset, orderedInstances.length)]
    selectInstance(nextInstance)
    tabRefs.current[nextInstance.instanceSuffix]?.focus()
  }

  function selectBoundary(boundary: "first" | "last"): void {
    if (orderedInstances.length === 0) return
    const nextInstance =
      boundary === "first" ? orderedInstances[0] : orderedInstances[orderedInstances.length - 1]
    selectInstance(nextInstance)
    tabRefs.current[nextInstance.instanceSuffix]?.focus()
  }

  function onTabsKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (orderedInstances.length < 2) return

    if (event.key === "ArrowLeft") {
      event.preventDefault()
      selectOffset(-1)
    } else if (event.key === "ArrowRight") {
      event.preventDefault()
      selectOffset(1)
    } else if (event.key === "Home") {
      event.preventDefault()
      selectBoundary("first")
    } else if (event.key === "End") {
      event.preventDefault()
      selectBoundary("last")
    }
  }

  return (
    <section
      aria-label="Agent instance carousel"
      className={cn("space-y-3", className)}
      data-testid="instance-carousel"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <div className="flex size-9 items-center justify-center rounded-md border bg-primary/10 text-primary">
            <Layers3 className="size-4" aria-hidden="true" />
          </div>
          <div>
            <h2 className="text-sm font-semibold leading-tight">Agent Instances</h2>
            <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
              alpha / beta / gamma
            </p>
          </div>
        </div>

        <Badge variant="outline" className="h-7 px-2 font-mono text-[10px]">
          {selectedInstance ? INSTANCE_LABELS[selectedInstance.instanceSuffix] : "No instance"}
        </Badge>
      </div>

      <div className="grid gap-3 sm:grid-cols-[auto_1fr_auto] sm:items-start">
        <button
          type="button"
          aria-label="Previous agent instance"
          onClick={() => selectOffset(-1)}
          disabled={orderedInstances.length < 2}
          className="inline-flex size-9 items-center justify-center rounded-md border bg-card text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-45"
          data-testid="instance-carousel-previous"
        >
          <ChevronLeft className="size-4" aria-hidden="true" />
        </button>

        <div className="min-w-0 space-y-3">
          <div
            role="tablist"
            aria-label="Agent instance selector"
            onKeyDown={onTabsKeyDown}
            tabIndex={-1}
            className="grid grid-cols-3 overflow-hidden rounded-md border bg-muted/25"
          >
            {INSTANCE_ORDER.map((suffix) => {
              const instance = orderedInstances.find((item) => item.instanceSuffix === suffix)
              const selected = selectedInstance?.instanceSuffix === suffix

              return (
                <button
                  key={suffix}
                  ref={(element) => {
                    if (element) tabRefs.current[suffix] = element
                    else delete tabRefs.current[suffix]
                  }}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  aria-controls={instance ? `instance-card-${suffix}` : undefined}
                  tabIndex={selected ? 0 : -1}
                  disabled={!instance}
                  onClick={() => {
                    if (instance) selectInstance(instance)
                  }}
                  className={cn(
                    "min-w-0 border-r px-3 py-2 text-left last:border-r-0",
                    "disabled:cursor-not-allowed disabled:opacity-45",
                    selected ? "bg-background shadow-sm" : "hover:bg-background/60",
                  )}
                  data-instance-suffix={suffix}
                  data-testid={`instance-carousel-tab-${suffix}`}
                >
                  <span className="block truncate text-xs font-semibold">
                    {INSTANCE_LABELS[suffix]}
                  </span>
                  <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground">
                    {instance?.statusLabel?.trim() ||
                      (instance ? instance.agentId : "Unavailable")}
                  </span>
                </button>
              )
            })}
          </div>

          {selectedInstance ? (
            <div
              id={`instance-card-${selectedInstance.instanceSuffix}`}
              role="tabpanel"
              aria-label={`${INSTANCE_LABELS[selectedInstance.instanceSuffix]} instance`}
              data-active-instance={selectedInstance.instanceSuffix}
              data-testid="instance-carousel-panel"
            >
              <CharacterCard
                {...selectedInstance}
                instanceSuffix={selectedInstance.instanceSuffix}
              />
            </div>
          ) : (
            <div
              className="rounded-lg border border-dashed bg-muted/20 p-4 text-sm text-muted-foreground"
              data-testid="instance-carousel-empty"
            >
              No alpha, beta, or gamma agent instance is available.
            </div>
          )}
        </div>

        <button
          type="button"
          aria-label="Next agent instance"
          onClick={() => selectOffset(1)}
          disabled={orderedInstances.length < 2}
          className="inline-flex size-9 items-center justify-center rounded-md border bg-card text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-45"
          data-testid="instance-carousel-next"
        >
          <ChevronRight className="size-4" aria-hidden="true" />
        </button>
      </div>
    </section>
  )
}

export default InstanceCarousel

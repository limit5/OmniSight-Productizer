"use client"

/**
 * OP-53 / MP.W6.1 - Multi-provider War Room container.
 *
 * ADR-0007's War Room mode is a four-panel operator surface. This
 * file owns only the detachable container shell; the quota tracker,
 * cost calculator, task backlog, and fine-grained tradeoff controls
 * land in the sibling MP.W6 tickets and can be injected through the
 * render slots without changing the layout contract.
 *
 * Module-global state audit: immutable panel metadata only. Detach
 * state is per component instance.
 */

import type { JSX, ReactNode } from "react"
import { useMemo, useState } from "react"
import {
  Calculator,
  CheckSquare,
  Gauge,
  Maximize2,
  Minimize2,
  PanelTopOpen,
  TimerReset,
  X,
} from "lucide-react"

import { cn } from "@/lib/utils"

export type WarRoomPanelId = "quota" | "cost" | "tasks" | "tradeoff"

export interface WarRoomPanelRenderContext {
  panelId: WarRoomPanelId
  detached: boolean
}

export interface WarRoomPanelSlot {
  title?: string
  eyebrow?: string
  status?: string
  render?: (context: WarRoomPanelRenderContext) => ReactNode
}

export interface WarRoomProps {
  className?: string
  title?: string
  subtitle?: string
  panels?: Partial<Record<WarRoomPanelId, WarRoomPanelSlot>>
  initialDetachedPanel?: WarRoomPanelId | null
  onDetachedPanelChange?: (panelId: WarRoomPanelId | null) => void
}

interface WarRoomPanelMeta {
  id: WarRoomPanelId
  title: string
  eyebrow: string
  status: string
  icon: typeof TimerReset
}

const PANEL_META: readonly WarRoomPanelMeta[] = Object.freeze([
  {
    id: "quota",
    title: "Quota tracker",
    eyebrow: "Provider capacity",
    status: "W6.2 slot",
    icon: TimerReset,
  },
  {
    id: "cost",
    title: "Cost calculator",
    eyebrow: "Per-task spend",
    status: "W6.3 slot",
    icon: Calculator,
  },
  {
    id: "tasks",
    title: "Tasks backlog",
    eyebrow: "Selectable work",
    status: "W6.4 slot",
    icon: CheckSquare,
  },
  {
    id: "tradeoff",
    title: "Tradeoff controls",
    eyebrow: "Cheap / fast",
    status: "W6.5 slot",
    icon: Gauge,
  },
])

function panelLabel(meta: WarRoomPanelMeta, slot?: WarRoomPanelSlot): string {
  return slot?.title ?? meta.title
}

function DefaultPanelBody({ meta }: { meta: WarRoomPanelMeta }): JSX.Element {
  const Icon = meta.icon

  return (
    <div className="flex h-full min-h-[180px] flex-col items-center justify-center gap-3 px-4 py-6 text-center">
      <div className="grid size-12 place-items-center rounded-sm border border-[var(--neural-cyan,#67e8f9)]/40 bg-[var(--neural-cyan,#67e8f9)]/10">
        <Icon className="size-6 text-[var(--neural-cyan,#67e8f9)]" aria-hidden="true" />
      </div>
      <div className="space-y-1">
        <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
          Awaiting module
        </p>
        <p className="text-sm leading-5 text-[var(--foreground,#e2e8f0)]">
          {meta.status} content mounts here.
        </p>
      </div>
    </div>
  )
}

function WarRoomPanel({
  meta,
  slot,
  detached,
  hidden,
  onDetach,
  onRestore,
}: {
  meta: WarRoomPanelMeta
  slot?: WarRoomPanelSlot
  detached: boolean
  hidden?: boolean
  onDetach: () => void
  onRestore: () => void
}): JSX.Element {
  const Icon = meta.icon
  const title = panelLabel(meta, slot)
  const eyebrow = slot?.eyebrow ?? meta.eyebrow
  const status = slot?.status ?? meta.status

  return (
    <section
      data-testid={`mp-war-room-panel-${meta.id}`}
      data-mp-war-room-panel={meta.id}
      data-mp-war-room-detached={detached ? "true" : "false"}
      aria-labelledby={`mp-war-room-panel-${meta.id}-title`}
      className={cn(
        "holo-glass-simple corner-brackets flex min-h-[260px] min-w-0 flex-col overflow-hidden rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))]",
        detached && "min-h-[520px]",
        hidden && "hidden",
      )}
    >
      <header className="flex min-h-12 items-center justify-between gap-3 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)]/50 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Icon className="size-4 shrink-0 text-[var(--neural-cyan,#67e8f9)]" aria-hidden="true" />
          <div className="min-w-0">
            <p className="truncate font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--muted-foreground,#94a3b8)]">
              {eyebrow}
            </p>
            <h3
              id={`mp-war-room-panel-${meta.id}-title`}
              className="truncate text-sm font-semibold text-[var(--foreground,#e2e8f0)]"
            >
              {title}
            </h3>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <span className="hidden rounded-sm border border-white/10 bg-white/[0.04] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--muted-foreground,#94a3b8)] sm:inline-flex">
            {status}
          </span>
          <button
            type="button"
            onClick={detached ? onRestore : onDetach}
            className="grid size-7 place-items-center rounded-sm border border-white/10 text-[var(--muted-foreground,#94a3b8)] transition hover:border-[var(--neural-cyan,#67e8f9)]/50 hover:bg-[var(--neural-cyan,#67e8f9)]/10 hover:text-[var(--neural-cyan,#67e8f9)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)]"
            aria-label={detached ? `Restore ${title}` : `Detach ${title}`}
            title={detached ? "Restore panel" : "Detach panel"}
          >
            {detached ? (
              <Minimize2 className="size-3.5" aria-hidden="true" />
            ) : (
              <Maximize2 className="size-3.5" aria-hidden="true" />
            )}
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-auto">
        {slot?.render ? (
          slot.render({ panelId: meta.id, detached })
        ) : (
          <DefaultPanelBody meta={meta} />
        )}
      </div>
    </section>
  )
}

export function WarRoom({
  className,
  title = "War Room",
  subtitle = "Four detachable panels for subscription routing decisions.",
  panels,
  initialDetachedPanel = null,
  onDetachedPanelChange,
}: WarRoomProps): JSX.Element {
  const [detachedPanel, setLocalDetachedPanel] =
    useState<WarRoomPanelId | null>(initialDetachedPanel)

  const panelSlots = useMemo(
    () =>
      Object.fromEntries(
        PANEL_META.map((meta) => [meta.id, panels?.[meta.id]]),
      ) as Partial<Record<WarRoomPanelId, WarRoomPanelSlot>>,
    [panels],
  )

  const setDetachedPanel = (panelId: WarRoomPanelId | null) => {
    setLocalDetachedPanel(panelId)
    onDetachedPanelChange?.(panelId)
  }

  const detachedMeta = PANEL_META.find((meta) => meta.id === detachedPanel)

  return (
    <section
      data-testid="mp-war-room"
      data-mp-war-room-detached-panel={detachedPanel ?? "none"}
      aria-labelledby="mp-war-room-title"
      className={cn(
        "relative space-y-3 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-[var(--background,#020617)]/70 p-3",
        className,
      )}
    >
      <header className="flex flex-col gap-3 border-b border-[var(--neural-border,rgba(148,163,184,0.35))] pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--neural-cyan,#67e8f9)]">
            <PanelTopOpen className="size-3.5" aria-hidden="true" />
            MP.W6 command surface
          </p>
          <h2
            id="mp-war-room-title"
            className="mt-1 truncate text-xl font-semibold text-[var(--foreground,#e2e8f0)]"
          >
            {title}
          </h2>
          <p className="mt-1 text-sm leading-5 text-[var(--muted-foreground,#94a3b8)]">
            {subtitle}
          </p>
        </div>

        {detachedMeta ? (
          <button
            type="button"
            onClick={() => setDetachedPanel(null)}
            className="inline-flex min-h-9 shrink-0 items-center justify-center gap-2 rounded-sm border border-white/10 px-3 py-2 text-sm text-[var(--muted-foreground,#94a3b8)] transition hover:border-[var(--neural-cyan,#67e8f9)]/50 hover:bg-[var(--neural-cyan,#67e8f9)]/10 hover:text-[var(--neural-cyan,#67e8f9)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)]"
          >
            <X className="size-4" aria-hidden="true" />
            Restore grid
          </button>
        ) : null}
      </header>

      {detachedMeta ? (
        <div
          data-testid="mp-war-room-detached-container"
          className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_220px]"
        >
          <WarRoomPanel
            meta={detachedMeta}
            slot={panelSlots[detachedMeta.id]}
            detached
            onDetach={() => setDetachedPanel(detachedMeta.id)}
            onRestore={() => setDetachedPanel(null)}
          />
          <aside
            aria-label="Docked War Room panels"
            className="grid gap-2 sm:grid-cols-3 lg:grid-cols-1"
          >
            {PANEL_META.filter((meta) => meta.id !== detachedMeta.id).map((meta) => (
              <button
                key={meta.id}
                type="button"
                data-testid={`mp-war-room-docked-panel-${meta.id}`}
                onClick={() => setDetachedPanel(meta.id)}
                className="flex min-h-16 min-w-0 items-center gap-2 rounded-sm border border-[var(--neural-border,rgba(148,163,184,0.35))] bg-white/[0.03] px-3 py-2 text-left transition hover:border-[var(--neural-cyan,#67e8f9)]/50 hover:bg-[var(--neural-cyan,#67e8f9)]/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--neural-cyan,#67e8f9)]"
              >
                <meta.icon className="size-4 shrink-0 text-[var(--neural-cyan,#67e8f9)]" aria-hidden="true" />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium text-[var(--foreground,#e2e8f0)]">
                    {panelLabel(meta, panelSlots[meta.id])}
                  </span>
                  <span className="block truncate font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--muted-foreground,#94a3b8)]">
                    Docked
                  </span>
                </span>
              </button>
            ))}
          </aside>
        </div>
      ) : (
        <div
          data-testid="mp-war-room-grid"
          className="grid gap-3 lg:grid-cols-2"
        >
          {PANEL_META.map((meta) => (
            <WarRoomPanel
              key={meta.id}
              meta={meta}
              slot={panelSlots[meta.id]}
              detached={false}
              onDetach={() => setDetachedPanel(meta.id)}
              onRestore={() => setDetachedPanel(null)}
            />
          ))}
        </div>
      )}
    </section>
  )
}

export default WarRoom

/**
 * V0 #5 — Workspace Bridge Card.
 *
 * Summary card rendered inside the command-center dashboard that gives
 * the operator a one-glance view of what's happening inside the three
 * per-product workspaces (`web` / `mobile` / `software`) without
 * actually leaking workspace state into the command-center global
 * state.  The card also doubles as the *entry point* back into a
 * workspace — clicking a row routes to `/workspace/<type>`.
 *
 * Why this component is deliberately read-only:
 * V0 #3 established a hard isolation contract between the command
 * center's global providers (auth, tenant, engine) and each
 * workspace's per-subtree `WorkspaceProvider`.  This card lives in
 * the command center, so it **must not** call `useWorkspaceContext()`
 * — there is no provider here.  Instead it reads the persisted
 * snapshots V0 #4 already writes to:
 *   1. `localStorage` (fast, sync-on-mount)
 *   2. `GET /api/workspace/<type>/session` (authoritative, async)
 *
 * If a workspace has never been entered, its entry renders as
 * "idle" / "No project loaded".  That's intentional: the card is a
 * faithful projection of whatever state the workspace last
 * persisted; it does not invent a phantom session to look busy.
 *
 * Scope for V0 #5:
 *   - 3 rows (one per workspace type) with agent status + project
 *     name + preview status + last-event relative timestamp.
 *   - Summary header: "{N} / 3 workspaces running" where *running*
 *     = agent status is one of `running` / `paused` / `error`.
 *   - Each row is a Link to `/workspace/<type>` and also calls an
 *     optional `onNavigate(type)` hook so the command-center shell
 *     can intercept (tabbed nav / split-panel / analytics).
 *
 * Explicitly OUT of scope (future V0 checkboxes):
 *   - Live SSE updates          → V0 #6 (workspace.type SSE filter)
 *   - Shared chat preview       → V0 #7 (workspace-chat)
 *   - Per-type sidebar template → V0 #8 (workspace navigation sidebar)
 *
 * Test seam: callers can inject `workspaces` directly to render a
 * known state (used by the command-center dashboard when it already
 * has live data from other providers); otherwise the card hydrates
 * itself via the persistence utilities.  `disableBackendSync` skips
 * the GET-on-mount for browser-only / offline use cases.
 */
"use client"

import * as React from "react"
import Link from "next/link"
import { cn } from "@/lib/utils"
import {
  WORKSPACE_TYPES,
  type WorkspaceType,
} from "@/app/workspace/[type]/layout"
import {
  DEFAULT_AGENT_SESSION_STATE,
  DEFAULT_PREVIEW_STATE,
  DEFAULT_PROJECT_STATE,
  type WorkspaceAgentSessionState,
  type WorkspaceAgentSessionStatus,
  type WorkspacePreviewState,
  type WorkspacePreviewStatus,
  type WorkspaceProjectState,
} from "@/components/omnisight/workspace-context"
import {
  fetchWorkspaceSnapshotFromBackend,
  loadWorkspaceSnapshotFromStorage,
  pickNewerEnvelope,
  type WorkspaceSnapshotEnvelope,
} from "@/hooks/use-workspace-persistence"

// ─── Public shape ──────────────────────────────────────────────────────────

export interface WorkspaceSummary {
  type: WorkspaceType
  project: WorkspaceProjectState
  agentSession: WorkspaceAgentSessionState
  preview: WorkspacePreviewState
  /** ISO-8601 of the snapshot's `savedAt`; `null` = never persisted. */
  savedAt: string | null
}

export interface WorkspaceBridgeCardProps {
  /**
   * Explicit workspace summaries.  If provided, the card renders
   * them directly and skips storage/backend hydration entirely.
   * Missing workspace types render with default (idle) state.
   */
  workspaces?: WorkspaceSummary[]
  /** Optional navigation interceptor; fires before the `<Link>`. */
  onNavigate?: (type: WorkspaceType) => void
  /** Skip the backend GET on mount.  Storage-only / offline mode. */
  disableBackendSync?: boolean
  /** Inject a fetch for tests; defaults to global `fetch`. */
  fetchImpl?: typeof fetch
  /** Active = status ∈ {running, paused, error}; override for taste. */
  isWorkspaceActive?: (w: WorkspaceSummary) => boolean
  /**
   * Clock for the "last event" relative timestamp.  Defaults to
   * `Date.now`.  Tests can pin this so the rendered string is
   * deterministic.
   */
  nowMs?: () => number
  /** Card heading override; defaults to "Workspaces". */
  title?: string
  className?: string
}

// ─── Default predicates ────────────────────────────────────────────────────

const RUNNING_STATUSES: ReadonlySet<WorkspaceAgentSessionStatus> = new Set([
  "running",
  "paused",
  "error",
])

export function defaultIsWorkspaceActive(w: WorkspaceSummary): boolean {
  return RUNNING_STATUSES.has(w.agentSession.status)
}

export function emptyWorkspaceSummary(type: WorkspaceType): WorkspaceSummary {
  return {
    type,
    project: { ...DEFAULT_PROJECT_STATE },
    agentSession: { ...DEFAULT_AGENT_SESSION_STATE },
    preview: { ...DEFAULT_PREVIEW_STATE },
    savedAt: null,
  }
}

/**
 * Narrow a persisted envelope into the summary shape used by the
 * card.  Missing sub-states fall back to V0 #3 defaults, so a
 * partial snapshot (e.g. only `project`) renders cleanly.
 */
export function summariseEnvelope(
  type: WorkspaceType,
  envelope: WorkspaceSnapshotEnvelope | null,
): WorkspaceSummary {
  const base = emptyWorkspaceSummary(type)
  if (!envelope) return base
  return {
    type,
    project: { ...base.project, ...(envelope.state.project ?? {}) },
    agentSession: {
      ...base.agentSession,
      ...(envelope.state.agentSession ?? {}),
    },
    preview: { ...base.preview, ...(envelope.state.preview ?? {}) },
    savedAt: envelope.savedAt,
  }
}

// ─── Presentation helpers ──────────────────────────────────────────────────

const WORKSPACE_LABELS: Record<WorkspaceType, string> = {
  web: "Web",
  mobile: "Mobile",
  software: "Software",
}

const AGENT_STATUS_LABELS: Record<WorkspaceAgentSessionStatus, string> = {
  idle: "Idle",
  running: "Running",
  paused: "Paused",
  error: "Error",
  done: "Done",
}

const PREVIEW_STATUS_LABELS: Record<WorkspacePreviewStatus, string> = {
  idle: "No preview",
  loading: "Preview loading",
  ready: "Preview ready",
  error: "Preview error",
}

/**
 * Compact tone for the status badge — kept as tailwind utility
 * strings so the caller's theme still wins (the command-center
 * dashboard already ships its own holo / neural palette; these
 * map onto the neutral shadcn tokens and stay readable on both).
 */
const AGENT_STATUS_TONE: Record<WorkspaceAgentSessionStatus, string> = {
  idle: "bg-muted text-muted-foreground",
  running: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  paused: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  error: "bg-destructive/15 text-destructive",
  done: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
}

const ONE_SECOND = 1_000
const ONE_MINUTE = 60 * ONE_SECOND
const ONE_HOUR = 60 * ONE_MINUTE
const ONE_DAY = 24 * ONE_HOUR

export function formatRelativeSince(
  iso: string | null,
  nowMs: number,
): string {
  if (!iso) return "—"
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return "—"
  const diff = Math.max(0, nowMs - t)
  if (diff < ONE_MINUTE) return "just now"
  if (diff < ONE_HOUR) return `${Math.floor(diff / ONE_MINUTE)}m ago`
  if (diff < ONE_DAY) return `${Math.floor(diff / ONE_HOUR)}h ago`
  return `${Math.floor(diff / ONE_DAY)}d ago`
}

// ─── Component ─────────────────────────────────────────────────────────────

export function WorkspaceBridgeCard({
  workspaces,
  onNavigate,
  disableBackendSync = false,
  fetchImpl,
  isWorkspaceActive = defaultIsWorkspaceActive,
  nowMs = () => Date.now(),
  title = "Workspaces",
  className,
}: WorkspaceBridgeCardProps) {
  // When the caller passes `workspaces` explicitly we treat that as
  // the source of truth: the card is driven by the command-center
  // shell and should not second-guess it with stale storage reads.
  const controlled = Array.isArray(workspaces)

  const [hydrated, setHydrated] = React.useState<WorkspaceSummary[]>(() =>
    WORKSPACE_TYPES.map((t) => emptyWorkspaceSummary(t)),
  )

  // 1. Hydrate from localStorage + optional backend on mount.
  React.useEffect(() => {
    if (controlled) return
    let cancelled = false
    const ctrl = new AbortController()

    const local: Record<WorkspaceType, WorkspaceSnapshotEnvelope | null> = {
      web: loadWorkspaceSnapshotFromStorage("web"),
      mobile: loadWorkspaceSnapshotFromStorage("mobile"),
      software: loadWorkspaceSnapshotFromStorage("software"),
    }
    setHydrated(
      WORKSPACE_TYPES.map((t) => summariseEnvelope(t, local[t])),
    )

    if (disableBackendSync) return

    void (async () => {
      const results = await Promise.all(
        WORKSPACE_TYPES.map((t) =>
          fetchWorkspaceSnapshotFromBackend(t, {
            signal: ctrl.signal,
            fetchImpl,
          }).then((env) => ({ type: t, env })),
        ),
      )
      if (cancelled) return
      setHydrated((prev) =>
        prev.map((current) => {
          const hit = results.find((r) => r.type === current.type)
          if (!hit) return current
          const winner = pickNewerEnvelope(
            { schemaVersion: 1, savedAt: current.savedAt ?? "", state: {
              project: current.project,
              agentSession: current.agentSession,
              preview: current.preview,
            } },
            hit.env,
          )
          // When the stored snapshot is newer (or equal), keep the
          // current row; `pickNewerEnvelope` ties go to the
          // challenger (`hit.env`) so we still prefer the backend
          // on a tie, which matches V0 #4 semantics.
          if (!hit.env || winner !== hit.env) return current
          return summariseEnvelope(current.type, hit.env)
        }),
      )
    })()

    return () => {
      cancelled = true
      ctrl.abort()
    }
  }, [controlled, disableBackendSync, fetchImpl])

  // When controlled, render exactly what the caller passed — but
  // fill in missing workspace types with empty rows so the card
  // always shows all three.
  const source: WorkspaceSummary[] = React.useMemo(() => {
    if (!controlled) return hydrated
    const byType = new Map<WorkspaceType, WorkspaceSummary>()
    for (const w of workspaces!) byType.set(w.type, w)
    return WORKSPACE_TYPES.map(
      (t) => byType.get(t) ?? emptyWorkspaceSummary(t),
    )
  }, [controlled, workspaces, hydrated])

  const now = nowMs()
  const activeCount = source.filter(isWorkspaceActive).length

  return (
    <section
      data-testid="workspace-bridge-card"
      data-active-count={activeCount}
      data-total-count={WORKSPACE_TYPES.length}
      aria-label="Workspace summary bridge"
      className={cn(
        "rounded-lg border border-border bg-card/60 text-card-foreground shadow-sm",
        className,
      )}
    >
      <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex flex-col">
          <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
          <p
            data-testid="workspace-bridge-summary"
            className="text-xs text-muted-foreground"
          >
            <span data-testid="workspace-bridge-active-count">
              {activeCount}
            </span>
            {" / "}
            <span data-testid="workspace-bridge-total-count">
              {WORKSPACE_TYPES.length}
            </span>
            {" workspaces running"}
          </p>
        </div>
      </header>
      <ul
        data-testid="workspace-bridge-list"
        className="flex flex-col divide-y divide-border"
      >
        {source.map((w) => {
          const active = isWorkspaceActive(w)
          return (
            <li
              key={w.type}
              data-testid={`workspace-bridge-row-${w.type}`}
              data-workspace-type={w.type}
              data-active={active ? "true" : "false"}
              data-agent-status={w.agentSession.status}
              data-preview-status={w.preview.status}
              className="flex"
            >
              <Link
                href={`/workspace/${w.type}`}
                prefetch={false}
                onClick={() => onNavigate?.(w.type)}
                data-testid={`workspace-bridge-link-${w.type}`}
                aria-label={`Open ${WORKSPACE_LABELS[w.type]} workspace — agent ${AGENT_STATUS_LABELS[w.agentSession.status]}`}
                className="flex w-full items-center justify-between gap-3 px-4 py-3 text-sm transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="font-medium">
                      {WORKSPACE_LABELS[w.type]}
                    </span>
                    <span
                      data-testid={`workspace-bridge-project-${w.type}`}
                      className="truncate text-xs text-muted-foreground"
                    >
                      {w.project.name ?? "No project loaded"}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                    <span
                      data-testid={`workspace-bridge-preview-${w.type}`}
                    >
                      {PREVIEW_STATUS_LABELS[w.preview.status]}
                    </span>
                    <span aria-hidden="true">·</span>
                    <span
                      data-testid={`workspace-bridge-last-event-${w.type}`}
                    >
                      {formatRelativeSince(w.agentSession.lastEventAt, now)}
                    </span>
                  </div>
                </div>
                <span
                  data-testid={`workspace-bridge-status-${w.type}`}
                  className={cn(
                    "inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
                    AGENT_STATUS_TONE[w.agentSession.status],
                  )}
                >
                  {AGENT_STATUS_LABELS[w.agentSession.status]}
                </span>
              </Link>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

export default WorkspaceBridgeCard

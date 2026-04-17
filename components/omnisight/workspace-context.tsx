/**
 * V0 #3 — Per-workspace React context provider.
 *
 * Each `/workspace/[type]` subtree owns **its own** project / agent-session /
 * preview state.  The command-center dashboard (Agent Matrix Wall, global
 * status header, etc.) keeps its own global state via the existing auth /
 * tenant / engine providers; this provider deliberately does NOT read or
 * write to any of those.  That separation is the whole point of V0 #3:
 * switching between the web / mobile / software workspaces must never let
 * an in-flight preview URL or an agent session leak into the dashboard
 * (or between the three workspace types themselves).
 *
 * Scope for V0 #3 — *in-memory* state only:
 *   - current project (id / name / updatedAt)
 *   - current agent session (id / status / agentId / timestamps)
 *   - preview surface state (status / url / errorMessage / updatedAt)
 *
 * Explicitly OUT of scope (handled by later V0 checkboxes under #316):
 *   - localStorage + backend session sync          → V0 #4 persistence
 *   - bridge card summarising all 3 workspaces     → V0 #5
 *   - SSE workspace.type filter                    → V0 #6
 *   - shared chat panel                            → V0 #7
 *
 * This file therefore keeps the API surface lean: one provider, one hook,
 * three partial-merge setters plus a reset.  The persistence layer can
 * later compose around this provider (read `useWorkspaceContext()`, write
 * to storage on change) without any shape changes.
 */
"use client"

import * as React from "react"
import { isWorkspaceType, type WorkspaceType } from "@/app/workspace/[type]/layout"

// ─── Shape of per-workspace state ──────────────────────────────────────────

export interface WorkspaceProjectState {
  /** Stable id of the project bound to this workspace (null = no project loaded). */
  id: string | null
  /** Human-visible project name (null until resolved). */
  name: string | null
  /** ISO-8601 timestamp of the last mutation (any field). */
  updatedAt: string | null
}

export type WorkspaceAgentSessionStatus =
  | "idle"
  | "running"
  | "paused"
  | "error"
  | "done"

export interface WorkspaceAgentSessionState {
  sessionId: string | null
  agentId: string | null
  status: WorkspaceAgentSessionStatus
  /** ISO-8601 — when the session first transitioned out of idle. */
  startedAt: string | null
  /** ISO-8601 — last SSE / agent event observed in this workspace. */
  lastEventAt: string | null
}

export type WorkspacePreviewStatus = "idle" | "loading" | "ready" | "error"

export interface WorkspacePreviewState {
  status: WorkspacePreviewStatus
  /** Currently rendered preview URL (iframe / device frame / runtime output). */
  url: string | null
  /** Human-readable error message when `status === "error"`. */
  errorMessage: string | null
  /** ISO-8601 of the last preview surface update. */
  updatedAt: string | null
}

export interface WorkspaceState {
  type: WorkspaceType
  project: WorkspaceProjectState
  agentSession: WorkspaceAgentSessionState
  preview: WorkspacePreviewState
}

// ─── Defaults ──────────────────────────────────────────────────────────────

export const DEFAULT_PROJECT_STATE: WorkspaceProjectState = Object.freeze({
  id: null,
  name: null,
  updatedAt: null,
})

export const DEFAULT_AGENT_SESSION_STATE: WorkspaceAgentSessionState = Object.freeze({
  sessionId: null,
  agentId: null,
  status: "idle",
  startedAt: null,
  lastEventAt: null,
})

export const DEFAULT_PREVIEW_STATE: WorkspacePreviewState = Object.freeze({
  status: "idle",
  url: null,
  errorMessage: null,
  updatedAt: null,
})

export function defaultWorkspaceState(type: WorkspaceType): WorkspaceState {
  return {
    type,
    project: { ...DEFAULT_PROJECT_STATE },
    agentSession: { ...DEFAULT_AGENT_SESSION_STATE },
    preview: { ...DEFAULT_PREVIEW_STATE },
  }
}

// ─── Context value (state + setters) ──────────────────────────────────────

export interface WorkspaceContextValue extends WorkspaceState {
  /**
   * Partial merge of project fields.  Pass `null` to reset to defaults.
   * Bumps `project.updatedAt` if the caller didn't supply one.
   */
  setProject: (patch: Partial<WorkspaceProjectState> | null) => void
  /** Partial merge of agent-session fields; `null` resets to idle defaults. */
  setAgentSession: (patch: Partial<WorkspaceAgentSessionState> | null) => void
  /** Partial merge of preview-surface fields; `null` resets to idle defaults. */
  setPreviewState: (patch: Partial<WorkspacePreviewState> | null) => void
  /** Reset project + agentSession + preview back to fresh defaults. */
  resetWorkspace: () => void
}

const WorkspaceCtx = React.createContext<WorkspaceContextValue | null>(null)

// ─── Provider ──────────────────────────────────────────────────────────────

export interface WorkspaceProviderProps {
  type: WorkspaceType
  /**
   * Optional hydrated initial state — used by the persistence layer (V0 #4)
   * to seed the provider from localStorage / backend session sync.  Fields
   * absent from `initialState` fall back to defaults.
   */
  initialState?: Partial<Omit<WorkspaceState, "type">>
  children: React.ReactNode
}

function nowIso(): string {
  return new Date().toISOString()
}

export function WorkspaceProvider({
  type,
  initialState,
  children,
}: WorkspaceProviderProps) {
  if (!isWorkspaceType(type)) {
    throw new Error(
      `WorkspaceProvider received unknown workspace type "${type}" — ` +
        "must be one of web / mobile / software.",
    )
  }

  const [project, setProjectState] = React.useState<WorkspaceProjectState>(() => ({
    ...DEFAULT_PROJECT_STATE,
    ...(initialState?.project ?? {}),
  }))
  const [agentSession, setAgentSessionState] = React.useState<WorkspaceAgentSessionState>(
    () => ({
      ...DEFAULT_AGENT_SESSION_STATE,
      ...(initialState?.agentSession ?? {}),
    }),
  )
  const [preview, setPreviewInternal] = React.useState<WorkspacePreviewState>(() => ({
    ...DEFAULT_PREVIEW_STATE,
    ...(initialState?.preview ?? {}),
  }))

  const setProject = React.useCallback(
    (patch: Partial<WorkspaceProjectState> | null) => {
      setProjectState((prev) => {
        if (patch === null) return { ...DEFAULT_PROJECT_STATE }
        return {
          ...prev,
          ...patch,
          updatedAt: patch.updatedAt ?? nowIso(),
        }
      })
    },
    [],
  )

  const setAgentSession = React.useCallback(
    (patch: Partial<WorkspaceAgentSessionState> | null) => {
      setAgentSessionState((prev) => {
        if (patch === null) return { ...DEFAULT_AGENT_SESSION_STATE }
        return { ...prev, ...patch }
      })
    },
    [],
  )

  const setPreviewState = React.useCallback(
    (patch: Partial<WorkspacePreviewState> | null) => {
      setPreviewInternal((prev) => {
        if (patch === null) return { ...DEFAULT_PREVIEW_STATE }
        return {
          ...prev,
          ...patch,
          updatedAt: patch.updatedAt ?? nowIso(),
        }
      })
    },
    [],
  )

  const resetWorkspace = React.useCallback(() => {
    setProjectState({ ...DEFAULT_PROJECT_STATE })
    setAgentSessionState({ ...DEFAULT_AGENT_SESSION_STATE })
    setPreviewInternal({ ...DEFAULT_PREVIEW_STATE })
  }, [])

  const value = React.useMemo<WorkspaceContextValue>(
    () => ({
      type,
      project,
      agentSession,
      preview,
      setProject,
      setAgentSession,
      setPreviewState,
      resetWorkspace,
    }),
    [
      type,
      project,
      agentSession,
      preview,
      setProject,
      setAgentSession,
      setPreviewState,
      resetWorkspace,
    ],
  )

  return <WorkspaceCtx.Provider value={value}>{children}</WorkspaceCtx.Provider>
}

// ─── Hooks ─────────────────────────────────────────────────────────────────

/**
 * Read the current workspace context.  Throws if called outside a
 * `<WorkspaceProvider>` — this is on purpose: consumers should NEVER fall
 * back to reading the global command-center state when their workspace
 * scope is missing.  A missing provider is a caller bug, not a runtime
 * fallback case.
 */
export function useWorkspaceContext(): WorkspaceContextValue {
  const ctx = React.useContext(WorkspaceCtx)
  if (!ctx) {
    throw new Error(
      "useWorkspaceContext must be used inside <WorkspaceProvider>. " +
        "Did you render this component outside of /workspace/[type]?",
    )
  }
  return ctx
}

/** Convenience: read only the workspace type (no state subscription cost). */
export function useWorkspaceType(): WorkspaceType {
  return useWorkspaceContext().type
}

export default WorkspaceProvider

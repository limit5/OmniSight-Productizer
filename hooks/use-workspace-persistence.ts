/**
 * V0 #4 — Workspace session persistence utilities.
 *
 * Persists a per-workspace snapshot of {project, agentSession, preview}
 * (the three sub-states owned by `WorkspaceProvider`, V0 #3) across:
 *
 *   1. localStorage — synchronous, per-browser, SSR-safe.
 *   2. Backend session sync — eventual durability via
 *      `/api/workspace/[type]/session` (GET / PUT).
 *
 * Everything here is a pure utility — no React hooks, no context reads.
 * The integration into `WorkspaceProvider` lives in
 * `components/omnisight/persistent-workspace-provider.tsx`, which keeps
 * the V0 #3 API surface untouched.
 *
 * Envelope (schemaVersion=1):
 *   {
 *     schemaVersion: 1,
 *     savedAt: <ISO-8601>,
 *     state: {
 *       project?: WorkspaceProjectState,
 *       agentSession?: WorkspaceAgentSessionState,
 *       preview?: WorkspacePreviewState,
 *     }
 *   }
 *
 * Only `state` fields that resolve to objects are persisted — the
 * `type` discriminator lives in the URL / storage key, never in the
 * envelope body.  Any malformed payload (bad JSON, wrong version,
 * wrong shape) is treated as "no prior snapshot" and the loader
 * returns `null`.  The same guard applies to the backend response.
 */

import { WORKSPACE_TYPES, type WorkspaceType } from "@/app/workspace/[type]/types"
import type {
  WorkspaceAgentSessionState,
  WorkspacePreviewState,
  WorkspaceProjectState,
  WorkspaceState,
} from "@/components/omnisight/workspace-context"

export const WORKSPACE_SNAPSHOT_SCHEMA_VERSION = 1

export type WorkspaceSnapshotState = Partial<
  Omit<WorkspaceState, "type">
>

export interface WorkspaceSnapshotEnvelope {
  schemaVersion: number
  savedAt: string
  state: WorkspaceSnapshotState
}

const STORAGE_PREFIX = "omnisight:workspace"

export function workspaceStorageKey(type: WorkspaceType): string {
  return `${STORAGE_PREFIX}:${type}:session`
}

export function workspaceSessionApiPath(type: WorkspaceType): string {
  return `/api/workspace/${type}/session`
}

// ─── Guards ────────────────────────────────────────────────────────────────

function isValidWorkspaceType(value: unknown): value is WorkspaceType {
  return typeof value === "string" && (WORKSPACE_TYPES as readonly string[]).includes(value)
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v)
}

/**
 * Narrow an unknown payload to a valid `WorkspaceSnapshotEnvelope` or
 * return `null`.  Permissive on unknown fields inside `state` so
 * forward-compatible additions don't force a full reset, but strict on
 * `schemaVersion` + the top-level shape.
 */
export function parseWorkspaceEnvelope(
  raw: unknown,
): WorkspaceSnapshotEnvelope | null {
  if (!isPlainObject(raw)) return null
  const { schemaVersion, savedAt, state } = raw
  if (schemaVersion !== WORKSPACE_SNAPSHOT_SCHEMA_VERSION) return null
  if (typeof savedAt !== "string" || !savedAt) return null
  if (!isPlainObject(state)) return null

  const cleaned: WorkspaceSnapshotState = {}
  if (isPlainObject(state.project)) {
    cleaned.project = state.project as unknown as WorkspaceProjectState
  }
  if (isPlainObject(state.agentSession)) {
    cleaned.agentSession = state.agentSession as unknown as WorkspaceAgentSessionState
  }
  if (isPlainObject(state.preview)) {
    cleaned.preview = state.preview as unknown as WorkspacePreviewState
  }
  return { schemaVersion, savedAt, state: cleaned }
}

// ─── localStorage (synchronous, SSR-safe) ──────────────────────────────────

function hasLocalStorage(): boolean {
  return typeof window !== "undefined" && typeof window.localStorage !== "undefined"
}

/**
 * Read the persisted snapshot for a workspace type.  Returns `null`
 * when there is no prior snapshot, when storage is unavailable (SSR /
 * private mode / quota), or when the stored payload fails the schema
 * guard.  Never throws.
 */
export function loadWorkspaceSnapshotFromStorage(
  type: WorkspaceType,
): WorkspaceSnapshotEnvelope | null {
  if (!isValidWorkspaceType(type)) return null
  if (!hasLocalStorage()) return null
  try {
    const raw = window.localStorage.getItem(workspaceStorageKey(type))
    if (!raw) return null
    return parseWorkspaceEnvelope(JSON.parse(raw))
  } catch {
    return null
  }
}

/**
 * Write a snapshot to localStorage.  Returns the envelope that was
 * written, or `null` if storage was unavailable.  Never throws.
 */
export function saveWorkspaceSnapshotToStorage(
  type: WorkspaceType,
  state: WorkspaceSnapshotState,
  opts?: { savedAt?: string },
): WorkspaceSnapshotEnvelope | null {
  if (!isValidWorkspaceType(type)) return null
  if (!hasLocalStorage()) return null
  const envelope: WorkspaceSnapshotEnvelope = {
    schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
    savedAt: opts?.savedAt ?? new Date().toISOString(),
    state: {
      ...(state.project ? { project: state.project } : {}),
      ...(state.agentSession ? { agentSession: state.agentSession } : {}),
      ...(state.preview ? { preview: state.preview } : {}),
    },
  }
  try {
    window.localStorage.setItem(
      workspaceStorageKey(type),
      JSON.stringify(envelope),
    )
    return envelope
  } catch {
    return null
  }
}

export function clearWorkspaceSnapshotFromStorage(type: WorkspaceType): void {
  if (!isValidWorkspaceType(type)) return
  if (!hasLocalStorage()) return
  try {
    window.localStorage.removeItem(workspaceStorageKey(type))
  } catch {
    /* ignore */
  }
}

// ─── Backend session sync (best-effort) ────────────────────────────────────

/**
 * Fetch the latest snapshot from the backend for the given workspace
 * type.  Returns `null` on 204 / 404 / network error / malformed JSON
 * — never throws, so the caller can keep the localStorage snapshot.
 */
export async function fetchWorkspaceSnapshotFromBackend(
  type: WorkspaceType,
  opts?: { signal?: AbortSignal; fetchImpl?: typeof fetch },
): Promise<WorkspaceSnapshotEnvelope | null> {
  if (!isValidWorkspaceType(type)) return null
  const f = opts?.fetchImpl ?? (typeof fetch === "function" ? fetch : null)
  if (!f) return null
  try {
    const res = await f(workspaceSessionApiPath(type), {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: opts?.signal,
    })
    if (res.status === 204 || res.status === 404) return null
    if (!res.ok) return null
    const body = (await res.json()) as unknown
    return parseWorkspaceEnvelope(body)
  } catch {
    return null
  }
}

/**
 * Push a snapshot to the backend.  Returns the envelope that was
 * sent on success or `null` on failure.  Never throws.
 */
export async function pushWorkspaceSnapshotToBackend(
  type: WorkspaceType,
  state: WorkspaceSnapshotState,
  opts?: { savedAt?: string; signal?: AbortSignal; fetchImpl?: typeof fetch },
): Promise<WorkspaceSnapshotEnvelope | null> {
  if (!isValidWorkspaceType(type)) return null
  const f = opts?.fetchImpl ?? (typeof fetch === "function" ? fetch : null)
  if (!f) return null
  const envelope: WorkspaceSnapshotEnvelope = {
    schemaVersion: WORKSPACE_SNAPSHOT_SCHEMA_VERSION,
    savedAt: opts?.savedAt ?? new Date().toISOString(),
    state: {
      ...(state.project ? { project: state.project } : {}),
      ...(state.agentSession ? { agentSession: state.agentSession } : {}),
      ...(state.preview ? { preview: state.preview } : {}),
    },
  }
  try {
    const res = await f(workspaceSessionApiPath(type), {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(envelope),
      signal: opts?.signal,
    })
    if (!res.ok) return null
    return envelope
  } catch {
    return null
  }
}

/**
 * Compare two envelopes — returns the one with the later `savedAt`.
 * Ties go to `b` (the caller's "challenger"), matching the common
 * "newest-wins, last-writer-wins on equal" convention.  `null` inputs
 * are handled: a non-null envelope always beats `null`.
 */
export function pickNewerEnvelope(
  a: WorkspaceSnapshotEnvelope | null,
  b: WorkspaceSnapshotEnvelope | null,
): WorkspaceSnapshotEnvelope | null {
  if (!a) return b
  if (!b) return a
  return new Date(a.savedAt).getTime() > new Date(b.savedAt).getTime() ? a : b
}

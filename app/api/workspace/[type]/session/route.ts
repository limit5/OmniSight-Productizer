/**
 * V0 #4 — Workspace session sync endpoint.
 *
 * Complements the localStorage layer in
 * `hooks/use-workspace-persistence.ts`.  The browser writes its
 * snapshot to localStorage synchronously on every state change and,
 * after a short debounce, PUTs the same envelope here.  On mount the
 * browser GETs and, if the server has a newer `savedAt`, it hydrates
 * the live provider from the backend — this is how the state follows
 * the user across devices / browsers.
 *
 * Storage is a per-process in-memory `Map<WorkspaceType, envelope>`.
 * Durable backends (Redis / DB) can slot in behind the same
 * signature without the frontend noticing.  That deliberate
 * simplicity keeps the contract focused on the HTTP shape.
 *
 * Envelope schema (must match the browser side):
 *   {
 *     schemaVersion: 1,
 *     savedAt: <ISO-8601>,
 *     state: {
 *       project?:      { ... },
 *       agentSession?: { ... },
 *       preview?:      { ... },
 *     }
 *   }
 *
 * Responses:
 *   GET  /api/workspace/{type}/session
 *     200 envelope   when a snapshot exists
 *     204            when the store is empty
 *     400            unknown workspace type
 *   PUT  /api/workspace/{type}/session
 *     204            on success (body stored)
 *     400            unknown workspace type, bad JSON, bad schema
 */

import { NextResponse } from "next/server"

import {
  isWorkspaceType,
  WORKSPACE_TYPES,
  type WorkspaceType,
} from "@/app/workspace/[type]/layout"

const SCHEMA_VERSION = 1

type WorkspaceSnapshotState = {
  project?: Record<string, unknown>
  agentSession?: Record<string, unknown>
  preview?: Record<string, unknown>
}

type WorkspaceSnapshotEnvelope = {
  schemaVersion: number
  savedAt: string
  state: WorkspaceSnapshotState
}

type RouteParams = { type: string }

// ─── Store (in-memory, per-process) ───────────────────────────────────────
//
// We attach the map to `globalThis` so Next.js dev-mode HMR doesn't
// reset the snapshot between edits — the UX of losing your session
// every time a file hot-reloads would defeat the purpose.  The cast
// keeps TypeScript honest without polluting `global` types.

type WorkspaceSessionStore = Map<WorkspaceType, WorkspaceSnapshotEnvelope>

const STORE_KEY = "__omnisight_workspace_session_store__"

function getStore(): WorkspaceSessionStore {
  const g = globalThis as unknown as Record<string, WorkspaceSessionStore | undefined>
  let store = g[STORE_KEY]
  if (!store) {
    store = new Map<WorkspaceType, WorkspaceSnapshotEnvelope>()
    g[STORE_KEY] = store
  }
  return store
}

/** Exported for tests so they can tear down between cases. */
export function __resetWorkspaceSessionStoreForTests(): void {
  getStore().clear()
}

// ─── Validation ────────────────────────────────────────────────────────────

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v)
}

function parseEnvelope(raw: unknown): WorkspaceSnapshotEnvelope | null {
  if (!isPlainObject(raw)) return null
  if (raw.schemaVersion !== SCHEMA_VERSION) return null
  if (typeof raw.savedAt !== "string" || !raw.savedAt) return null
  if (!isPlainObject(raw.state)) return null

  const state: WorkspaceSnapshotState = {}
  if (isPlainObject(raw.state.project)) state.project = raw.state.project
  if (isPlainObject(raw.state.agentSession)) state.agentSession = raw.state.agentSession
  if (isPlainObject(raw.state.preview)) state.preview = raw.state.preview

  return { schemaVersion: SCHEMA_VERSION, savedAt: raw.savedAt, state }
}

function badType(type: string) {
  return NextResponse.json(
    {
      error: "unknown_workspace_type",
      message: `Unknown workspace type "${type}". Expected one of: ${WORKSPACE_TYPES.join(", ")}.`,
    },
    { status: 400 },
  )
}

// ─── Handlers ──────────────────────────────────────────────────────────────

export async function GET(
  _req: Request,
  ctx: { params: Promise<RouteParams> },
): Promise<NextResponse> {
  const { type } = await ctx.params
  if (!isWorkspaceType(type)) return badType(type)

  const snapshot = getStore().get(type)
  if (!snapshot) {
    return new NextResponse(null, { status: 204 })
  }
  return NextResponse.json(snapshot, { status: 200 })
}

export async function PUT(
  req: Request,
  ctx: { params: Promise<RouteParams> },
): Promise<NextResponse> {
  const { type } = await ctx.params
  if (!isWorkspaceType(type)) return badType(type)

  let body: unknown
  try {
    body = await req.json()
  } catch {
    return NextResponse.json(
      { error: "invalid_json", message: "Request body must be valid JSON." },
      { status: 400 },
    )
  }

  const envelope = parseEnvelope(body)
  if (!envelope) {
    return NextResponse.json(
      {
        error: "invalid_envelope",
        message: `Expected {schemaVersion: ${SCHEMA_VERSION}, savedAt: ISO-8601, state: { project?, agentSession?, preview? }}.`,
      },
      { status: 400 },
    )
  }

  getStore().set(type, envelope)
  return new NextResponse(null, { status: 204 })
}

export async function DELETE(
  _req: Request,
  ctx: { params: Promise<RouteParams> },
): Promise<NextResponse> {
  const { type } = await ctx.params
  if (!isWorkspaceType(type)) return badType(type)
  getStore().delete(type)
  return new NextResponse(null, { status: 204 })
}

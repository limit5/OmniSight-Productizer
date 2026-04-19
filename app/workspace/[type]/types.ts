// Pure types + constants for the `/workspace/[type]` route.
//
// This file exists to break a Next.js 16 server/client import trap: when
// `app/api/workspace/[type]/session/route.ts` (a server route) imports
// symbols from `layout.tsx`, Next.js transitively pulls in the layout's
// client dependencies (PersistentWorkspaceProvider has "use client"),
// which then forbids `generateMetadata` from being exported at all.
// Keeping these values in a zero-import module lets both files import
// without dragging client code into server build graphs.

export const WORKSPACE_TYPES = ["web", "mobile", "software"] as const
export type WorkspaceType = (typeof WORKSPACE_TYPES)[number]

const WORKSPACE_TYPE_SET: ReadonlySet<string> = new Set(WORKSPACE_TYPES)

export function isWorkspaceType(value: string): value is WorkspaceType {
  return WORKSPACE_TYPE_SET.has(value)
}

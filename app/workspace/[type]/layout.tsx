/**
 * V0 #1 — Workspace router. V0 #3 — wraps children in the per-workspace
 * `WorkspaceProvider` so every page under `/workspace/[type]` gets its own
 * (type-scoped) project / agent-session / preview state, cleanly isolated
 * from the command-center global state.  V0 #4 — upgrades the wrapper to
 * `PersistentWorkspaceProvider`, which seeds the provider from
 * `localStorage` + the backend session-sync endpoint so switching away
 * and back to a workspace no longer discards in-flight state.
 *
 * Dynamic segment `[type]` gates the three product-line workspaces
 * (`/workspace/web`, `/workspace/mobile`, `/workspace/software`).
 * An unknown type 404s immediately instead of rendering an empty shell,
 * which keeps typos and stale links from leaking the dev UX.
 */
import type { Metadata } from "next"
import { notFound } from "next/navigation"

import { PersistentWorkspaceProvider } from "@/components/omnisight/persistent-workspace-provider"

export const WORKSPACE_TYPES = ["web", "mobile", "software"] as const
export type WorkspaceType = (typeof WORKSPACE_TYPES)[number]

const WORKSPACE_TYPE_SET: ReadonlySet<string> = new Set(WORKSPACE_TYPES)

export function isWorkspaceType(value: string): value is WorkspaceType {
  return WORKSPACE_TYPE_SET.has(value)
}

const WORKSPACE_META: Record<WorkspaceType, { title: string; description: string }> = {
  web: {
    title: "Web Workspace · OmniSight",
    description: "Conversational UI generation with live preview, visual annotation, and shadcn/ui components.",
  },
  mobile: {
    title: "Mobile Workspace · OmniSight",
    description: "Multi-platform (iOS / Android / Flutter / RN) app generation with device-frame preview.",
  },
  software: {
    title: "Software Workspace · OmniSight",
    description: "Language-agnostic software track — CLI, services, libraries — generated and verified end-to-end.",
  },
}

type RouteParams = { type: string }

export function generateStaticParams(): Array<{ type: WorkspaceType }> {
  return WORKSPACE_TYPES.map((type) => ({ type }))
}

export async function generateMetadata(
  { params }: { params: Promise<RouteParams> },
): Promise<Metadata> {
  const { type } = await params
  if (!isWorkspaceType(type)) {
    return { title: "Workspace · OmniSight" }
  }
  const meta = WORKSPACE_META[type]
  return { title: meta.title, description: meta.description }
}

export default async function WorkspaceLayout(
  { children, params }: {
    children: React.ReactNode
    params: Promise<RouteParams>
  },
) {
  const { type } = await params
  if (!isWorkspaceType(type)) notFound()

  return (
    <section
      data-workspace-type={type}
      data-testid="workspace-root"
      className="min-h-screen bg-background text-foreground"
    >
      <PersistentWorkspaceProvider type={type}>{children}</PersistentWorkspaceProvider>
    </section>
  )
}

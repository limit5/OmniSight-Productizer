/**
 * V0 #1 — Workspace router.
 *
 * Dynamic segment `[type]` gates the three product-line workspaces
 * (`/workspace/web`, `/workspace/mobile`, `/workspace/software`).
 * An unknown type 404s immediately instead of rendering an empty shell,
 * which keeps typos and stale links from leaking the dev UX.
 *
 * This file is intentionally the *router only* — V0's shell / context
 * provider / chat / bridge card / SSE filter are separate checkboxes
 * under #316 and plug in as children (the shell will wrap `{children}`
 * once it lands in `components/omnisight/workspace-shell.tsx`).
 */
import type { Metadata } from "next"
import { notFound } from "next/navigation"

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
      {children}
    </section>
  )
}

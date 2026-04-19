/**
 * V0 #2 — Shared workspace layout shell.
 *
 * Three-column chrome used by every `/workspace/[type]` page:
 *   ┌────────────┬──────────────────────────┬──────────────────┐
 *   │  sidebar   │        preview           │   code + chat    │
 *   │ (palette / │   (live app / device /   │  (editor + NL    │
 *   │ platform / │    runtime output)       │   iteration)     │
 *   │  language) │                          │                  │
 *   └────────────┴──────────────────────────┴──────────────────┘
 *
 * The shell is purposefully dumb: it owns only the *layout* (grid
 * template, sidebar collapse state, slot headers). All content — the
 * per-type palette, the preview surface, the chat panel — is injected
 * by the caller as props. That keeps the three workspace pages free to
 * vary their inner contents without forking the frame.
 *
 * Why a grid (not <Resizable>) for V0 #2:
 *   We start with a fixed 3-column CSS grid because V0 just needs the
 *   structural envelope — resize affordances, tab-drag, panel persistence
 *   belong under later checkboxes of #316 (shell → context provider →
 *   persistence). A grid keeps the server-rendered markup testable with
 *   no react-resizable-panels client runtime in the critical path.
 */
"use client"

import * as React from "react"
import { PanelLeft, PanelLeftClose } from "lucide-react"
import { cn } from "@/lib/utils"
import type { WorkspaceType } from "@/app/workspace/[type]/types"

export interface WorkspaceShellProps {
  /** Workspace product line — drives default slot titles + data attrs. */
  type: WorkspaceType
  /** Left column. Per-type: component palette / platform selector / language selector. */
  sidebar: React.ReactNode
  /** Center column. Per-type: iframe preview / device frame / runtime output. */
  preview: React.ReactNode
  /** Right column. Shared across all three types once `workspace-chat.tsx` lands. */
  codeChat: React.ReactNode
  /** Override per-slot headings. Falls back to per-type defaults below. */
  sidebarTitle?: string
  previewTitle?: string
  codeChatTitle?: string
  /** Start with sidebar collapsed (narrow strip with icon only). */
  defaultSidebarCollapsed?: boolean
  className?: string
}

const DEFAULT_TITLES: Record<WorkspaceType, { sidebar: string; preview: string; codeChat: string }> = {
  web: { sidebar: "Components", preview: "Preview", codeChat: "Code & Chat" },
  mobile: { sidebar: "Platforms", preview: "Device Preview", codeChat: "Code & Chat" },
  software: { sidebar: "Languages", preview: "Runtime Output", codeChat: "Code & Chat" },
}

export function WorkspaceShell({
  type,
  sidebar,
  preview,
  codeChat,
  sidebarTitle,
  previewTitle,
  codeChatTitle,
  defaultSidebarCollapsed = false,
  className,
}: WorkspaceShellProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = React.useState<boolean>(defaultSidebarCollapsed)
  const titles = DEFAULT_TITLES[type]

  return (
    <div
      data-testid="workspace-shell"
      data-workspace-type={type}
      data-sidebar-collapsed={sidebarCollapsed ? "true" : "false"}
      className={cn(
        "grid h-full min-h-[calc(100vh-0px)] w-full bg-background text-foreground",
        // Expanded: fixed sidebar + flexible preview + fixed chat column.
        // Collapsed: sidebar shrinks to an icon rail.
        sidebarCollapsed
          ? "grid-cols-[44px_minmax(0,1fr)_minmax(320px,420px)]"
          : "grid-cols-[minmax(200px,260px)_minmax(0,1fr)_minmax(320px,420px)]",
        className,
      )}
    >
      <aside
        data-slot="sidebar"
        data-testid="workspace-shell-sidebar"
        data-collapsed={sidebarCollapsed ? "true" : "false"}
        aria-label={`${type} workspace sidebar`}
        className="flex min-h-0 flex-col overflow-hidden border-r border-border bg-card/40"
      >
        <header className="flex h-10 shrink-0 items-center justify-between gap-2 border-b border-border px-2">
          {!sidebarCollapsed && (
            <span className="truncate text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {sidebarTitle ?? titles.sidebar}
            </span>
          )}
          <button
            type="button"
            onClick={() => setSidebarCollapsed((c) => !c)}
            aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!sidebarCollapsed}
            aria-controls="workspace-shell-sidebar-body"
            data-testid="workspace-shell-sidebar-toggle"
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
          >
            {sidebarCollapsed ? (
              <PanelLeft className="size-4" aria-hidden="true" />
            ) : (
              <PanelLeftClose className="size-4" aria-hidden="true" />
            )}
          </button>
        </header>
        <div
          id="workspace-shell-sidebar-body"
          data-testid="workspace-shell-sidebar-body"
          hidden={sidebarCollapsed}
          className="min-h-0 flex-1 overflow-auto"
        >
          {sidebar}
        </div>
      </aside>

      <main
        data-slot="preview"
        data-testid="workspace-shell-preview"
        aria-label={`${type} workspace preview`}
        className="flex min-h-0 flex-col overflow-hidden border-r border-border"
      >
        <header className="flex h-10 shrink-0 items-center border-b border-border px-3">
          <span className="truncate text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {previewTitle ?? titles.preview}
          </span>
        </header>
        <div
          data-testid="workspace-shell-preview-body"
          className="min-h-0 flex-1 overflow-auto"
        >
          {preview}
        </div>
      </main>

      <aside
        data-slot="code-chat"
        data-testid="workspace-shell-code-chat"
        aria-label={`${type} workspace code and chat`}
        className="flex min-h-0 flex-col overflow-hidden bg-card/30"
      >
        <header className="flex h-10 shrink-0 items-center border-b border-border px-3">
          <span className="truncate text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            {codeChatTitle ?? titles.codeChat}
          </span>
        </header>
        <div
          data-testid="workspace-shell-code-chat-body"
          className="min-h-0 flex-1 overflow-auto"
        >
          {codeChat}
        </div>
      </aside>
    </div>
  )
}

export default WorkspaceShell

"use client"

import { useEffect, useRef, useState } from "react"
import { ChevronDown, FolderKanban } from "lucide-react"
import { useAuth } from "@/lib/auth-context"
import { useTenant } from "@/lib/tenant-context"
import { useProject } from "@/lib/project-context"

/**
 * Y8 row 2 — dashboard header project picker.
 *
 * Sits beside <TenantSwitcher /> and exposes the second level of the
 * tenant → project scope chain. Operator picks a project; the
 * X-Project-Id header is flipped via lib/api.setCurrentProjectId so
 * downstream requests (workflow run / artifacts / providers) get
 * project-scoped via the Y5 _project_header_gate.
 *
 * Visibility rules (mirror TenantSwitcher noise filter):
 *   • Hidden when authMode == "open" (anon dev environment) or no user.
 *   • Hidden when no current tenant is selected (gate prevents the
 *     dropdown from listing across tenants).
 *   • Hidden when the tenant has zero projects — there is nothing
 *     to pick. (Operator creates one via the Y8 row 4 settings page
 *     once it lands; until then the dashboard simply has no project
 *     scope and works at the tenant level.)
 *
 * Tenant-switch coupling is implicit: ProjectProvider's internal
 * useEffect re-fetches the project list whenever currentTenantId
 * changes, so this component's render is always coherent with the
 * active tenant. ProjectProvider also clears _currentProjectId at
 * the start of the tenant flip so X-Project-Id never leaks across
 * the tenant boundary while the new list is in flight.
 */
export function ProjectSwitcher() {
  const { user, authMode } = useAuth()
  const { currentTenantId } = useTenant()
  const { currentProjectId, projects, loading, switchProject } = useProject()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false) }
    document.addEventListener("mousedown", onDoc)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDoc)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  if (authMode === "open" || !user) return null
  if (!currentTenantId) return null

  // Loading or zero-project tenant: render nothing rather than an
  // empty dropdown (no actionable choice for the operator).
  if (loading) return null
  if (projects.length === 0) return null

  // Single-project tenant: render a static label so the operator
  // can still see the active scope, but no dropdown — clicking would
  // be a no-op. Same noise rule as TenantSwitcher's t-default branch.
  if (projects.length === 1) {
    const only = projects[0]
    return (
      <div
        className="inline-flex items-center gap-1 px-2 py-1 rounded font-mono text-[10px] text-[var(--muted-foreground)] bg-[var(--secondary)]/40"
        data-testid="project-switcher-static"
      >
        <FolderKanban size={10} />
        <span className="truncate max-w-[100px]">{only.name}</span>
      </div>
    )
  }

  const currentProject = projects.find(p => p.project_id === currentProjectId)

  function handleSwitch(projectId: string) {
    if (projectId === currentProjectId) {
      setOpen(false)
      return
    }
    switchProject(projectId)
    setOpen(false)
  }

  return (
    <div ref={ref} className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-label="Switch project"
        aria-haspopup="listbox"
        aria-expanded={open}
        className="inline-flex items-center gap-1 px-2 py-1 rounded font-mono text-[10px] hover:bg-[var(--neural-blue)]/10 transition-colors text-[var(--muted-foreground)]"
        data-testid="project-switcher-btn"
      >
        <FolderKanban size={10} />
        <span className="truncate max-w-[100px]">
          {currentProject?.name || currentProjectId || "—"}
        </span>
        <ChevronDown size={10} />
      </button>
      {open && (
        <div
          role="listbox"
          aria-label="Select project"
          className="absolute left-0 top-full mt-1 z-50 min-w-[200px] rounded border border-[var(--border)] bg-[var(--card)] shadow-lg p-1 font-mono text-xs"
          data-testid="project-switcher-list"
        >
          {projects.map(p => {
            const isArchived = p.archived_at !== null
            const isActive = p.project_id === currentProjectId
            return (
              <button
                key={p.project_id}
                type="button"
                role="option"
                aria-selected={isActive}
                onClick={() => handleSwitch(p.project_id)}
                disabled={isArchived}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-left ${
                  isActive
                    ? "bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]"
                    : "hover:bg-[var(--secondary)] text-[var(--foreground)]"
                } ${isArchived ? "opacity-50" : ""}`}
                data-testid={`project-option-${p.project_id}`}
              >
                <FolderKanban size={10} />
                <span className="truncate">{p.name}</span>
                <span className="ml-auto text-[9px] text-[var(--muted-foreground)]">
                  {isArchived ? "archived" : p.product_line}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

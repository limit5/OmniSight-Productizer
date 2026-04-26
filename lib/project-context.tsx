"use client"

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import { useAuth } from "@/lib/auth-context"
import { useTenant, onTenantChange } from "@/lib/tenant-context"
import {
  listTenantProjects,
  setCurrentProjectId,
  type TenantProjectInfo,
} from "@/lib/api"

interface ProjectContextValue {
  currentProjectId: string | null
  projects: TenantProjectInfo[]
  loading: boolean
  // Y8 row 2 — bumps every time switchProject() flips the active
  // project, so hooks holding project-scoped state can refetch by
  // including this in their useEffect dep array.
  projectChangeEpoch: number
  switchProject: (projectId: string | null) => void
  /** Force-refetch the project list for the current tenant — used by
   * the Y8 row 3+ admin pages when they create/archive a project so
   * the dashboard switcher reflects the change without a full reload. */
  refetch: () => void
}

const Ctx = createContext<ProjectContextValue | null>(null)

// Y8 row 2 — non-React subscriber bus, mirrors the tenant-context
// onTenantChange pattern. Used by hooks that hold project-scoped
// state and want to invalidate without re-reading context.
type ProjectChangeListener = (projectId: string | null) => void
const _listeners = new Set<ProjectChangeListener>()

export function onProjectChange(cb: ProjectChangeListener): () => void {
  _listeners.add(cb)
  return () => { _listeners.delete(cb) }
}

function _notifyProjectChange(projectId: string | null): void {
  for (const cb of Array.from(_listeners)) {
    try { cb(projectId) } catch (err) { console.warn("[project-context] listener error", err) }
  }
}

export function ProjectProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  const { currentTenantId } = useTenant()
  const [projects, setProjects] = useState<TenantProjectInfo[]>([])
  const [currentProjectId, setProjectId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [projectChangeEpoch, setProjectChangeEpoch] = useState(0)
  const [refetchTick, setRefetchTick] = useState(0)

  // Fetch project list whenever the active tenant flips. The
  // tenantChangeEpoch bump in tenant-context.switchTenant() already
  // cleared _currentProjectId via setCurrentProjectId(null) — we
  // mirror that into local state so the dropdown's "current" label
  // resets immediately rather than dangling on the previous tenant's
  // project until the network round-trip lands.
  useEffect(() => {
    if (!user || !currentTenantId) {
      setProjects([])
      setProjectId(null)
      setLoading(false)
      return
    }

    setProjectId(null)
    setLoading(true)

    let cancelled = false
    ;(async () => {
      try {
        const list = await listTenantProjects(currentTenantId)
        if (cancelled) return
        setProjects(list)
        // Auto-select the first live project so requests carry an
        // X-Project-Id header by default. Operator can pick another
        // via the dropdown; if no live projects exist, currentProjectId
        // stays null and the project-scoped header is omitted.
        const firstLive = list.find(p => p.archived_at === null) ?? list[0] ?? null
        if (firstLive) {
          setProjectId(firstLive.project_id)
          setCurrentProjectId(firstLive.project_id)
        } else {
          setCurrentProjectId(null)
        }
      } catch {
        if (!cancelled) {
          setProjects([])
          setCurrentProjectId(null)
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()

    return () => { cancelled = true }
  }, [user, currentTenantId, refetchTick])

  // Subscribe to the tenant-context bus so non-React tenant flips
  // (the rare case the React tree didn't see currentTenantId change
  // before a fetch fires) still trigger a project list refresh.
  useEffect(() => {
    const unsub = onTenantChange(() => {
      setRefetchTick(t => t + 1)
    })
    return unsub
  }, [])

  const switchProject = useCallback((projectId: string | null) => {
    setProjectId((prev) => {
      if (prev === projectId) return prev
      // Flip the api-level X-Project-Id header BEFORE notifying any
      // subscriber that re-fetches data — same ordering rule as the
      // tenant switcher, otherwise refetches would race the header.
      setCurrentProjectId(projectId)
      setProjectChangeEpoch((e) => e + 1)
      _notifyProjectChange(projectId)
      return projectId
    })
  }, [])

  const refetch = useCallback(() => {
    setRefetchTick(t => t + 1)
  }, [])

  return (
    <Ctx.Provider value={{
      currentProjectId,
      projects,
      loading,
      projectChangeEpoch,
      switchProject,
      refetch,
    }}>
      {children}
    </Ctx.Provider>
  )
}

export function useProject(): ProjectContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error("useProject must be used inside <ProjectProvider>")
  return v
}

"use client"

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import { useAuth } from "@/lib/auth-context"
import {
  listUserTenants,
  setCurrentTenantId,
  setCurrentProjectId,
  type TenantInfo,
} from "@/lib/api"

interface TenantContextValue {
  currentTenantId: string | null
  tenants: TenantInfo[]
  loading: boolean
  // Y8 row 1: monotonically incremented every time switchTenant() flips
  // the active tenant. Hooks that hold tenant-scoped state (provider
  // list, workflow runs, dashboard summary, etc.) include this in their
  // useEffect dep array so they refetch — and downstream localStorage
  // is auto-isolated by the tenant-prefixed key in lib/storage.ts, so
  // we only need to invalidate in-memory React state.
  tenantChangeEpoch: number
  switchTenant: (tenantId: string) => void
}

const Ctx = createContext<TenantContextValue | null>(null)

// Y8 row 1: low-level event bus so non-React consumers (vanilla classes,
// EventSource handlers, ad-hoc fetchers in app/page.tsx) can subscribe
// without going through TenantContext. The React layer drives this on
// every switchTenant() call after setCurrentTenantId(); subscribers see
// the new tenant id passed through directly so they don't need to re-read
// the context themselves.
type TenantChangeListener = (tenantId: string | null) => void
const _listeners = new Set<TenantChangeListener>()

export function onTenantChange(cb: TenantChangeListener): () => void {
  _listeners.add(cb)
  return () => { _listeners.delete(cb) }
}

function _notifyTenantChange(tenantId: string | null): void {
  for (const cb of Array.from(_listeners)) {
    try { cb(tenantId) } catch (err) { console.warn("[tenant-context] listener error", err) }
  }
}

export function TenantProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  const [tenants, setTenants] = useState<TenantInfo[]>([])
  const [currentTenantId, setTenantId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tenantChangeEpoch, setTenantChangeEpoch] = useState(0)

  useEffect(() => {
    if (!user) {
      setTenants([])
      setTenantId(null)
      setCurrentTenantId(null)
      setLoading(false)
      return
    }
    const tid = user.tenant_id || "t-default"
    setTenantId(tid)
    setCurrentTenantId(tid)

    let cancelled = false
    ;(async () => {
      try {
        const list = await listUserTenants()
        if (!cancelled) setTenants(list)
      } catch {
        if (!cancelled) {
          setTenants([{ id: tid, name: tid, plan: "free", enabled: true }])
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [user])

  const switchTenant = useCallback((tenantId: string) => {
    setTenantId((prev) => {
      if (prev === tenantId) return prev
      // The tenant-scoped X-Tenant-Id header is read from this module-
      // global on every request(), so flip it BEFORE notifying any
      // subscriber that re-fetches data — otherwise the refetch would
      // race the header update and hit the wrong tenant.
      setCurrentTenantId(tenantId)
      // Y8 row 1: clear the active project too — the previous tenant's
      // project id is meaningless under the new tenant, and Y5's
      // X-Project-Id header would otherwise leak across the boundary.
      // The project-switcher (Y8 row 2) seeds a fresh project on the
      // tenantChangeEpoch tick.
      setCurrentProjectId(null)
      setTenantChangeEpoch((e) => e + 1)
      _notifyTenantChange(tenantId)
      return tenantId
    })
  }, [])

  return (
    <Ctx.Provider value={{ currentTenantId, tenants, loading, tenantChangeEpoch, switchTenant }}>
      {children}
    </Ctx.Provider>
  )
}

export function useTenant(): TenantContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error("useTenant must be used inside <TenantProvider>")
  return v
}

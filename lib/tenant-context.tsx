"use client"

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import { useAuth } from "@/lib/auth-context"
import {
  listUserTenants,
  setCurrentTenantId,
  type TenantInfo,
} from "@/lib/api"

interface TenantContextValue {
  currentTenantId: string | null
  tenants: TenantInfo[]
  loading: boolean
  switchTenant: (tenantId: string) => void
}

const Ctx = createContext<TenantContextValue | null>(null)

export function TenantProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  const [tenants, setTenants] = useState<TenantInfo[]>([])
  const [currentTenantId, setTenantId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

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
    setTenantId(tenantId)
    setCurrentTenantId(tenantId)
  }, [])

  return (
    <Ctx.Provider value={{ currentTenantId, tenants, loading, switchTenant }}>
      {children}
    </Ctx.Provider>
  )
}

export function useTenant(): TenantContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error("useTenant must be used inside <TenantProvider>")
  return v
}

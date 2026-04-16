"use client"

import { useRef, useState, useEffect } from "react"
import { Building2, ChevronDown } from "lucide-react"
import { useTenant } from "@/lib/tenant-context"
import { useAuth } from "@/lib/auth-context"

export function TenantSwitcher() {
  const { currentTenantId, tenants, loading, switchTenant } = useTenant()
  const { user, authMode } = useAuth()
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
  if (loading || tenants.length <= 1) {
    if (!currentTenantId || currentTenantId === "t-default") return null
    return (
      <div className="inline-flex items-center gap-1 px-2 py-1 rounded font-mono text-[10px] text-[var(--muted-foreground)] bg-[var(--secondary)]/40">
        <Building2 size={10} />
        <span className="truncate max-w-[100px]">{tenants[0]?.name || currentTenantId}</span>
      </div>
    )
  }

  const currentTenant = tenants.find(t => t.id === currentTenantId)

  function handleSwitch(tenantId: string) {
    if (tenantId === currentTenantId) {
      setOpen(false)
      return
    }
    switchTenant(tenantId)
    setOpen(false)
  }

  return (
    <div ref={ref} className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-label="Switch tenant"
        aria-haspopup="listbox"
        aria-expanded={open}
        className="inline-flex items-center gap-1 px-2 py-1 rounded font-mono text-[10px] hover:bg-[var(--neural-blue)]/10 transition-colors text-[var(--muted-foreground)]"
        data-testid="tenant-switcher-btn"
      >
        <Building2 size={10} />
        <span className="truncate max-w-[100px]">{currentTenant?.name || currentTenantId}</span>
        <ChevronDown size={10} />
      </button>
      {open && (
        <div
          role="listbox"
          aria-label="Select tenant"
          className="absolute left-0 top-full mt-1 z-50 min-w-[180px] rounded border border-[var(--border)] bg-[var(--card)] shadow-lg p-1 font-mono text-xs"
          data-testid="tenant-switcher-list"
        >
          {tenants.map(t => (
            <button
              key={t.id}
              type="button"
              role="option"
              aria-selected={t.id === currentTenantId}
              onClick={() => handleSwitch(t.id)}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-left ${
                t.id === currentTenantId
                  ? "bg-[var(--neural-blue)]/10 text-[var(--neural-blue)]"
                  : "hover:bg-[var(--secondary)] text-[var(--foreground)]"
              } ${!t.enabled ? "opacity-50" : ""}`}
              disabled={!t.enabled}
              data-testid={`tenant-option-${t.id}`}
            >
              <Building2 size={10} />
              <span className="truncate">{t.name}</span>
              <span className="ml-auto text-[9px] text-[var(--muted-foreground)]">{t.plan}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

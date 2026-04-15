"use client"

/**
 * UserMenu — header-bar identity + logout chip.
 *
 * Renders nothing in `auth_mode=open` (the dev / single-user pre-
 * Phase-54 flow): there's no real user to display and no "logout"
 * action that does anything useful. In session/strict mode it
 * shows the logged-in operator's email + role with a logout button.
 */

import { useEffect, useRef, useState } from "react"
import { LogOut, Monitor, Shield, User as UserIcon } from "lucide-react"
import { useRouter } from "next/navigation"
import { useAuth } from "@/lib/auth-context"
import { SessionManagerPanel } from "./session-manager-panel"
import { MfaManagementPanel } from "./mfa-management-panel"

export function UserMenu() {
  const auth = useAuth()
  const router = useRouter()
  const [open, setOpen] = useState(false)
  const [showSessions, setShowSessions] = useState(false)
  const [showMfa, setShowMfa] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // Close on outside click / Escape — same shape as PanelHelp.
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

  // open mode = no real user + no useful logout. Render nothing so
  // the dev box's header stays unchanged.
  if (auth.authMode === "open" || !auth.user) return null

  const handleLogout = async () => {
    setOpen(false)
    await auth.logout()
    router.replace("/login")
  }

  return (
    <div ref={ref} className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={`account menu — ${auth.user.email}`}
        aria-haspopup="menu"
        aria-expanded={open}
        className="p-1.5 rounded hover:bg-[var(--neural-blue)]/10 transition-colors"
        title={`${auth.user.email} (${auth.user.role})`}
      >
        <UserIcon size={14} className="text-[var(--muted-foreground)] hover:text-[var(--neural-blue)]" />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-1 z-50 min-w-[200px] rounded border border-[var(--border)] bg-[var(--card)] shadow-lg p-2 font-mono text-xs"
        >
          <div className="px-2 py-1 border-b border-[var(--border)] mb-1">
            <div className="text-[var(--foreground)] truncate" title={auth.user.email}>
              {auth.user.email}
            </div>
            <div className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)]">
              role: {auth.user.role}
            </div>
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={() => { setOpen(false); setShowMfa(true) }}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[var(--secondary)] text-[var(--foreground)]"
            data-testid="menu-mfa-settings"
          >
            <Shield size={12} />
            MFA settings
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => { setOpen(false); setShowSessions(true) }}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[var(--secondary)] text-[var(--foreground)]"
            data-testid="menu-manage-sessions"
          >
            <Monitor size={12} />
            Manage sessions
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={handleLogout}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[var(--destructive)]/10 text-[var(--destructive)]"
          >
            <LogOut size={12} />
            Sign out
          </button>
        </div>
      )}

      {showSessions && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={() => setShowSessions(false)}>
          <div className="absolute inset-0 bg-black/40" />
          <div
            className="relative w-full max-w-lg max-h-[80vh] overflow-y-auto bg-[var(--background)] border border-[var(--border)] rounded-lg shadow-xl p-4"
            onClick={e => e.stopPropagation()}
          >
            <SessionManagerPanel />
            <div className="mt-3 flex justify-end">
              <button
                onClick={() => setShowSessions(false)}
                className="px-3 py-1.5 rounded font-mono text-xs bg-[var(--secondary)] text-[var(--foreground)] hover:bg-[var(--secondary)]/80 transition-colors"
                data-testid="sessions-close"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
      {showMfa && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={() => setShowMfa(false)}>
          <div className="absolute inset-0 bg-black/40" />
          <div
            className="relative w-full max-w-lg max-h-[80vh] overflow-y-auto bg-[var(--background)] border border-[var(--border)] rounded-lg shadow-xl p-4"
            onClick={e => e.stopPropagation()}
          >
            <MfaManagementPanel />
            <div className="mt-3 flex justify-end">
              <button
                onClick={() => setShowMfa(false)}
                className="px-3 py-1.5 rounded font-mono text-xs bg-[var(--secondary)] text-[var(--foreground)] hover:bg-[var(--secondary)]/80 transition-colors"
                data-testid="mfa-close"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

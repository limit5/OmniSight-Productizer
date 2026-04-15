"use client"

/**
 * Internet-exposure auth: client-side session context.
 *
 * Holds the result of `/auth/whoami` so any component can read the
 * current user without re-fetching. Also exposes the operations the
 * UI layer cares about (login / logout / refresh).
 *
 * The provider auto-fetches whoami once on mount. If the response is
 * 401 in non-`open` auth modes, the consumer (HomePage gate) sees
 * `user === null` and redirects to /login. In `open` mode whoami
 * returns the synthetic admin user, so the gate is a no-op for dev.
 */

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import {
  whoami as apiWhoami,
  login as apiLogin,
  logout as apiLogout,
  setCurrentSessionId,
  type AuthUser,
  type WhoamiResponse,
} from "@/lib/api"

interface AuthContextValue {
  user: AuthUser | null
  authMode: WhoamiResponse["auth_mode"] | null
  sessionId: string | null
  loading: boolean
  error: string | null
  login: (email: string, password: string) => Promise<boolean>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const Ctx = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [authMode, setAuthMode] = useState<WhoamiResponse["auth_mode"] | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const info = await apiWhoami()
      setUser(info.user)
      setAuthMode(info.auth_mode)
      setSessionId(info.session_id ?? null)
      setCurrentSessionId(info.session_id ?? null)
      setError(null)
    } catch (exc) {
      // 401 in non-open mode is the "not logged in" path — surface
      // user=null, no error banner. Other errors keep the message.
      const msg = exc instanceof Error ? exc.message : String(exc)
      setUser(null)
      if (msg.includes(" 401:") || msg.includes("401")) {
        setError(null)
      } else {
        setError(msg)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const login = useCallback(async (email: string, password: string) => {
    try {
      const res = await apiLogin(email, password)
      setUser(res.user)
      setError(null)
      return true
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      // Bad creds = 401, rate limit = 429. Surface both as a user
      // message; rethrow only on transport-level failures.
      if (msg.includes("401")) {
        setError("Invalid email or password.")
        return false
      }
      if (msg.includes("429")) {
        setError("Too many attempts. Please wait a few minutes and retry.")
        return false
      }
      setError(msg)
      return false
    }
  }, [])

  const logout = useCallback(async () => {
    try {
      await apiLogout()
    } catch {
      // logout is best-effort; even if the server 5xx's we still
      // want the local UI to clear so the user can re-login.
    }
    setUser(null)
    setSessionId(null)
    setCurrentSessionId(null)
  }, [])

  return (
    <Ctx.Provider value={{ user, authMode, sessionId, loading, error, login, logout, refresh }}>
      {children}
    </Ctx.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>")
  return v
}

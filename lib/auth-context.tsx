"use client"

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import {
  whoami as apiWhoami,
  login as apiLogin,
  logout as apiLogout,
  mfaChallenge as apiMfaChallenge,
  setCurrentSessionId,
  type AuthUser,
  type WhoamiResponse,
} from "@/lib/api"

interface MfaPending {
  mfa_token: string
  mfa_methods: string[]
  email: string
}

interface AuthContextValue {
  user: AuthUser | null
  authMode: WhoamiResponse["auth_mode"] | null
  sessionId: string | null
  loading: boolean
  error: string | null
  mfaPending: MfaPending | null
  login: (email: string, password: string) => Promise<boolean>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  submitMfa: (code: string) => Promise<boolean>
  cancelMfa: () => void
}

const Ctx = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [authMode, setAuthMode] = useState<WhoamiResponse["auth_mode"] | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [mfaPending, setMfaPending] = useState<MfaPending | null>(null)

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
      if (res.mfa_required && res.mfa_token) {
        setMfaPending({
          mfa_token: res.mfa_token,
          mfa_methods: res.mfa_methods || [],
          email,
        })
        setError(null)
        return false
      }
      if (res.user) {
        setUser(res.user as AuthUser)
      }
      setError(null)
      setMfaPending(null)
      return true
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
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

  const submitMfa = useCallback(async (code: string) => {
    if (!mfaPending) return false
    try {
      const res = await apiMfaChallenge(mfaPending.mfa_token, code)
      setUser(res.user)
      setMfaPending(null)
      setError(null)
      return true
    } catch (exc) {
      const msg = exc instanceof Error ? exc.message : String(exc)
      if (msg.includes("401")) {
        setError("Invalid MFA code.")
      } else {
        setError(msg)
      }
      return false
    }
  }, [mfaPending])

  const cancelMfa = useCallback(() => {
    setMfaPending(null)
    setError(null)
  }, [])

  const logout = useCallback(async () => {
    try {
      await apiLogout()
    } catch {
      // best-effort
    }
    setUser(null)
    setSessionId(null)
    setCurrentSessionId(null)
    setMfaPending(null)
  }, [])

  return (
    <Ctx.Provider value={{ user, authMode, sessionId, loading, error, mfaPending, login, logout, refresh, submitMfa, cancelMfa }}>
      {children}
    </Ctx.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>")
  return v
}

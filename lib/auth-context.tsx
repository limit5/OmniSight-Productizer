"use client"

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import {
  whoami as apiWhoami,
  login as apiLogin,
  logout as apiLogout,
  mfaChallenge as apiMfaChallenge,
  signup as apiSignup,
  setCurrentSessionId,
  ApiError,
  type AuthUser,
  type SignupRequestBody,
  type SignupResponse,
  type WhoamiResponse,
} from "@/lib/api"
import {
  classifyLoginError,
  type LoginErrorOutcome,
} from "@/lib/auth/login-form-helpers"
import {
  classifySignupError,
  type SignupErrorOutcome,
} from "@/lib/auth/signup-form-helpers"

interface MfaPending {
  mfa_token: string
  mfa_methods: string[]
  email: string
}

/** Pull a `Retry-After`-style hint out of an `ApiError`.
 *
 *  The backend's 423 / 429 responses set the `Retry-After` HTTP
 *  header AND embed the same number in the response body's `detail`
 *  string ("account locked; retry in 30s"). The header doesn't
 *  survive `request()`'s ApiError mapping today (the helper only
 *  retains body + parsed JSON), so we walk the parsed body's detail
 *  string for the `retry in <N>s` substring. Returns the integer
 *  string suitable for `parseRetryAfter()`, or null when neither
 *  source has a usable value. */
/** Pull the canonical error-code string out of an `ApiError`'s
 *  parsed body. Backend `/auth/login` shapes:
 *    - 429 bot/honeypot: `{"detail": {"error": "bot_challenge_failed"}}`
 *    - 423 lockout: `{"detail": "account locked; retry in 30s"}`
 *    - 401 invalid: `{"detail": "invalid email or password"}`
 *  Returns the explicit error-code field when one exists, else null.
 */
function _extractErrorCode(err: ApiError): string | null {
  const parsed = err.parsed
  if (!parsed) return null
  const top = parsed.error
  if (typeof top === "string") return top
  const detail = parsed.detail
  if (detail && typeof detail === "object") {
    const inner = (detail as Record<string, unknown>).error
    if (typeof inner === "string") return inner
  }
  return null
}

function _extractRetryAfter(err: ApiError): string | null {
  const detail = err.parsed?.detail
  if (typeof detail === "string") {
    const m = detail.match(/retry in (\d+)\s*s/i)
    if (m) return m[1]
  }
  // Some endpoints return numeric seconds at parsed.retry_after_s.
  if (typeof err.parsed?.retry_after_s === "number") {
    return String(err.parsed.retry_after_s)
  }
  return null
}

/** AS.7.2 — outcome surfaced to the signup page after `signup()`
 *  resolves. Either `ok` (auth-context absorbed the user) or
 *  `verifyEmail` (terminal state — page renders a "check your inbox"
 *  card) or `failed` (page renders the canonical error banner +
 *  spring-shake). */
export interface SignupOutcome {
  readonly status: "ok" | "verifyEmail" | "failed"
  readonly error: SignupErrorOutcome | null
  readonly emailVerificationRequired: boolean
  readonly email: string | null
}

interface AuthContextValue {
  user: AuthUser | null
  authMode: WhoamiResponse["auth_mode"] | null
  sessionId: string | null
  loading: boolean
  error: string | null
  /** AS.7.1 — structured outcome for the most recent failed login.
   *  Carries the canonical error kind + `accountLocked` flag the
   *  login page uses to render the blue-tint frozen overlay on 423.
   *  `null` until the first failure; cleared on the next success or
   *  on `cancelMfa()`. */
  lastLoginError: LoginErrorOutcome | null
  /** AS.7.2 — structured outcome for the most recent failed signup.
   *  `null` until the first failure; cleared on the next success or
   *  on a fresh `signup()` call. */
  lastSignupError: SignupErrorOutcome | null
  mfaPending: MfaPending | null
  login: (
    email: string,
    password: string,
    extras?: Readonly<Record<string, string>>,
  ) => Promise<boolean>
  /** AS.7.2 — submit a new-account registration. Returns a
   *  structured outcome the page can branch on (auto-login vs
   *  email-verify gate vs failure). */
  signup: (
    body: SignupRequestBody,
    extras?: Readonly<Record<string, string>>,
  ) => Promise<SignupOutcome>
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
  const [lastLoginError, setLastLoginError] =
    useState<LoginErrorOutcome | null>(null)
  const [lastSignupError, setLastSignupError] =
    useState<SignupErrorOutcome | null>(null)
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
      // Phase-3 P3 (2026-04-20): ONLY clear the user on a genuine 401.
      // Previously we did ``setUser(null)`` on every catch, which meant a
      // transient 429 (rate-limited), 5xx, or network blip flipped the
      // client into a logged-out state. That caused a cascading
      // redirect loop: transient error → user becomes null →
      // ``app/page.tsx`` effect redirects to ``/login?next=...`` →
      // ``/login`` page's AuthProvider re-runs whoami → 200 →
      // ``setUser(admin)`` → ``/login`` redirects back to the
      // dashboard → dashboard remounts its 7 panels → 14 fresh XHRs
      // hit the rate bucket → next whoami 429 → loop at ~333 ms
      // (matching the 6 batches-in-2-seconds pattern observed in
      // caddy access logs after the PG cutover exposed it by making
      // backend responses fast enough for the loop to close tightly).
      //
      // 401 is the ONLY status that MEANS "you are logged out" — keep
      // the existing user state for every other error so transient
      // failures surface as a banner, not a logout.
      const status = exc instanceof ApiError ? exc.status : null
      const msg = exc instanceof Error ? exc.message : String(exc)
      if (status === 401) {
        setUser(null)
        setError(null)
      } else {
        // Keep user as-is; surface the error so the UI can toast it
        // but don't treat it as a logout event.
        setError(msg)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const login = useCallback(
    async (
      email: string,
      password: string,
      extras?: Readonly<Record<string, string>>,
    ) => {
      try {
        const res = await apiLogin(email, password, extras)
        if (res.mfa_required && res.mfa_token) {
          setMfaPending({
            mfa_token: res.mfa_token,
            mfa_methods: res.mfa_methods || [],
            email,
          })
          setError(null)
          setLastLoginError(null)
          return false
        }
        if (res.user) {
          setUser(res.user as AuthUser)
        }
        setError(null)
        setLastLoginError(null)
        setMfaPending(null)
        return true
      } catch (exc) {
        // AS.7.1 — route every failure through the canonical
        // classifier so the login page gets a structured outcome
        // (kind / accountLocked / retryAfterSeconds) on top of the
        // human-readable message. Falls back to string-parse for
        // non-ApiError throws (network / timeout) so existing
        // behaviour is preserved.
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyLoginError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastLoginError(outcome)
          setError(outcome.message)
          return false
        }
        const msg = exc instanceof Error ? exc.message : String(exc)
        const outcome = classifyLoginError({
          status: null,
          message: msg,
          retryAfter: null,
        })
        setLastLoginError(outcome)
        setError(outcome.message)
        return false
      }
    },
    [],
  )

  const signup = useCallback(
    async (
      body: SignupRequestBody,
      extras?: Readonly<Record<string, string>>,
    ): Promise<SignupOutcome> => {
      try {
        const res: SignupResponse = await apiSignup(body, extras)
        // Success branch — clear any prior error so the next render
        // doesn't show stale state.
        setLastSignupError(null)
        if (res.email_verification_required) {
          setError(null)
          return Object.freeze({
            status: "verifyEmail" as const,
            error: null,
            emailVerificationRequired: true,
            email: res.email ?? body.email,
          })
        }
        if (res.user) {
          setUser(res.user as AuthUser)
        }
        setError(null)
        return Object.freeze({
          status: "ok" as const,
          error: null,
          emailVerificationRequired: false,
          email: res.email ?? body.email,
        })
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifySignupError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastSignupError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
            emailVerificationRequired: false,
            email: null,
          })
        }
        const msg = exc instanceof Error ? exc.message : String(exc)
        const outcome = classifySignupError({
          status: null,
          message: msg,
          retryAfter: null,
        })
        setLastSignupError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
          emailVerificationRequired: false,
          email: null,
        })
      }
    },
    [],
  )

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
    setLastLoginError(null)
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
    <Ctx.Provider value={{ user, authMode, sessionId, loading, error, lastLoginError, lastSignupError, mfaPending, login, signup, logout, refresh, submitMfa, cancelMfa }}>
      {children}
    </Ctx.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>")
  return v
}

"use client"

import { createContext, useCallback, useContext, useEffect, useState } from "react"
import {
  whoami as apiWhoami,
  login as apiLogin,
  logout as apiLogout,
  mfaChallenge as apiMfaChallenge,
  mfaWebauthnChallengeBegin as apiMfaWebauthnChallengeBegin,
  mfaWebauthnChallengeComplete as apiMfaWebauthnChallengeComplete,
  signup as apiSignup,
  requestPasswordReset as apiRequestPasswordReset,
  resetPassword as apiResetPassword,
  verifyEmail as apiVerifyEmail,
  resendEmailVerification as apiResendEmailVerification,
  setCurrentSessionId,
  ApiError,
  type AuthUser,
  type RequestPasswordResetResponse,
  type ResetPasswordRequestBody,
  type ResetPasswordResponse,
  type SignupRequestBody,
  type SignupResponse,
  type VerifyEmailRequestBody,
  type VerifyEmailResponse,
  type ResendVerifyEmailResponse,
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
import {
  classifyRequestResetError,
  classifyResetPasswordError,
  type RequestResetErrorOutcome,
  type ResetPasswordErrorOutcome,
} from "@/lib/auth/password-reset-helpers"
import {
  classifyMfaChallengeError,
  type MfaChallengeErrorOutcome,
} from "@/lib/auth/mfa-challenge-helpers"
import {
  classifyEmailVerifyError,
  classifyResendVerifyEmailError,
  type EmailVerifyErrorOutcome,
  type ResendVerifyEmailErrorOutcome,
} from "@/lib/auth/email-verify-helpers"

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

/** AS.7.3 — outcome surfaced to the forgot-password page after
 *  `requestPasswordReset()` resolves. `linkSent` is the terminal
 *  state where the page renders the "check your inbox" copy
 *  regardless of whether the email matched a known account (the
 *  enumeration-resistance contract). `failed` is reserved for
 *  genuine failure modes (rate-limit / bot-challenge / 5xx). */
export interface RequestPasswordResetOutcome {
  readonly status: "linkSent" | "failed"
  readonly error: RequestResetErrorOutcome | null
  readonly email: string | null
}

/** AS.7.3 — outcome surfaced to the reset-password page after
 *  `resetPassword()` resolves. `ok` means the new password was
 *  accepted; the page transitions to the success card and offers a
 *  "sign in now" CTA. `failed` carries the structured error so the
 *  page can branch on invalid_token / expired_token vs. weak
 *  password. */
export interface ResetPasswordOutcome {
  readonly status: "ok" | "failed"
  readonly error: ResetPasswordErrorOutcome | null
  readonly email: string | null
}

/** AS.7.4 — outcome surfaced to the dedicated MFA-challenge page
 *  after `submitMfa()` / `submitMfaWebauthn()` resolves. `ok` means
 *  the second-factor was accepted; the page plays the passed-check
 *  overlay and navigates to the post-login destination. `failed`
 *  carries the structured error so the page can branch on
 *  expired-challenge (kicks back to /login) vs. retryable
 *  invalid-code / rate-limited / webauthn-failed. */
export interface MfaChallengeOutcome {
  readonly status: "ok" | "failed"
  readonly error: MfaChallengeErrorOutcome | null
}

/** AS.7.5 — outcome surfaced to the email-verify page after
 *  `verifyEmail()` resolves. `ok` means the magic-link token was
 *  accepted; the page transitions to the success card and offers
 *  a "sign in now" CTA. `failed` carries the structured error so
 *  the page can branch on invalid_token / expired_token vs.
 *  already_verified vs. retryable. */
export interface EmailVerifyOutcome {
  readonly status: "ok" | "failed"
  readonly error: EmailVerifyErrorOutcome | null
  readonly email: string | null
}

/** AS.7.5 — outcome surfaced to the email-verify page after
 *  `resendEmailVerification()` resolves. `linkSent` is the terminal
 *  state where the page renders "we sent another link to ..." copy
 *  regardless of whether the email matched a known unverified user
 *  (enumeration-resistance contract per AS.0.7 §3.4). `failed` is
 *  reserved for genuine failure modes (invalid_input / rate-limit /
 *  bot-challenge / 5xx). */
export interface ResendVerifyEmailOutcome {
  readonly status: "linkSent" | "failed"
  readonly error: ResendVerifyEmailErrorOutcome | null
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
  /** AS.7.3 — structured outcome for the most recent failed
   *  request-password-reset call. `null` on success (terminal copy
   *  is shown regardless) or until the first failure. */
  lastRequestResetError: RequestResetErrorOutcome | null
  /** AS.7.3 — structured outcome for the most recent failed
   *  reset-password call (the new-password submission stage). */
  lastResetPasswordError: ResetPasswordErrorOutcome | null
  /** AS.7.4 — structured outcome for the most recent failed MFA
   *  challenge submission (TOTP / backup code / WebAuthn). `null`
   *  on success or until the first failure. */
  lastMfaChallengeError: MfaChallengeErrorOutcome | null
  /** AS.7.5 — structured outcome for the most recent failed
   *  email-verification token submission. `null` on success or
   *  until the first failure. */
  lastEmailVerifyError: EmailVerifyErrorOutcome | null
  /** AS.7.5 — structured outcome for the most recent failed resend
   *  request. `null` on success (terminal copy is shown regardless)
   *  or until the first failure. */
  lastResendVerifyEmailError: ResendVerifyEmailErrorOutcome | null
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
  /** AS.7.3 — request a password-reset email. Resolves with the
   *  canonical `linkSent` terminal state on every 2xx response
   *  regardless of whether the email matched a known account
   *  (enumeration-resistance contract per AS.0.7 §3.4). */
  requestPasswordReset: (
    email: string,
    extras?: Readonly<Record<string, string>>,
  ) => Promise<RequestPasswordResetOutcome>
  /** AS.7.3 — submit the new password using the magic-link token.
   *  Returns a structured outcome the page branches on (success vs
   *  invalid/expired token vs weak password). */
  resetPassword: (
    body: ResetPasswordRequestBody,
    extras?: Readonly<Record<string, string>>,
  ) => Promise<ResetPasswordOutcome>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  submitMfa: (code: string) => Promise<boolean>
  /** AS.7.4 — structured-outcome variant of `submitMfa()` that
   *  routes errors through `classifyMfaChallengeError` so the
   *  dedicated `/mfa-challenge` page can branch on expired-challenge
   *  vs retryable invalid-code without parsing the message string.
   *  Both `submitMfa()` and `submitMfaStructured()` hit the same
   *  backend endpoint; the boolean variant is preserved for the
   *  legacy login-page inline flow. */
  submitMfaStructured: (code: string) => Promise<MfaChallengeOutcome>
  /** AS.7.4 — submit a WebAuthn challenge. The hook orchestrates
   *  the two-step `webauthn/challenge/{begin,complete}` round-trip
   *  including the `navigator.credentials.get()` invocation; the
   *  caller only passes the (begin → get → complete) execution as
   *  one operation. */
  submitMfaWebauthn: () => Promise<MfaChallengeOutcome>
  cancelMfa: () => void
  /** AS.7.5 — submit the magic-link token to the verify-email
   *  endpoint. Returns a structured outcome the page branches on
   *  (success vs invalid/expired/already-verified token). */
  verifyEmail: (
    body: VerifyEmailRequestBody,
    extras?: Readonly<Record<string, string>>,
  ) => Promise<EmailVerifyOutcome>
  /** AS.7.5 — request a fresh verification email. Resolves with
   *  the canonical `linkSent` terminal state on every 2xx response
   *  regardless of whether the email matched a known unverified
   *  user (enumeration-resistance contract per AS.0.7 §3.4). */
  resendEmailVerification: (
    email: string,
    extras?: Readonly<Record<string, string>>,
  ) => Promise<ResendVerifyEmailOutcome>
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
  const [lastRequestResetError, setLastRequestResetError] =
    useState<RequestResetErrorOutcome | null>(null)
  const [lastResetPasswordError, setLastResetPasswordError] =
    useState<ResetPasswordErrorOutcome | null>(null)
  const [lastMfaChallengeError, setLastMfaChallengeError] =
    useState<MfaChallengeErrorOutcome | null>(null)
  const [lastEmailVerifyError, setLastEmailVerifyError] =
    useState<EmailVerifyErrorOutcome | null>(null)
  const [lastResendVerifyEmailError, setLastResendVerifyEmailError] =
    useState<ResendVerifyEmailErrorOutcome | null>(null)
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

  const requestPasswordReset = useCallback(
    async (
      email: string,
      extras?: Readonly<Record<string, string>>,
    ): Promise<RequestPasswordResetOutcome> => {
      try {
        const res: RequestPasswordResetResponse =
          await apiRequestPasswordReset(email, extras)
        // The 2xx response is the canonical terminal-copy branch.
        // We do NOT branch on `link_sent === true|false` for the
        // visible UI copy because the AS.0.7 §3.4 contract requires
        // the same response shape regardless of whether the email
        // matched a known user. Pages render "if your account
        // exists, we've sent a link" full stop.
        setLastRequestResetError(null)
        setError(null)
        return Object.freeze({
          status: "linkSent" as const,
          error: null,
          email: res.email ?? email,
        })
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyRequestResetError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastRequestResetError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
            email: null,
          })
        }
        const msg = exc instanceof Error ? exc.message : String(exc)
        const outcome = classifyRequestResetError({
          status: null,
          message: msg,
          retryAfter: null,
        })
        setLastRequestResetError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
          email: null,
        })
      }
    },
    [],
  )

  const resetPassword = useCallback(
    async (
      body: ResetPasswordRequestBody,
      extras?: Readonly<Record<string, string>>,
    ): Promise<ResetPasswordOutcome> => {
      try {
        const res: ResetPasswordResponse = await apiResetPassword(body, extras)
        setLastResetPasswordError(null)
        setError(null)
        return Object.freeze({
          status: "ok" as const,
          error: null,
          email: res.email ?? null,
        })
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyResetPasswordError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastResetPasswordError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
            email: null,
          })
        }
        const msg = exc instanceof Error ? exc.message : String(exc)
        const outcome = classifyResetPasswordError({
          status: null,
          message: msg,
          retryAfter: null,
        })
        setLastResetPasswordError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
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
      setLastMfaChallengeError(null)
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

  const submitMfaStructured = useCallback(
    async (code: string): Promise<MfaChallengeOutcome> => {
      if (!mfaPending) {
        const outcome = classifyMfaChallengeError({
          status: 410,
          errorCode: null,
          retryAfter: null,
        })
        setLastMfaChallengeError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
        })
      }
      try {
        const res = await apiMfaChallenge(mfaPending.mfa_token, code)
        setUser(res.user)
        setMfaPending(null)
        setError(null)
        setLastMfaChallengeError(null)
        return Object.freeze({ status: "ok" as const, error: null })
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyMfaChallengeError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastMfaChallengeError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
          })
        }
        const msg = exc instanceof Error ? exc.message : String(exc)
        const outcome = classifyMfaChallengeError({
          status: null,
          message: msg,
          retryAfter: null,
        })
        setLastMfaChallengeError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
        })
      }
    },
    [mfaPending],
  )

  const submitMfaWebauthn = useCallback(
    async (): Promise<MfaChallengeOutcome> => {
      if (!mfaPending) {
        const outcome = classifyMfaChallengeError({
          status: 410,
          errorCode: null,
          retryAfter: null,
        })
        setLastMfaChallengeError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
        })
      }
      const buildFailed = (): MfaChallengeOutcome => {
        const outcome = classifyMfaChallengeError({
          status: 400,
          errorCode: "webauthn_failed",
          retryAfter: null,
        })
        setLastMfaChallengeError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
        })
      }
      let assertion: unknown
      try {
        const options = await apiMfaWebauthnChallengeBegin(
          mfaPending.mfa_token,
        )
        const cred = (globalThis as { navigator?: Navigator }).navigator
          ?.credentials
        if (!cred || typeof cred.get !== "function") {
          return buildFailed()
        }
        assertion = await cred.get({
          publicKey: options as unknown as PublicKeyCredentialRequestOptions,
        })
        if (!assertion) {
          return buildFailed()
        }
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyMfaChallengeError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastMfaChallengeError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
          })
        }
        return buildFailed()
      }
      try {
        const res = await apiMfaWebauthnChallengeComplete(
          mfaPending.mfa_token,
          assertion,
        )
        setUser(res.user)
        setMfaPending(null)
        setError(null)
        setLastMfaChallengeError(null)
        return Object.freeze({ status: "ok" as const, error: null })
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyMfaChallengeError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastMfaChallengeError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
          })
        }
        return buildFailed()
      }
    },
    [mfaPending],
  )

  const cancelMfa = useCallback(() => {
    setMfaPending(null)
    setError(null)
    setLastLoginError(null)
    setLastMfaChallengeError(null)
  }, [])

  const verifyEmail = useCallback(
    async (
      body: VerifyEmailRequestBody,
      extras?: Readonly<Record<string, string>>,
    ): Promise<EmailVerifyOutcome> => {
      try {
        const res: VerifyEmailResponse = await apiVerifyEmail(body, extras)
        if (res.user) {
          // Some backend revisions auto-sign-in inline after verify;
          // absorb the user so the page can route to the dashboard
          // without a follow-up whoami round-trip.
          setUser(res.user as AuthUser)
        }
        setLastEmailVerifyError(null)
        setError(null)
        return Object.freeze({
          status: "ok" as const,
          error: null,
          email: res.email ?? null,
        })
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyEmailVerifyError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastEmailVerifyError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
            email: null,
          })
        }
        const msg = exc instanceof Error ? exc.message : String(exc)
        const outcome = classifyEmailVerifyError({
          status: null,
          message: msg,
          retryAfter: null,
        })
        setLastEmailVerifyError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
          email: null,
        })
      }
    },
    [],
  )

  const resendEmailVerification = useCallback(
    async (
      email: string,
      extras?: Readonly<Record<string, string>>,
    ): Promise<ResendVerifyEmailOutcome> => {
      try {
        const res: ResendVerifyEmailResponse =
          await apiResendEmailVerification(email, extras)
        // The 2xx response is the canonical terminal-copy branch.
        // Same enumeration-resistance contract as the AS.7.3
        // request-reset endpoint: the page renders the same "we
        // sent another link" copy regardless of `link_sent`.
        setLastResendVerifyEmailError(null)
        setError(null)
        return Object.freeze({
          status: "linkSent" as const,
          error: null,
          email: res.email ?? email,
        })
      } catch (exc) {
        if (exc instanceof ApiError) {
          const errorCode = _extractErrorCode(exc)
          const retryAfter = _extractRetryAfter(exc)
          const outcome = classifyResendVerifyEmailError({
            status: exc.status,
            errorCode,
            retryAfter,
          })
          setLastResendVerifyEmailError(outcome)
          setError(outcome.message)
          return Object.freeze({
            status: "failed" as const,
            error: outcome,
            email: null,
          })
        }
        const msg = exc instanceof Error ? exc.message : String(exc)
        const outcome = classifyResendVerifyEmailError({
          status: null,
          message: msg,
          retryAfter: null,
        })
        setLastResendVerifyEmailError(outcome)
        setError(outcome.message)
        return Object.freeze({
          status: "failed" as const,
          error: outcome,
          email: null,
        })
      }
    },
    [],
  )

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
    <Ctx.Provider value={{ user, authMode, sessionId, loading, error, lastLoginError, lastSignupError, lastRequestResetError, lastResetPasswordError, lastMfaChallengeError, lastEmailVerifyError, lastResendVerifyEmailError, mfaPending, login, signup, requestPasswordReset, resetPassword, logout, refresh, submitMfa, submitMfaStructured, submitMfaWebauthn, cancelMfa, verifyEmail, resendEmailVerification }}>
      {children}
    </Ctx.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error("useAuth must be used inside <AuthProvider>")
  return v
}

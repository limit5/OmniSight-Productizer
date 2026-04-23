"use client"

import { useEffect, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import { Lock, Mail, AlertCircle, Loader2, Shield, ArrowLeft, KeyRound } from "lucide-react"
import { AuthProvider, useAuth } from "@/lib/auth-context"

// Q.1 UI follow-up (2026-04-24): canonical copy for the "your session
// was ended because a security event happened on another device"
// banner. Backend ``/auth/change-password`` and the MFA routes set the
// ``trigger`` when they log the peer-session revocation; ``lib/api.ts``
// forwards it to ``/login?reason=user_security_event&trigger=<t>``.
// The banner's ``message`` query param (also set by the API layer)
// takes precedence so backend copy wins — this map is the fallback
// when ``message`` is absent (direct navigation, older cached API
// bundle, etc.).
const SESSION_REVOCATION_TRIGGER_COPY: Record<string, string> = {
  password_change:
    "Your password was changed on another device. Please sign in again.",
  totp_enrolled:
    "Two-factor authentication was enabled on another device. Please sign in again.",
  totp_disabled:
    "Two-factor authentication was disabled on another device. Please sign in again.",
  backup_codes_regenerated:
    "Your MFA backup codes were regenerated on another device. Please sign in again.",
  webauthn_registered:
    "A new security key was registered on your account. Please sign in again.",
  webauthn_removed:
    "A security key was removed from your account. Please sign in again.",
  role_change:
    "Your account role was changed by an administrator. Please sign in again.",
  account_disabled:
    "Your account was disabled by an administrator. Contact your administrator for access.",
}

function getSessionRevocationCopy(
  reason: string | null, trigger: string | null, message: string | null,
): string | null {
  if (reason !== "user_security_event") return null
  if (message && message.length > 0) return message
  if (trigger && SESSION_REVOCATION_TRIGGER_COPY[trigger]) {
    return SESSION_REVOCATION_TRIGGER_COPY[trigger]
  }
  return "Your session was ended for security reasons. Please sign in again."
}

function MfaChallengeForm() {
  const router = useRouter()
  const search = useSearchParams()
  const next = search.get("next") || "/"
  const auth = useAuth()
  const [code, setCode] = useState("")
  const [busy, setBusy] = useState(false)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || !code.trim()) return
    setBusy(true)
    try {
      const ok = await auth.submitMfa(code.trim())
      if (ok) router.replace(next)
    } finally {
      setBusy(false)
    }
  }

  const methods = auth.mfaPending?.mfa_methods || []
  const hasTotp = methods.includes("totp")
  const hasWebauthn = methods.includes("webauthn")

  return (
    <form
      onSubmit={onSubmit}
      className="w-full max-w-sm rounded-lg border border-[var(--border)] bg-[var(--card)] p-6 flex flex-col gap-4"
    >
      <div className="text-center">
        <Shield size={28} className="mx-auto mb-2 text-[var(--artifact-purple)]" />
        <h1 className="font-mono text-lg font-semibold text-[var(--foreground)]">
          Two-Factor Authentication
        </h1>
        <p className="font-mono text-xs text-[var(--muted-foreground)] mt-1">
          {hasTotp && "Enter your authenticator code or a backup code"}
          {!hasTotp && hasWebauthn && "Use your security key to continue"}
        </p>
      </div>

      {(hasTotp || true) && (
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            CODE
          </span>
          <div className="flex items-center gap-2 px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus-within:ring-1 focus-within:ring-[var(--artifact-purple)]">
            <Shield size={14} className="text-[var(--muted-foreground)]" />
            <input
              type="text"
              autoComplete="one-time-code"
              autoFocus
              required
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="flex-1 bg-transparent outline-none font-mono text-sm text-[var(--foreground)] tracking-widest"
              placeholder="000000"
              maxLength={20}
            />
          </div>
        </label>
      )}

      {auth.error && (
        <div
          role="alert"
          className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
        >
          <AlertCircle size={14} className="shrink-0 mt-0.5" />
          <span>{auth.error}</span>
        </div>
      )}

      <button
        type="submit"
        disabled={busy || !code.trim()}
        className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : null}
        Verify
      </button>

      <button
        type="button"
        onClick={auth.cancelMfa}
        className="flex items-center justify-center gap-1 font-mono text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
      >
        <ArrowLeft size={12} />
        Back to login
      </button>

      <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
        You can use a 6-digit authenticator code or a backup code (xxxx-xxxx).
      </p>
    </form>
  )
}

function LoginForm() {
  const router = useRouter()
  const search = useSearchParams()
  const next = search.get("next") || "/"
  const auth = useAuth()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [busy, setBusy] = useState(false)

  // Q.1 UI follow-up (2026-04-24): parse the security-event reason the
  // API layer appended to the /login URL when a peer-session rotation
  // kicked this device. Renders a dedicated banner above the form so
  // the operator understands *why* they were logged out (password
  // changed on another device / TOTP change / admin disable) rather
  // than seeing a bare form with no context.
  const revokedReason = search.get("reason")
  const revokedTrigger = search.get("trigger")
  const revokedMessage = search.get("message")
  const revocationCopy = getSessionRevocationCopy(
    revokedReason, revokedTrigger, revokedMessage,
  )

  useEffect(() => {
    if (!auth.loading && auth.user) router.replace(next)
  }, [auth.loading, auth.user, next, router])

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      const ok = await auth.login(email, password)
      if (ok) router.replace(next)
    } finally {
      setBusy(false)
    }
  }

  if (auth.mfaPending) {
    return (
      <main className="min-h-screen flex items-center justify-center p-6 bg-[var(--background)]">
        <MfaChallengeForm />
      </main>
    )
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6 bg-[var(--background)]">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-lg border border-[var(--border)] bg-[var(--card)] p-6 flex flex-col gap-4"
      >
        <div className="text-center">
          <h1 className="font-mono text-lg font-semibold text-[var(--foreground)]">
            OmniSight
          </h1>
          <p className="font-mono text-xs text-[var(--muted-foreground)] mt-1">
            Sign in to continue
          </p>
        </div>

        {revocationCopy && (
          <div
            role="status"
            data-testid="login-session-revoked-banner"
            data-trigger={revokedTrigger || ""}
            className="flex items-start gap-2 p-3 rounded border border-[var(--artifact-purple)] bg-[var(--artifact-purple)]/10 text-[var(--foreground)] font-mono text-xs leading-relaxed"
          >
            <KeyRound size={14} className="shrink-0 mt-0.5 text-[var(--artifact-purple)]" />
            <div className="flex flex-col gap-1">
              <span className="font-semibold tracking-wider text-[10px] text-[var(--artifact-purple)]">
                SESSION ENDED
              </span>
              <span>{revocationCopy}</span>
            </div>
          </div>
        )}

        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            EMAIL
          </span>
          <div className="flex items-center gap-2 px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus-within:ring-1 focus-within:ring-[var(--artifact-purple)]">
            <Mail size={14} className="text-[var(--muted-foreground)]" />
            <input
              type="email"
              autoComplete="email"
              autoFocus
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="flex-1 bg-transparent outline-none font-mono text-sm text-[var(--foreground)]"
              placeholder="you@example.com"
            />
          </div>
        </label>

        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] tracking-wider text-[var(--muted-foreground)]">
            PASSWORD
          </span>
          <div className="flex items-center gap-2 px-2 py-1.5 rounded border border-[var(--border)] bg-[var(--background)] focus-within:ring-1 focus-within:ring-[var(--artifact-purple)]">
            <Lock size={14} className="text-[var(--muted-foreground)]" />
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="flex-1 bg-transparent outline-none font-mono text-sm text-[var(--foreground)]"
            />
          </div>
        </label>

        {auth.error && (
          <div
            role="alert"
            className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
          >
            <AlertCircle size={14} className="shrink-0 mt-0.5" />
            <span>{auth.error}</span>
          </div>
        )}

        <button
          type="submit"
          disabled={busy || !email || !password}
          className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : null}
          Sign in
        </button>

        <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
          First boot? Bootstrap admin email is whatever you set in
          <code className="mx-1">OMNISIGHT_ADMIN_EMAIL</code>
          (default: <code>admin@omnisight.local</code>).
        </p>
      </form>
    </main>
  )
}

// Phase-3 P5 (2026-04-20) — remove the per-page ``<AuthProvider>``
// wrap. ``app/layout.tsx`` already mounts ``<Providers>`` (which
// includes ``<AuthProvider>``) around every route's children, so
// re-wrapping here was creating a SECOND nested AuthProvider with
// independent state. The nested one handled the login call and set
// ``user = admin`` on its own state, but after
// ``router.replace("/")`` the dashboard read the OUTER provider's
// state — still ``user = null`` because the nested provider's
// login setState never reached it. Dashboard's guard effect saw
// ``!user``, redirected back to /login; /login re-mounted the inner
// AuthProvider with fresh ``user = null``; operator retried login;
// inner got it; navigation to / read outer (still null); redirect
// back; loop. Dropping the nested wrapper makes login() + setUser
// update the SAME AuthProvider instance that Home then reads, so
// state is coherent across the route change.
export default function LoginPage() {
  return <LoginForm />
}

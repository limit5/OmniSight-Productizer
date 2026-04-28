"use client"

/**
 * AS.7.1 — Login page redesign.
 *
 * Composes the AS.7.0 visual foundation (`<AuthVisualFoundation>` +
 * `<AuthGlassCard>` + `<AuthBrandWordmark>`) with the AS.7.1 login-
 * specific primitives:
 *
 *   - `<AuthFieldElectric>` for email + password (corner brackets
 *     snap on focus / gradient border / scan line / spring-shake +
 *     red lightning on error)
 *   - `<OAuthEnergySphere>` × 5 primary (Google / GitHub / Microsoft
 *     / Apple / Discord) + `More` toggle revealing 6 secondary
 *     vendors behind a row of smaller spheres
 *   - `<AuthTurnstileWidget>` widget — rendered when
 *     `NEXT_PUBLIC_TURNSTILE_SITE_KEY` env is set; otherwise the
 *     widget is a noop and the AS.6.3 backend Phase-1 fail-open
 *     contract carries the request
 *   - `<AuthHoneypotField>` — hidden field with the rotating AS.6.4
 *     name backed by Web Crypto SHA-256
 *   - `<AccountLockedOverlay>` — blue tint + frozen overlay shown
 *     when the backend returns 423 (driven by `auth.lastLoginError
 *     .accountLocked`)
 *   - `<WarpDriveTransition>` — fullscreen warp animation playing
 *     between `auth.login()` resolving truthy and the actual
 *     `router.replace(next)` so the page doesn't pop straight to
 *     the dashboard
 *
 * MFA flow integration: when `auth.login()` returns false because
 * the backend issued an mfa_token, we route to the in-page
 * `<MfaChallengeForm>` mounted on the same glass card, preserving
 * the foundation + warp-drive transition.
 *
 * Module-global state audit (per implement_phase_step.md SOP §1):
 * pure browser component. All state lives in React (`useState` /
 * `useRef`). The auth context is the SoT for user / mfa / error.
 * Per-tab / per-worker derivation is trivially identical (Answer
 * #1 of the SOP audit).
 *
 * Read-after-write timing audit: N/A — no DB, no parallelisation
 * change vs. existing auth-context behaviour.
 */

import { useEffect, useMemo, useRef, useState } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import {
  AlertCircle,
  ArrowLeft,
  ChevronDown,
  KeyRound,
  Loader2,
  Lock,
  Mail,
  Shield,
} from "lucide-react"

import { useAuth } from "@/lib/auth-context"
import { useEffectiveMotionLevel } from "@/hooks/use-effective-motion-level"
import {
  AccountLockedOverlay,
  AuthBrandWordmark,
  AuthFieldElectric,
  AuthGlassCard,
  AuthHoneypotField,
  AuthTurnstileWidget,
  AuthVisualFoundation,
  OAuthEnergySphere,
  OAuthProviderIcon,
  WarpDriveTransition,
} from "@/components/omnisight/auth"
import {
  buildOAuthAuthorizeUrl,
  getPrimaryProviders,
  getSecondaryProviders,
  type OAuthProviderId,
} from "@/lib/auth/oauth-providers"
import { bumpShakeKey } from "@/lib/auth/login-form-helpers"

// AS.7.1 — Q.1 banner copy carried across from the previous page
// rewrite. Kept inline so an operator landing on /login?reason=...
// after a peer-session revocation still sees the explanation.
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
  not_me_cascade:
    "You flagged a new-device login as suspicious. Every session was signed out and you will be required to change your password after signing in again.",
}

function getSessionRevocationCopy(
  reason: string | null,
  trigger: string | null,
  message: string | null,
): string | null {
  if (reason !== "user_security_event") return null
  if (message && message.length > 0) return message
  if (trigger && SESSION_REVOCATION_TRIGGER_COPY[trigger]) {
    return SESSION_REVOCATION_TRIGGER_COPY[trigger]
  }
  return "Your session was ended for security reasons. Please sign in again."
}

// ─────────────────────────────────────────────────────────────────
// MFA challenge form — re-used inside the AS.7.0 glass card.
// ─────────────────────────────────────────────────────────────────

function MfaChallengeForm({ onCompleted }: { onCompleted: () => void }) {
  const auth = useAuth()
  const [code, setCode] = useState("")
  const [busy, setBusy] = useState(false)
  const [errorKey, setErrorKey] = useState(0)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy || !code.trim()) return
    setBusy(true)
    try {
      const ok = await auth.submitMfa(code.trim())
      if (ok) {
        onCompleted()
      } else {
        setErrorKey(bumpShakeKey)
      }
    } finally {
      setBusy(false)
    }
  }

  const methods = auth.mfaPending?.mfa_methods || []
  const hasTotp = methods.includes("totp")
  const hasWebauthn = methods.includes("webauthn")
  const hint = hasTotp
    ? "Enter your authenticator code or a backup code"
    : hasWebauthn
    ? "Use your security key to continue"
    : "Enter your verification code"

  return (
    <form
      onSubmit={onSubmit}
      data-testid="as7-mfa-form"
      className="flex flex-col gap-4"
    >
      <div className="flex flex-col items-center text-center gap-1.5">
        <Shield size={26} className="text-[var(--artifact-purple)]" />
        <h1 className="font-mono text-base font-semibold text-[var(--foreground)]">
          Two-Factor Authentication
        </h1>
        <p className="font-mono text-[11px] text-[var(--muted-foreground)]">
          {hint}
        </p>
      </div>

      <AuthFieldElectric
        level="dramatic"
        label="CODE"
        leadingIcon={<Shield size={14} />}
        hasError={Boolean(auth.error)}
        errorKey={errorKey}
        inputProps={{
          name: "mfa_code",
          type: "text",
          autoComplete: "one-time-code",
          inputMode: "numeric",
          autoFocus: true,
          required: true,
          maxLength: 32,
          placeholder: "000000",
          value: code,
          onChange: (e) => setCode(e.target.value),
        }}
      />

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

// ─────────────────────────────────────────────────────────────────
// Login form — composed inside the AS.7.0 glass card.
// ─────────────────────────────────────────────────────────────────

const TURNSTILE_SITE_KEY: string | null =
  process.env.NEXT_PUBLIC_TURNSTILE_SITE_KEY ?? null

function LoginForm() {
  const router = useRouter()
  const search = useSearchParams()
  const next = search.get("next") || "/"
  const auth = useAuth()
  const level = useEffectiveMotionLevel()

  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [busy, setBusy] = useState(false)
  const [errorKey, setErrorKey] = useState(0)
  const [warpActive, setWarpActive] = useState(false)
  const [showSecondary, setShowSecondary] = useState(false)
  const [bloomKey, setBloomKey] = useState(0)
  const [turnstileToken, setTurnstileToken] = useState<string | null>(null)
  const honeypotFieldRef = useRef<string | null>(null)

  // Q.1 UI follow-up — security-event session revocation banner.
  const revokedReason = search.get("reason")
  const revokedTrigger = search.get("trigger")
  const revokedMessage = search.get("message")
  const revocationCopy = getSessionRevocationCopy(
    revokedReason,
    revokedTrigger,
    revokedMessage,
  )

  // Track the locked-overlay countdown locally — the parent owns
  // the timer so the `AccountLockedOverlay` leaf stays presentation-
  // only.
  const [lockedRemaining, setLockedRemaining] = useState<number | null>(null)
  useEffect(() => {
    if (!auth.lastLoginError?.accountLocked) {
      setLockedRemaining(null)
      return
    }
    const start = auth.lastLoginError.retryAfterSeconds
    if (start === null || start <= 0) {
      setLockedRemaining(null)
      return
    }
    setLockedRemaining(start)
    const timer = window.setInterval(() => {
      setLockedRemaining((prev) => {
        if (prev === null) return null
        return prev > 1 ? prev - 1 : 0
      })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [auth.lastLoginError])

  useEffect(() => {
    if (!auth.loading && auth.user) router.replace(next)
  }, [auth.loading, auth.user, next, router])

  // AS.7.4 — When the backend issued an MFA challenge, push the
  // user to the dedicated `/mfa-challenge` page. The auth context
  // is the SoT for `mfaPending`, so the new page can read it from
  // the same provider without a query-string handoff.
  useEffect(() => {
    if (auth.mfaPending) {
      router.push(`/mfa-challenge?next=${encodeURIComponent(next)}`)
    }
  }, [auth.mfaPending, next, router])

  const primaryProviders = useMemo(getPrimaryProviders, [])
  const secondaryProviders = useMemo(getSecondaryProviders, [])

  const onFieldFocus = () => {
    setBloomKey((k) => k + 1)
  }

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    try {
      const extras: Record<string, string> = {}
      if (turnstileToken) extras.turnstile_token = turnstileToken
      if (honeypotFieldRef.current) {
        // The honeypot field must be empty to pass — the value goes
        // out as the empty string which is the canonical "not filled"
        // signal per AS.4.1 §3.
        extras[honeypotFieldRef.current] = ""
      }
      const ok = await auth.login(email, password, extras)
      if (ok) {
        // AS.7.1 — warp drive transition before navigating. The
        // overlay's onComplete callback fires router.replace(next).
        setWarpActive(true)
      } else {
        // Failed (or routed to MFA). Bump the shake key so the
        // form re-mounts the spring-shake animation.
        setErrorKey(bumpShakeKey)
      }
    } finally {
      setBusy(false)
    }
  }

  const onWarpComplete = () => {
    router.replace(next)
  }

  if (auth.mfaPending) {
    return <MfaChallengeForm onCompleted={() => setWarpActive(true)} />
  }

  const hasError = Boolean(auth.error)
  const accountLocked = Boolean(auth.lastLoginError?.accountLocked)

  return (
    <>
      <form
        onSubmit={onSubmit}
        data-testid="as7-login-form"
        className="flex flex-col gap-4 relative"
      >
        <div className="flex flex-col items-center gap-1.5 text-center">
          <AuthBrandWordmark level={level} bloomKey={bloomKey} />
          <p className="font-mono text-[11px] text-[var(--muted-foreground)]">
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
            <KeyRound
              size={14}
              className="shrink-0 mt-0.5 text-[var(--artifact-purple)]"
            />
            <div className="flex flex-col gap-1">
              <span className="font-semibold tracking-wider text-[10px] text-[var(--artifact-purple)]">
                SESSION ENDED
              </span>
              <span>{revocationCopy}</span>
            </div>
          </div>
        )}

        <AuthFieldElectric
          level={level}
          label="EMAIL"
          leadingIcon={<Mail size={14} />}
          hasError={hasError}
          errorKey={errorKey}
          inputProps={{
            name: "email",
            type: "email",
            autoComplete: "email",
            autoFocus: true,
            required: true,
            placeholder: "you@example.com",
            value: email,
            onChange: (e) => setEmail(e.target.value),
            onFocus: onFieldFocus,
          }}
        />

        <AuthFieldElectric
          level={level}
          label="PASSWORD"
          leadingIcon={<Lock size={14} />}
          hasError={hasError}
          errorKey={errorKey}
          inputProps={{
            name: "password",
            type: "password",
            autoComplete: "current-password",
            required: true,
            value: password,
            onChange: (e) => setPassword(e.target.value),
            onFocus: onFieldFocus,
          }}
        />

        {/* AS.7.3 — Forgot-password link. Forwards the typed email
            so the next page pre-fills it; users with no email yet
            land on a blank input. */}
        <a
          href={
            email
              ? `/forgot-password?email=${encodeURIComponent(email)}`
              : "/forgot-password"
          }
          data-testid="as7-login-forgot-link"
          className="self-end font-mono text-[10px] text-[var(--muted-foreground)] hover:text-[var(--foreground)] underline"
        >
          Forgot your password?
        </a>

        {/* AS.6.4 honeypot — rotating field name. Renders empty
            placeholder until SHA-256 resolves; the form's submit
            adds the resolved key to the extras payload. */}
        <AuthHoneypotField
          onResolved={(name) => {
            honeypotFieldRef.current = name
          }}
        />

        {/* AS.6.3 Turnstile — only mounts when the env var is set.
            The widget's onToken callback feeds turnstileToken which
            the submit handler threads as `turnstile_token`. */}
        {TURNSTILE_SITE_KEY ? (
          <div className="flex justify-center">
            <AuthTurnstileWidget
              siteKey={TURNSTILE_SITE_KEY}
              onToken={(token) => setTurnstileToken(token)}
              onExpired={() => setTurnstileToken(null)}
              onError={() => setTurnstileToken(null)}
            />
          </div>
        ) : null}

        {auth.error && !accountLocked && (
          <div
            role="alert"
            data-testid="as7-login-error"
            className="flex items-start gap-2 p-2 rounded border border-[var(--destructive)] bg-[var(--destructive)]/10 text-[var(--destructive)] font-mono text-xs"
          >
            <AlertCircle size={14} className="shrink-0 mt-0.5" />
            <span>{auth.error}</span>
          </div>
        )}

        <button
          type="submit"
          disabled={busy || !email || !password || accountLocked}
          className="flex items-center justify-center gap-2 px-3 py-2 rounded bg-[var(--artifact-purple)] text-white font-mono text-sm font-semibold hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : null}
          Sign in
        </button>

        {/* OAuth row — 5 primary providers always visible + a More
            toggle that reveals the secondary 6 in a second row. */}
        <div className="flex flex-col gap-3 mt-1">
          <div className="flex items-center gap-2 text-[var(--muted-foreground)] font-mono text-[10px] tracking-wider">
            <span className="flex-1 h-px bg-[var(--border)]" />
            OR CONTINUE WITH
            <span className="flex-1 h-px bg-[var(--border)]" />
          </div>
          <div
            data-testid="as7-oauth-row-primary"
            className="flex items-center justify-center gap-3 flex-wrap"
          >
            {primaryProviders.map((p) => (
              <OAuthEnergySphere
                key={p.id}
                level={level}
                provider={p}
                href={buildOAuthAuthorizeUrl(p.id, next)}
                icon={<OAuthProviderIcon id={p.id as OAuthProviderId} />}
                disabled={accountLocked}
              />
            ))}
          </div>
          <button
            type="button"
            data-testid="as7-oauth-more-toggle"
            onClick={() => setShowSecondary((v) => !v)}
            className="self-center flex items-center gap-1 px-2 py-1 rounded font-mono text-[11px] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            aria-expanded={showSecondary}
          >
            <ChevronDown
              size={12}
              style={{
                transform: showSecondary ? "rotate(180deg)" : "rotate(0deg)",
                transition: "transform 180ms ease",
              }}
            />
            {showSecondary ? "Hide more" : "More providers"}
          </button>
          {showSecondary && (
            <div
              data-testid="as7-oauth-row-secondary"
              className="flex items-center justify-center gap-2 flex-wrap"
            >
              {secondaryProviders.map((p) => (
                <OAuthEnergySphere
                  key={p.id}
                  level={level}
                  provider={p}
                  href={buildOAuthAuthorizeUrl(p.id, next)}
                  icon={<OAuthProviderIcon id={p.id as OAuthProviderId} />}
                  size="secondary"
                  disabled={accountLocked}
                />
              ))}
            </div>
          )}
        </div>

        <p className="font-mono text-[10px] text-[var(--muted-foreground)] text-center leading-relaxed">
          First boot? Bootstrap admin email is whatever you set in
          <code className="mx-1">OMNISIGHT_ADMIN_EMAIL</code>
          (default: <code>admin@omnisight.local</code>).
        </p>

        {/* 423 / lockout overlay — only renders when the backend
            reported account_locked. Sits inside the form so it
            covers the inputs + submit button visually. */}
        {accountLocked ? (
          <AccountLockedOverlay
            level={level}
            remainingSeconds={lockedRemaining}
          />
        ) : null}
      </form>

      <WarpDriveTransition
        level={level}
        active={warpActive}
        onComplete={onWarpComplete}
      />
    </>
  )
}

// Phase-3 P5 (2026-04-20) — `<Providers>` already mounts a single
// `<AuthProvider>` at the app layout; this page no longer wraps a
// nested provider. AS.7.1 keeps the same flat shape.
function LoginScaffold() {
  // Resolve the motion level once at the page root and pass it
  // down. `<AuthVisualFoundation>` accepts `forceLevel` so it
  // re-uses the same value rather than running its own resolver
  // and risking a one-frame mismatch with the glass card.
  const level = useEffectiveMotionLevel()
  return (
    <AuthVisualFoundation forceLevel={level}>
      <AuthGlassCard level={level}>
        <LoginForm />
      </AuthGlassCard>
    </AuthVisualFoundation>
  )
}

export default function LoginPage() {
  return <LoginScaffold />
}
